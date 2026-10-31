"""交易纪律规则（纯逻辑，不联网，方便测试）。

K线 DataFrame 统一列名：date, open, high, low, close, volume, amount，按日期升序。
"""
from dataclasses import dataclass

import pandas as pd

# ---- 规则参数（v1，和你确认过的） ----
MAX_AMPLITUDE = 0.10      # 横盘：区间 (最高-最低)/最低 ≤ 10%
MIN_PLATFORM_DAYS = 3     # 至少 3 个交易日才算支撑
MID_PLATFORM_DAYS = 10    # ≥10 天 中支撑
BIG_PLATFORM_DAYS = 20    # ≥20 天 大平台
VOLUME_RATIO = 1.5        # 放量：当日量 ≥ 前5日均量 × 1.5
VOLUME_LOOKBACK = 5


@dataclass
class Platform:
    start: str
    end: str
    days: int
    support: float   # 平台最低收盘价
    high: float
    low: float

    @property
    def level(self) -> str:
        if self.days >= BIG_PLATFORM_DAYS:
            return "大平台"
        if self.days >= MID_PLATFORM_DAYS:
            return "中支撑"
        return "小支撑"


def _amplitude(window: pd.DataFrame) -> float:
    low = window["low"].min()
    return (window["high"].max() - low) / low


def find_platforms(bars: pd.DataFrame, lookback: int = 120, limit: int = 5) -> list[Platform]:
    """从最近往前找横盘平台，返回最近的若干个（最新的在前）。"""
    bars = bars.reset_index(drop=True)
    n = len(bars)
    found = []
    end = n - 1
    while end >= max(0, n - lookback) and len(found) < limit:
        start = end
        while start > 0 and _amplitude(bars.iloc[start - 1:end + 1]) <= MAX_AMPLITUDE:
            start -= 1
        days = end - start + 1
        if days >= MIN_PLATFORM_DAYS:
            w = bars.iloc[start:end + 1]
            found.append(Platform(
                start=str(w["date"].iloc[0]), end=str(w["date"].iloc[-1]), days=days,
                support=round(float(w["close"].min()), 2),
                high=float(w["high"].max()), low=float(w["low"].min()),
            ))
            end = start - 1
        else:
            end -= 1
    return found


@dataclass
class Breakdown:
    broken: bool
    heavy_volume: bool
    close: float
    volume_ratio: float | None


def check_close(bars: pd.DataFrame, support: float) -> Breakdown:
    """第1天收盘检查：收盘价跌破支撑位即算跌破（不看量），放量只用于标红。"""
    last = bars.iloc[-1]
    prev = bars["volume"].iloc[-1 - VOLUME_LOOKBACK:-1]
    ratio = float(last["volume"] / prev.mean()) if len(prev) == VOLUME_LOOKBACK and prev.mean() > 0 else None
    return Breakdown(
        broken=bool(last["close"] < support),
        heavy_volume=ratio is not None and ratio >= VOLUME_RATIO,
        close=float(last["close"]),
        volume_ratio=ratio,
    )


@dataclass
class Recovery:
    recovered: bool
    price: float
    vwap: float
    above_support: bool
    above_vwap: bool


def check_recovery(minutes: pd.DataFrame, support: float) -> Recovery:
    """第2天 14:40 检查：现价 ≥ 支撑位 且 现价 ≥ 分时均价，才算有效收回。

    minutes: 当天分钟线，需要 close, volume, amount 列（volume 单位为股）。
    """
    price = float(minutes["close"].iloc[-1])
    vol = minutes["volume"].sum()
    vwap = float(minutes["amount"].sum() / vol) if vol > 0 else price
    above_support = price >= support
    above_vwap = price >= vwap
    return Recovery(bool(above_support and above_vwap), price, round(vwap, 3), bool(above_support), bool(above_vwap))


# ---- 回补买入（减仓后等回落再买回） ----
REBUY_ZONE = 0.03         # 回补区：支撑位上方 3% 以内
REBUY_STABLE_DAYS = 3     # 企稳：进入回补区后连续 3 天收盘不破支撑

WAITING = "等待回落"
IN_ZONE = "回补区"
READY = "可买回"
FAILED = "支撑失效"


