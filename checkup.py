"""买前体检：这家公司现在值不值得买。纯计算，不联网。

结论三档（"不建议"会直接拦住建仓，所以只留给说得清的硬伤）：
  不建议    任一硬伤，或财务体检几乎全挂（通过率 < 25%）
  谨慎      财务体检一般（< 67%）、估值偏高、或现价下方 10% 内没有平台支撑（买了没有合适的止损位）
  值得考虑  以上都没有

硬伤（一票否决）：ST / 退市股、最近一期亏损、最近两个年报经营现金流都为负、非金融股资产负债率 > 80%、
  前十大股东合计持股 < 51%（股权太分散）、财报或股东数据拿不到
财务体检参考 Piotroski F-Score 的 9 项，用同花顺财务摘要的字段改造，最新一期和去年同一报告期比
（同花顺的累计口径，跨季度比没有意义）；F-Score 只看"有没有改善"，会冤枉高速增长的公司，所以另加营收、
扣非净利润两项增长。金融股没有毛利率、周转率、流动比率，这几项跳过不计分。
"""
from dataclasses import dataclass, field

import pandas as pd

from analysis import pct

PASS_GOOD = 0.67
PASS_MIN = 0.25
MAX_DEBT = 80.0
MIN_TOP10 = 51.0   # 前十大股东合计占总股本
PE_HIGH_PCT = 80.0
PE_LOW_PCT = 20.0
MAX_STOP_GAP = 0.10

VERDICT_GOOD, VERDICT_CAUTION, VERDICT_NO = "值得考虑", "谨慎", "不建议"


def amount(x) -> float | None:
    """'439.45亿' → 4.3945e10；'-5.3万' → -53000；'--' → None。"""
    s = str(x).strip()
    for unit, mul in (("万亿", 1e12), ("亿", 1e8), ("万", 1e4)):
        if s.endswith(unit):
            s, m = s[:-len(unit)], mul
            break
    else:
        m = 1.0
    try:
        return float(s) * m
    except ValueError:
        return None


def num(row, col) -> float | None:
    """百分比和普通数字都能解析；没有这一列返回 None。"""
    return pct(row[col]) if col in row.index else None


@dataclass
class Signal:
    name: str
    passed: bool
    detail: str


@dataclass
class Checkup:
    code: str
    name: str
    verdict: str = VERDICT_GOOD
    vetoes: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    period: str | None = None
    support: float | None = None

    @property
    def score(self) -> str:
        return f"{sum(s.passed for s in self.signals)}/{len(self.signals)}"

    @property
    def pass_rate(self) -> float:
        return sum(s.passed for s in self.signals) / len(self.signals) if self.signals else 0.0


def f_score(df: pd.DataFrame) -> tuple[list[Signal], list[str], str | None]:
    """返回 (体检项, 硬伤, 报告期)。df 是同花顺财务摘要（按报告期）。"""
    df = df.sort_values("报告期").reset_index(drop=True)
    last = df.iloc[-1]
    period = str(last["报告期"])
    ly_rows = df[df["报告期"].astype(str) == f"{int(period[:4]) - 1}{period[4:]}"]
    ly = ly_rows.iloc[0] if not ly_rows.empty else None
    financial = "销售毛利率" not in df.columns or df["销售毛利率"].map(pct).isna().all()

    vetoes, sig = [], []
    np_ = amount(last["净利润"])
    if np_ is not None and np_ < 0:
        vetoes.append(f"最近一期（{period}）亏损：净利润 {last['净利润']}")
    annual = df[df["报告期"].astype(str).str.endswith("12-31")].tail(2)
    cfo_annual = [num(r, "每股经营现金流") for _, r in annual.iterrows()]
    if len(cfo_annual) == 2 and all(x is not None and x < 0 for x in cfo_annual):
        vetoes.append(f"最近两个年报经营现金流都为负（每股 {cfo_annual[0]}、{cfo_annual[1]}）")
    debt = num(last, "资产负债率")
    if not financial and debt is not None and debt > MAX_DEBT:
        vetoes.append(f"资产负债率 {debt:.1f}%，超过 {MAX_DEBT:.0f}%")

    def add(name, ok, detail):
        if ok is not None:
            sig.append(Signal(name, bool(ok), detail))

    def cmp(col, label, higher_better=True):
        a, b = num(last, col), (num(ly, col) if ly is not None else None)
        if a is None or b is None:
            return
        add(label, a > b if higher_better else a < b, f"{b} → {a}（去年同期 → 本期）")

    roe, cfo, eps = num(last, "净资产收益率"), num(last, "每股经营现金流"), num(last, "基本每股收益")
    add("盈利", None if roe is None else roe > 0, f"ROE {roe}%")
    add("经营现金流为正", None if cfo is None else cfo > 0, f"每股经营现金流 {cfo}")
    cmp("净资产收益率", "ROE 改善")
    if cfo is not None and eps is not None:
        add("利润有现金支撑", cfo > eps, f"每股经营现金流 {cfo} vs 每股收益 {eps}")
    rev, kf = num(last, "营业总收入同比增长率"), num(last, "扣非净利润同比增长率")
    add("营收增长", None if rev is None else rev > 0, f"营收同比 {rev}%")
    add("扣非利润增长", None if kf is None else kf > 0, f"扣非净利润同比 {kf}%")
    cmp("资产负债率", "负债率下降", higher_better=False)
    if ly is not None:
        shares = [amount(r["净利润"]) / e if (e := num(r, "基本每股收益")) else None for r in (last, ly)]
        if all(s and s > 0 for s in shares):
            add("没有增发稀释", shares[0] <= shares[1] * 1.02,
                f"折算股本 {shares[1] / 1e8:.2f} 亿 → {shares[0] / 1e8:.2f} 亿")
    if financial:
        cmp("销售净利率", "净利率改善")
    else:
        cmp("流动比率", "流动性改善")
        cmp("销售毛利率", "毛利率改善")
        cmp("存货周转率", "周转加快")
    return sig, vetoes, period


