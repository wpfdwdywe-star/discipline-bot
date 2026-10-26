"""收盘简报的分析：趋势 + 基本面 → 风险信号 → 看法。纯计算，不联网。

风险信号（只提醒、不强制）：
  跌破年线   周五收盘（周线收盘）在 250 日均线下方；平时收在年线下方只算「注意」
  业绩恶化   净利润同比连续两期下滑
  营收萎缩   营收同比负增长
  ROE 下降   ROE 比去年同期下降超过 3 个百分点（同花顺 ROE 是年初至今累计，所以跟去年同一报告期比）
注意信号：扣非净利润同比连续两期下滑、短期走弱（收盘 < MA20 < MA60）、离止损位不到 3%
"""
import datetime as dt
from dataclasses import dataclass, field

import pandas as pd

ROE_DROP = 3.0
NEAR_STOP = 0.03


@dataclass
class Signals:
    risks: list[str] = field(default_factory=list)
    watches: list[str] = field(default_factory=list)
    goods: list[str] = field(default_factory=list)


def pct(x) -> float | None:
    """'14.03%' → 14.03；'--'、False、空 → None。"""
    try:
        return float(str(x).strip().rstrip("%"))
    except ValueError:
        return None


def trend(bars: pd.DataFrame) -> dict:
    c = bars["close"]
    last = float(c.iloc[-1])
    ma = {n: float(c.tail(n).mean()) if len(c) >= n else None for n in (20, 60, 250)}
    prev5 = bars["volume"].iloc[-6:-1].mean()
    return {
        "date": bars["date"].iloc[-1], "close": last,
        "change": (last / float(c.iloc[-2]) - 1) * 100 if len(c) > 1 else 0.0,
        "ma20": ma[20], "ma60": ma[60], "ma250": ma[250],
        "vol_ratio": float(bars["volume"].iloc[-1] / prev5) if prev5 else None,
        "from_high60": (last / float(bars["high"].tail(60).max()) - 1) * 100,
    }


def fundamentals(df: pd.DataFrame) -> dict | None:
    """同花顺财务摘要（按报告期，升序）→ 关键指标。"""
    if df is None or df.empty:
        return None
    df = df.sort_values("报告期").reset_index(drop=True)
    last = df.iloc[-1]
    period = str(last["报告期"])
    ly_period = f"{int(period[:4]) - 1}{period[4:]}"
    ly = df[df["报告期"].astype(str) == ly_period]
    return {
        "period": period,
        "np_yoy": [pct(x) for x in df["净利润同比增长率"].tail(2)],
        "kf_yoy": [pct(x) for x in df["扣非净利润同比增长率"].tail(2)],
        "rev_yoy": pct(last["营业总收入同比增长率"]),
        "roe": pct(last["净资产收益率"]),
        "roe_ly": pct(ly["净资产收益率"].iloc[0]) if not ly.empty else None,
    }


def signals(t: dict, f: dict | None, support: float) -> Signals:
    s = Signals()
    is_friday = dt.date.fromisoformat(t["date"]).weekday() == 4
    if t["ma250"]:
        if t["close"] < t["ma250"]:
            (s.risks if is_friday else s.watches).append(
                "跌破年线" if is_friday else f"收在年线 {t['ma250']:.2f} 下方")
        elif t["ma20"] and t["ma60"] and t["close"] > t["ma20"] > t["ma60"] > t["ma250"]:
            s.goods.append("均线多头排列")
    if t["ma20"] and t["ma60"] and t["close"] < t["ma20"] < t["ma60"]:
        s.watches.append("短期走弱")
    gap = t["close"] / support - 1
    if 0 <= gap < NEAR_STOP:
        s.watches.append(f"离止损位只有 {gap * 100:.1f}%")

    if f:
        np_ = [x for x in f["np_yoy"] if x is not None]
        if len(np_) == 2 and all(x < 0 for x in np_):
            s.risks.append("业绩恶化")
        if f["rev_yoy"] is not None and f["rev_yoy"] < 0:
            s.risks.append("营收萎缩")
        if f["roe"] is not None and f["roe_ly"] is not None and f["roe"] - f["roe_ly"] < -ROE_DROP:
            s.risks.append("ROE 下降")
        kf = [x for x in f["kf_yoy"] if x is not None]
        if len(kf) == 2 and all(x < 0 for x in kf) and "业绩恶化" not in s.risks:
            s.watches.append("扣非净利润连续下滑")
        if np_ and np_[-1] > 15 and (f["rev_yoy"] or 0) > 10:
            s.goods.append("业绩增长良好")
    return s


def opinion(status: str, s: Signals) -> str:
    """看法：纪律状态优先，然后按风险信号多少给结论。"""
    if status == "待卖出":
        return "按纪律今天必须清仓，没有商量。"
    if status == "观察期":
        return "已跌破止损位，明天 14:40 决定去留，到时按信号执行。"
    if len(s.risks) >= 2:
        return f"清仓风险偏高（{'、'.join(s.risks)}）。长期持有的逻辑可能已经变了，建议认真评估还要不要继续拿。"
    if s.risks:
        return f"出现风险信号（{s.risks[0]}），继续观察。如果再出现一项，考虑减仓或离场。"
    if s.watches:
        return f"可以继续持有，但要注意：{'、'.join(s.watches)}。"
    if s.goods:
        return f"趋势和基本面都健康（{'、'.join(s.goods)}），继续持有。"
    return "没有明显风险信号，继续持有。"


def _fmt_pct(x: float | None) -> str:
    return "--" if x is None else f"{x:+.1f}%"


def stock_section(h: dict, t: dict, f: dict | None, s: Signals, new_risks: list[str]) -> str:
    held = f"  持股 {h['shares']}" if h.get("shares") else ""
    lines = [f"【{h['name']} {h['code']}】收盘 {t['close']:.2f}（{t['change']:+.2f}%）  状态：{h['status']}{held}"]
    gap = (t["close"] / h["support"] - 1) * 100
    lines.append(f"  止损位 {h['support']}（距离 {gap:+.1f}%）")
    ma = "  ".join(f"MA{n} {t[f'ma{n}']:.2f}" for n in (20, 60, 250) if t[f"ma{n}"])
    vol = f"  量比 {t['vol_ratio']:.2f}" if t["vol_ratio"] else ""
    lines.append(f"  {ma}{vol}  距60日高点 {t['from_high60']:+.1f}%")
    if f:
        lines.append(f"  财报 {f['period']}：净利润同比 {_fmt_pct(f['np_yoy'][-1])}  扣非 {_fmt_pct(f['kf_yoy'][-1])}  "
                     f"营收 {_fmt_pct(f['rev_yoy'])}  ROE {f['roe'] if f['roe'] is not None else '--'}%"
                     + (f"（去年同期 {f['roe_ly']}%）" if f["roe_ly"] is not None else ""))
    else:
        lines.append("  财报数据暂时拿不到")
    if h.get("rebuy"):
        r = h["rebuy"]
        lines.append(f"  回补计划：回补价 {r['support']}  [{r['status']}]")
    if s.risks:
        lines.append("  ⛔ 风险：" + "、".join(f"{x}{'（新）' if x in new_risks else ''}" for x in s.risks))
    if s.watches:
        lines.append("  ⚠️ 注意：" + "、".join(s.watches))
    lines.append(f"  💬 看法：{opinion(h['status'], s)}")
    return "\n".join(lines)