@dataclass
class RebuyStep:
    status: str
    days: int
    event: str | None   # 状态变化时的事件：进入回补区 / 可买回 / 跌穿；没变化为 None


def check_rebuy(status: str, days: int, close: float, support: float) -> RebuyStep:
    """回补计划的每日收盘判断。days = 进入回补区以来连续收盘不破支撑的天数。"""
    if status == FAILED:
        return RebuyStep(FAILED, 0, None)
    if close < support:
        # 收盘跌破回补价 = 有效跌破（和止损同一标准），不管之前到了哪一步
        return RebuyStep(FAILED, 0, "跌穿")
    if status == WAITING:
        if close <= support * (1 + REBUY_ZONE):
            days = 1
            if days >= REBUY_STABLE_DAYS:
                return RebuyStep(READY, days, "可买回")
            return RebuyStep(IN_ZONE, days, "进入回补区")
        return RebuyStep(WAITING, 0, None)
    if status == IN_ZONE:
        # 进了回补区之后，只要收盘不破支撑就计数（反弹离开回补区也算站稳）
        days += 1
        if days >= REBUY_STABLE_DAYS:
            return RebuyStep(READY, days, "可买回")
        return RebuyStep(IN_ZONE, days, None)
    return RebuyStep(status, days, None)  # READY：保持，直到买回或跌穿


# ---- 盘中监控（交易时段每 5 分钟） ----
# 上午走势权重低（多是开盘情绪），严重门槛更高；一般情况上午攒到 11:31 收盘后再发
DEEP_BREAK = {"morning": 0.05, "afternoon": 0.03}   # 盘中跌破止损位超过这个比例 = 严重
BIG_DROP = {"morning": 9.0, "afternoon": 7.0}       # 单日跌幅超过这个 = 严重
DROP = 5.0                                           # 跌 5% 以上（未到严重）= 一般


@dataclass
class IntradayEvent:
    kind: str       # 去重用：同一只票同一种情况一天只提醒一次
    severe: bool
    text: str


def intraday_events(price: float, prev_close: float, cum_volume: float, avg5_volume: float | None,
                    support: float, status: str, rebuy: dict | None, overdue_sell: bool,
                    morning: bool) -> list[IntradayEvent]:
    out = []
    session = "morning" if morning else "afternoon"
    change = (price / prev_close - 1) * 100
    if overdue_sell:
        out.append(IntradayEvent("逾期未卖", True, "之前的清仓信号还没执行，按纪律现在就卖。"))
    if status != "待卖出":
        gap = price / support - 1
        if gap <= -DEEP_BREAK[session]:
            out.append(IntradayEvent("深度跌破", True,
                                     f"现价 {price} 跌破止损位 {support} 达 {-gap * 100:.1f}%，收盘大概率站不回来，提前准备。"))
        elif gap < 0:
            out.append(IntradayEvent("盘中跌破", False,
                                     f"现价 {price} 跌破止损位 {support}（{gap * 100:.1f}%），以收盘为准，收盘前可能收回。"))
    if change <= -BIG_DROP[session]:
        out.append(IntradayEvent("大跌", True, f"今天下跌 {-change:.1f}%。"))
    elif change <= -DROP:
        out.append(IntradayEvent("下跌", False, f"今天下跌 {-change:.1f}%。"))
    if rebuy and rebuy.get("status") in ("等待回落", "回补区"):
        rs = rebuy["support"]
        if price < rs:
            out.append(IntradayEvent("跌破回补价", False, f"现价 {price} 跌破回补价 {rs}，收盘站不回就是支撑失效。"))
        elif price <= rs * (1 + REBUY_ZONE) and rebuy["status"] == "等待回落":
            out.append(IntradayEvent("到回补区", False, f"现价 {price} 回落到回补区（{rs} 上方 3% 内），以收盘为准开始计天数。"))
    if morning and avg5_volume and cum_volume >= avg5_volume:
        out.append(IntradayEvent("放量", False, f"上午成交量已经超过 5 日平均的全天量（{cum_volume / avg5_volume:.1f} 倍），有资金异动。"))
    return out