def percentile(series: pd.Series, value: float) -> float:
    s = series.dropna()
    return float((s < value).mean() * 100) if len(s) else 50.0


def check_holders(c: "Checkup", holders: dict | None) -> None:
    if holders is None:
        c.vetoes.append("前十大股东数据拿不到，没法确认股权集中度，稍后再试")
        return
    total = holders["total"]
    top1 = (f"，第一大股东 {holders['top1_name']} {holders['top1_pct']:.2f}%"
            if holders.get("top1_pct") is not None else "")
    c.notes.append(f"前十大股东合计 {total:.1f}%（{holders['date']}）{top1}")
    if total < MIN_TOP10:
        if holders.get("complete", True):
            c.vetoes.append(f"前十大股东合计只有 {total:.1f}%，低于 {MIN_TOP10:.0f}%，股权太分散")
        else:
            c.vetoes.append(f"前十大股东数据不全（能拿到的合计 {total:.1f}%），没法确认达到 {MIN_TOP10:.0f}%，稍后再试")


def evaluate(code: str, name: str, fin: pd.DataFrame | None, bars: pd.DataFrame,
             platforms: list, pe: pd.DataFrame | None, pb: pd.DataFrame | None,
             holders: dict | None) -> Checkup:
    c = Checkup(code, name)
    if "ST" in name.upper() or "退" in name:
        c.vetoes.append(f"{name}：ST 或退市风险股")
    check_holders(c, holders)
    if fin is None or fin.empty:
        c.vetoes.append("财报数据拿不到，没法确认安全，稍后再试")
    else:
        c.signals, vetoes, c.period = f_score(fin)
        c.vetoes += vetoes

    if pe is not None and not pe.empty:
        cur = float(pe["value"].iloc[-1])
        if cur > 0:
            p = percentile(pe["value"][pe["value"] > 0], cur)
            c.notes.append(f"市盈率(TTM) {cur:.1f}，处于近 5 年 {p:.0f}% 分位")
            if p > PE_HIGH_PCT:
                c.cautions.append(f"估值偏高：市盈率处于近 5 年 {p:.0f}% 分位")
            elif p < PE_LOW_PCT:
                c.notes.append("估值处于近 5 年低位")
    if pb is not None and not pb.empty:
        cur = float(pb["value"].iloc[-1])
        c.notes.append(f"市净率 {cur:.2f}，处于近 5 年 {percentile(pb['value'], cur):.0f}% 分位")

    last = float(bars["close"].iloc[-1])
    below = [p for p in platforms if p.support < last]
    if not below:
        c.cautions.append("现价下方没有横盘平台，买了没有合适的止损位")
    else:
        p = max(below, key=lambda p: p.support)
        gap = 1 - p.support / last
        c.support = p.support
        c.notes.append(f"最近的支撑：{p.level} {p.support}（{p.start}~{p.end}），在现价下方 {gap * 100:.1f}%")
        if gap > MAX_STOP_GAP:
            c.cautions.append(f"最近的支撑在现价下方 {gap * 100:.1f}%，止损空间太大")
    if len(bars) >= 250:
        ma250 = float(bars["close"].tail(250).mean())
        c.notes.append(f"在年线 {ma250:.2f} {'上方' if last >= ma250 else '下方'}")

    if c.signals and PASS_MIN <= c.pass_rate < PASS_GOOD:
        c.cautions.insert(0, f"财务体检一般：{c.score} 项通过")
    if c.vetoes or (c.signals and c.pass_rate < PASS_MIN):
        c.verdict = VERDICT_NO
    elif (c.signals and c.pass_rate < PASS_GOOD) or c.cautions:
        c.verdict = VERDICT_CAUTION
    return c


def report(c: Checkup, price: float) -> str:
    icon = {VERDICT_GOOD: "🟢", VERDICT_CAUTION: "🟡", VERDICT_NO: "⛔"}[c.verdict]
    lines = [f"{icon} {c.name}（{c.code}）现价 {price}：{c.verdict}"]
    if c.vetoes:
        lines.append("硬伤（一票否决）：")
        lines += [f"  ⛔ {v}" for v in c.vetoes]
    if c.signals:
        lines.append(f"财务体检 {c.score} 项通过（{c.period} vs 去年同期）：")
        lines += [f"  {'✅' if s.passed else '❌'} {s.name}：{s.detail}" for s in c.signals]
    if c.cautions:
        lines.append("需要谨慎：")
        lines += [f"  ⚠️ {x}" for x in c.cautions]
    if c.notes:
        lines.append("参考：")
        lines += [f"  · {x}" for x in c.notes]
    if c.verdict == VERDICT_NO:
        lines.append("\n结论：不建议买，建仓会被拦住。")
    elif c.verdict == VERDICT_CAUTION:
        lines.append("\n结论：可以买，但上面的问题你要想清楚。")
    else:
        lines.append("\n结论：基本面和位置都过关，可以考虑。买之前想清楚止损位。")
    lines.append("（规则判断，只是提醒，不构成投资建议）")
    return "\n".join(lines)
