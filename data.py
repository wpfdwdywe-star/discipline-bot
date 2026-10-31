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

# AKShare 内部的请求大多没设超时，服务器不回数据时会一直挂着，把调度线程卡死（10-27 同花顺财报接口卡住，收盘简报没发）。
# 没指定超时的请求一律补上默认超时，超时抛异常走重试/降级。
REQUEST_TIMEOUT = 20
_session_request = requests.Session.request


def _request_with_timeout(self, method, url, **kw):
    if kw.get("timeout") is None:
        kw["timeout"] = REQUEST_TIMEOUT
    return _session_request(self, method, url, **kw)


requests.Session.request = _request_with_timeout

TZ = ZoneInfo("Asia/Shanghai")
CALENDAR_CACHE = Path(os.environ.get("DISCIPLINE_DATA") or Path(__file__).parent / "data") / "trade_calendar.json"

# 系统时钟要额外加的校正（秒）。平时是 0；只有系统时钟持续不准时，调度器才会设它（见 core.Scheduler._clock_ok）。
_clock_offset = 0.0


def now() -> dt.datetime:
    return dt.datetime.now(TZ) + dt.timedelta(seconds=_clock_offset)


def clock_skew() -> float | None:
    """网络时间 − 系统时钟（秒），只测量、不改任何状态。拿不到网络时间返回 None。

    休眠的机器刚被唤醒时，系统时钟会停在暂停那一刻，过几秒才被平台校准；这时测出来的差会很大。
    """
    for url in ("https://www.baidu.com", "https://hq.sinajs.cn"):
        try:
            before = time.time()
            r = requests.head(url, timeout=5)
            server = email.utils.parsedate_to_datetime(r.headers["Date"]).timestamp()
            return server - (before + time.time()) / 2
        except Exception:
            continue
    return None


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
    return _with_today_bar(df.sort_values("date"), _sina_symbol(code)).tail(days).reset_index(drop=True)


BAR_COLS = ["open", "high", "low", "close", "volume", "amount"]


def today_bar_from_minutes(sina_symbol: str) -> dict | None:
    """收盘后用当天分钟线合成今天的日K。新浪日K要到 16:30 左右才更新，分钟线 15:00 那根收盘后马上就有。

    只有拿到完整的一天（最后一根是 15:00）才返回，盘中不合成半截K线。实测和官方日K一致。
    """
    d = today()
    try:
        m = _retry(lambda: ak.stock_zh_a_minute(symbol=sina_symbol, period="1", adjust=""))
    except Exception:
        return None
    m = m[m["day"].astype(str).str.startswith(d)]
    if m.empty or str(m["day"].iloc[-1])[11:16] < "15:00":
        return None
    m = m.astype({c: float for c in BAR_COLS})
    return {"date": d, "open": m["open"].iloc[0], "high": m["high"].max(), "low": m["low"].min(),
            "close": m["close"].iloc[-1], "volume": m["volume"].sum(), "amount": m["amount"].sum()}


def _with_today_bar(df: pd.DataFrame, sina_symbol: str) -> pd.DataFrame:
    """收盘后（15:01 起）日K还没有今天那根，就用分钟线补上。"""
    if df.empty or df["date"].iloc[-1] >= today() or now().time() < dt.time(15, 1):
        return df
    bar = today_bar_from_minutes(sina_symbol)
    return pd.concat([df, pd.DataFrame([bar])], ignore_index=True) if bar else df


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


def valuation(code: str, indicator: str) -> pd.DataFrame | None:
    """近 5 年估值序列（百度）：indicator 为 "市盈率(TTM)" 或 "市净率"。按天缓存，拿不到返回 None。"""
    cache = CALENDAR_CACHE.parent / "valuation" / f"{code}_{indicator}.json"
    if cache.exists():
        saved = json.loads(cache.read_text())
        if saved["date"] == today():
            return pd.DataFrame(saved["rows"])
    try:
        df = _retry(lambda: ak.stock_zh_valuation_baidu(symbol=code, indicator=indicator, period="近五年"))
    except Exception:
        return None
    df = df[["date", "value"]].astype({"date": str, "value": float})
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"date": today(), "rows": df.to_dict("records")}, ensure_ascii=False))
    return df


def top10_holders(code: str) -> dict | None:
    """前十大股东（占总股本比例）。东财优先（数据完整），新浪备用（部分持股比例会缺，合计偏小时标为不完整）。

    返回 {"date", "total", "top1_name", "top1_pct", "complete"}；拿不到返回 None。
    """
    d = now().date()
    quarter_ends = [f"{y}{md}" for y in (d.year, d.year - 1) for md in ("1231", "0930", "0630", "0331")]
    for q in [q for q in quarter_ends if q <= d.strftime("%Y%m%d")][:4]:
        try:
            df = ak.stock_gdfx_top_10_em(symbol=_sina_symbol(code), date=q)
        except Exception:
            continue
        if len(df):
            pcts = df["占总股本持股比例"].astype(float)
            return {"date": f"{q[:4]}-{q[4:6]}-{q[6:]}", "total": round(float(pcts.sum()), 2),
                    "top1_name": str(df["股东名称"].iloc[0]), "top1_pct": float(pcts.iloc[0]), "complete": True}
    try:
        df = _retry(lambda: ak.stock_main_stock_holder(stock=code))
    except Exception:
        return None
    df = df[df["截至日期"].astype(str) == df["截至日期"].astype(str).max()].head(10)
    pcts = pd.to_numeric(df["持股比例"], errors="coerce")
    return {"date": str(df["截至日期"].iloc[0])[:10], "total": round(float(pcts.sum()), 2),
            "top1_name": str(df["股东名称"].iloc[0]), "top1_pct": float(pcts.iloc[0]) if pd.notna(pcts.iloc[0]) else None,
            "complete": bool(pcts.notna().all())}


def index_bars(symbol: str = "sh000300") -> pd.DataFrame:
    df = _retry(lambda: ak.stock_zh_index_daily(symbol=symbol))
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.astype({c: float for c in ["open", "high", "low", "close", "volume"]})
    return _with_today_bar(df.sort_values("date"), symbol).tail(5).reset_index(drop=True)


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
