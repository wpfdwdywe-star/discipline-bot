"""行情数据（AKShare），带重试和备用数据源。"""
import datetime as dt
import email.utils
import json
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
import requests

TZ = ZoneInfo("Asia/Shanghai")
CALENDAR_CACHE = Path(os.environ.get("DISCIPLINE_DATA") or Path(__file__).parent / "data") / "trade_calendar.json"

# 系统时钟和网络时间的差（秒）。休眠恢复后系统时钟可能不准，又没有 NTP，用 HTTP Date 头校正。
_clock_offset = 0.0


def now() -> dt.datetime:
    return dt.datetime.now(TZ) + dt.timedelta(seconds=_clock_offset)


def sync_clock() -> float:
    """用新浪服务器的 Date 头校时，返回校正量（秒）。失败时保持原样。"""
    global _clock_offset
    try:
        before = time.time()
        r = requests.head("https://hq.sinajs.cn", timeout=5)
        server = email.utils.parsedate_to_datetime(r.headers["Date"]).timestamp()
        offset = server - (before + time.time()) / 2
        _clock_offset = offset if abs(offset) > 5 else 0.0
    except Exception:
        pass
    return _clock_offset


def today() -> str:
    return now().strftime("%Y-%m-%d")


def _retry(fn, tries=3, wait=3):
    for i in range(tries):
        try:
            return fn()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(wait)


def _sina_symbol(code: str) -> str:
    if code.startswith(("6", "9")):
        return "sh" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


def daily_bars(code: str, days: int = 200) -> pd.DataFrame:
    """不复权日K（价格和你在软件里看到的一致）。"""
    end = now().strftime("%Y%m%d")
    start = (now() - dt.timedelta(days=days * 1.6)).strftime("%Y%m%d")
    # 新浪优先：从海外服务器访问东方财富经常被断开
    try:
        df = _retry(lambda: ak.stock_zh_a_daily(symbol=_sina_symbol(code), start_date=start, end_date=end, adjust=""))
    except Exception:
        df = _retry(lambda: ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust=""))
        df = df.rename(columns={"日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
                                "最低": "low", "成交量": "volume", "成交额": "amount"})
        df["volume"] = df["volume"].astype(float) * 100  # 东财单位是手，统一成股（和新浪、分钟线一致）
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df[["date", "open", "high", "low", "close", "volume", "amount"]]
    df[["open", "high", "low", "close", "volume", "amount"]] = df[["open", "high", "low", "close", "volume", "amount"]].astype(float)
    return df.sort_values("date").tail(days).reset_index(drop=True)


def exright_factor(code: str, since: str) -> float:
    """since 之后发生的分红送转累计因子（<1 表示除过权）。since 当天的 前复权价/不复权价。"""
    start = since.replace("-", "")
    end = now().strftime("%Y%m%d")
    sym = _sina_symbol(code)
    raw = _retry(lambda: ak.stock_zh_a_daily(symbol=sym, start_date=start, end_date=end, adjust=""))
    qfq = _retry(lambda: ak.stock_zh_a_daily(symbol=sym, start_date=start, end_date=end, adjust="qfq"))
    return float(qfq["close"].astype(float).iloc[0]) / float(raw["close"].astype(float).iloc[0])


MAX_LAG = 180  # 分钟线最新一根落后现在超过 3 分钟，算数据延迟


def minutes_lag(df: pd.DataFrame) -> float:
    """分钟线最新一根距离现在多少秒。空表返回无穷大。"""
    if df.empty:
        return float("inf")
    last = dt.datetime.fromisoformat(str(df["time"].iloc[-1])).replace(tzinfo=TZ)
    return max(0.0, (now() - last).total_seconds())


def _minutes_sina(code: str, d: str) -> pd.DataFrame:
    df = _retry(lambda: ak.stock_zh_a_minute(symbol=_sina_symbol(code), period="1", adjust=""))
    return df.rename(columns={"day": "time"})


def _minutes_em(code: str, d: str) -> pd.DataFrame:
    df = _retry(lambda: ak.stock_zh_a_hist_min_em(symbol=code, period="1", start_date=f"{d} 09:30:00",
                                                   end_date=f"{d} 15:00:00", adjust=""))
    df = df.rename(columns={"时间": "time", "收盘": "close", "成交量": "volume", "成交额": "amount"})
    df["volume"] = df["volume"].astype(float) * 100  # 东财单位是手
    return df


def today_minutes(code: str) -> pd.DataFrame:
    """当天1分钟线，volume 单位统一为股。新浪优先；新浪出错或延迟超过 MAX_LAG 就试东财，取更新的那份。

    没有数据（停牌等）返回空表。用 minutes_lag() 看数据新不新鲜。
    """
    d = today()
    best = pd.DataFrame(columns=["time", "close", "volume", "amount"])
    for source in (_minutes_sina, _minutes_em):
        try:
            df = source(code, d)
        except Exception:
            continue
        df = df[df["time"].astype(str).str.startswith(d)][["time", "close", "volume", "amount"]].copy()
        df[["close", "volume", "amount"]] = df[["close", "volume", "amount"]].astype(float)
        df = df.reset_index(drop=True)
        if minutes_lag(df) < minutes_lag(best):
            best = df
        if minutes_lag(best) <= MAX_LAG:
            break
    return best


def financial_abstract(code: str) -> pd.DataFrame | None:
    """同花顺财务摘要（按报告期）。一个季度才更新一次，按天缓存；拿不到返回 None。"""
    cache = CALENDAR_CACHE.parent / "financials" / f"{code}.json"
    if cache.exists():
        saved = json.loads(cache.read_text())
        if saved["date"] == today():
            return pd.DataFrame(saved["rows"])
    try:
        df = _retry(lambda: ak.stock_financial_abstract_ths(symbol=code, indicator="按报告期"))
    except Exception:
        return pd.DataFrame(json.loads(cache.read_text())["rows"]) if cache.exists() else None
    df = df.astype(str)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"date": today(), "rows": df.to_dict("records")}, ensure_ascii=False))
    return df


def index_bars(symbol: str = "sh000300") -> pd.DataFrame:
    df = _retry(lambda: ak.stock_zh_index_daily(symbol=symbol))
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    return df.tail(5).reset_index(drop=True)


def stock_name(code: str) -> str:
    try:
        r = requests.get(f"https://hq.sinajs.cn/list={_sina_symbol(code)}", timeout=10,
                         headers={"Referer": "https://finance.sina.com.cn"})
        return r.content.decode("gbk").split('"')[1].split(",")[0] or code
    except Exception:
        return code


def trading_days() -> list[str]:
    cached = json.loads(CALENDAR_CACHE.read_text()) if CALENDAR_CACHE.exists() else []
    if not cached or cached[-1] < today():
        df = _retry(ak.tool_trade_date_hist_sina)
        cached = [str(x) for x in df["trade_date"]]
        CALENDAR_CACHE.parent.mkdir(exist_ok=True)
        CALENDAR_CACHE.write_text(json.dumps(cached))
    return cached


def is_trading_day(date: str | None = None) -> bool:
    return (date or today()) in set(trading_days())


def trading_days_between(a: str, b: str) -> int:
    """a 之后到 b（含）之间有几个交易日。"""
    return sum(1 for d in trading_days() if a < d <= b)
