"""纪律机器人核心逻辑：持仓管理、定时检查、月报。App 和命令行共用。"""
import datetime as dt
import functools
import json
import threading
import time
import traceback

import analysis
import data
import rules
import store
from notify import push
from store import NORMAL, SELL, WATCH

_lock = threading.RLock()


class Refused(Exception):
    """操作被规则拒绝（消息直接给用户看）。"""


def locked(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        with _lock:
            return fn(*a, **kw)
    return wrapper


def _load() -> dict:
    state, tampered = store.load()
    if tampered:
        store.log("违规", note="state.json 被手动修改")
        push("⚠️ 规则文件被手动改动",
             "检测到 data/state.json 不是通过机器人修改的。\n这次改动已记入违规记录，月报里会显示。", "warn")
        store.save(state)
    return state


def _fmt(h: dict) -> str:
    return f"{h['name']}({h['code']})"


def execute_sell(h: dict, reason: str) -> None:
    """卖出执行。自动下单暂不启用，只做强提醒；开通 QMT/PTrade 后在这里接入。"""
    push(f"🔴 立即清仓 {_fmt(h)}",
         f"{reason}\n支撑位 {h['support']}\n\n按规则必须在 14:57 前清仓，不许再等一天。\n"
         f"卖完在纪律机器人里点「已卖出」。", "critical")


# ---------------- 持仓管理 ----------------

@locked
def holdings() -> list[dict]:
    return list(_load()["holdings"].values())


def candidate_platforms(code: str) -> dict:
    """建仓前：找现价下方的横盘平台供选择。"""
    bars = data.daily_bars(code)
    last = float(bars["close"].iloc[-1])
    # 支撑位在现价上方的平台已经被跌破，是压力位，不能当支撑
    platforms = [p for p in rules.find_platforms(bars) if p.support < last]
    return {"code": code, "name": data.stock_name(code), "price": last, "date": bars["date"].iloc[-1],
            "platforms": [{**p.__dict__, "level": p.level} for p in platforms]}


@locked
def add_holding(code: str, support: float, level: str = "手动", platform: str | None = None,
                shares: int | None = None) -> dict:
    state = _load()
    if code in state["holdings"]:
        raise Refused(f"{code} 已在持仓里。要改支撑位请用「上移支撑」。")
    bars = data.daily_bars(code, days=30)
    last = float(bars["close"].iloc[-1])
    support = round(support, 2)
    if support >= last:
        raise Refused(f"支撑位 {support} 不低于现价 {last}，等于一买就跌破，请重新选。")
    h = {"code": code, "name": data.stock_name(code), "support": support, "level": level,
         "platform": platform, "added": data.today(), "status": NORMAL, "last_price": last}
    if shares:
        h["shares"] = shares
    state["holdings"][code] = h
    store.save(state)
    store.log("建仓", code, name=h["name"], support=support, level=level, price=last, shares=shares)
    return h


@locked
def raise_support(code: str, price: float) -> dict:
    state = _load()
    h = state["holdings"].get(code)
    if not h:
        raise Refused(f"{code} 不在持仓里。")
    if price <= h["support"]:
        store.log("违规", code, note=f"试图把支撑位从 {h['support']} 下移到 {price}")
        push(f"⚠️ 试图下移支撑位 {_fmt(h)}",
             f"你想把支撑位从 {h['support']} 改成 {price}。\n拒绝。止损位只能上移，往下挪是最常见的自我欺骗，已记入违规。", "warn")
        raise Refused(f"拒绝：支撑位只能上移。{h['support']} → {price} 是下移，已记入违规。")
    if h["status"] != NORMAL:
        raise Refused(f"{_fmt(h)} 正处于「{h['status']}」，这时候不能改支撑位。")
    old, h["support"], h["level"] = h["support"], round(price, 2), "手动上移"
    store.save(state)
    store.log("上移支撑", code, old=old, new=h["support"])
    return h


@locked
def record_sold(code: str, price: float | None) -> str:
    state = _load()
    h = state["holdings"].pop(code, None)
    if not h:
        raise Refused(f"{code} 不在持仓里。")
    fields = {"name": h["name"], "price": price, "status_before": h["status"]}
    if h["status"] == SELL:
        fields["delay_days"] = data.trading_days_between(h["sell_date"], data.today())
        fields["signal_price"] = h["sell_price"]
    store.save(state)
    store.log("卖出", code, **fields)
    if fields.get("delay_days"):
        return f"已记录。信号后拖了 {fields['delay_days']} 个交易日才卖，会记进月报。"
    return "已记录。"


# ---------------- 减仓 & 回补 ----------------

def rebuy_candidates(code: str, below: float) -> list[dict]:
    """价格 below 下方的横盘平台，作为回补价候选。"""
    bars = data.daily_bars(code)
    below = min(below, float(bars["close"].iloc[-1]))
    return [{**p.__dict__, "level": p.level} for p in rules.find_platforms(bars) if p.support < below]


@locked
def trim_position(code: str, price: float, qty: int | None = None) -> dict:
    """止盈减仓（不清仓）：记录卖出，返回回补价候选。仓位继续受止损纪律管。"""
    state = _load()
    h = state["holdings"].get(code)
    if not h:
        raise Refused(f"{code} 不在持仓里。")
    if h["status"] == SELL:
        raise Refused(f"{_fmt(h)} 已触发止损清仓，按纪律是全部卖出，不是减仓。")
    if h.get("rebuy") and h["rebuy"]["status"] != rules.FAILED:
        raise Refused(f"{_fmt(h)} 已经有一个回补计划（{h['rebuy']['support']}），先买回或取消再减仓。")
    if qty and h.get("shares"):
        if qty >= h["shares"]:
            raise Refused(f"减仓 {qty} 股不少于持股 {h['shares']} 股，那是清仓，请点「已清仓」。")
        h["shares"] -= qty
        store.save(state)
    store.log("减仓", code, name=h["name"], price=price, qty=qty, shares=h.get("shares"))
    return {"code": code, "name": h["name"], "sell_price": price, "platforms": rebuy_candidates(code, price)}


@locked
def set_rebuy_plan(code: str, support: float, sell_price: float, level: str = "手动",
                   platform: str | None = None) -> dict:
    state = _load()
    h = state["holdings"].get(code)
    if not h:
        raise Refused(f"{code} 不在持仓里。")
    support = round(support, 2)
    if support >= sell_price:
        raise Refused(f"回补价 {support} 不低于卖出价 {sell_price}，那不叫回补，叫追高。")
    old = h.get("rebuy")
    if old and old["status"] != rules.FAILED:
        raise Refused(f"已经有回补计划（{old['support']}）。要改只能「下调回补价」。")
    h["rebuy"] = {"support": support, "sell_price": sell_price, "level": level, "platform": platform,
                  "status": rules.WAITING, "days": 0, "created": data.today()}
    store.save(state)
    store.log("回补计划", code, support=support, sell_price=sell_price, level=level)
    return h


@locked
def lower_rebuy(code: str, price: float) -> dict:
    """回补价只能下调（防追高）；支撑失效后选更低的平台也走这里，会重新开始等待回落。"""
    state = _load()
    h = state["holdings"].get(code)
    plan = h and h.get("rebuy")
    if not plan:
        raise Refused(f"{code} 没有回补计划。")
    if price >= plan["support"]:
        store.log("违规", code, note=f"试图把回补价从 {plan['support']} 上调到 {price}（追高）")
        raise Refused(f"拒绝：回补价只能下调。{plan['support']} → {price} 是上调，已记入违规。")
    old = plan["support"]
    plan.update(support=round(price, 2), status=rules.WAITING, days=0, level="手动下调")
    store.save(state)
    store.log("下调回补价", code, old=old, new=plan["support"])
    return h


@locked
def cancel_rebuy(code: str) -> dict:
    state = _load()
    h = state["holdings"].get(code)
    if not h or not h.get("rebuy"):
        raise Refused(f"{code} 没有回补计划。")
    plan = h.pop("rebuy")
    store.save(state)
    store.log("取消回补", code, support=plan["support"], status=plan["status"])
    return h


@locked
def record_rebuy(code: str, price: float | None, qty: int | None = None) -> str:
    """买回：回补价是新的止损位（支撑只上移，所以取两者较高的）。"""
    state = _load()
    h = state["holdings"].get(code)
    plan = h and h.get("rebuy")
    if not plan:
        raise Refused(f"{code} 没有回补计划。")
    h.pop("rebuy")
    if qty and h.get("shares"):
        h["shares"] += qty
    old = h["support"]
    msg = "已记录买回。"
    if plan["support"] > old and h["status"] == NORMAL:
        h.update(support=plan["support"], level=plan["level"])
        msg += f"止损位 {old} → {plan['support']}，买回的仓位同样受止损纪律管。"
    else:
        msg += f"止损位保持 {old}，买回的仓位同样受止损纪律管。"
    store.save(state)
    store.log("买回", code, price=price, qty=qty, rebuy_support=plan["support"], status_before=plan["status"],
              support=h["support"], shares=h.get("shares"))
    return msg


def _check_rebuy(h: dict, close: float) -> None:
    plan = h.get("rebuy")
    if not plan:
        return
    step = rules.check_rebuy(plan["status"], plan["days"], close, plan["support"])
    plan.update(status=step.status, days=step.days)
    if not step.event:
        return
    store.log(f"回补·{step.event}", h["code"], close=close, support=plan["support"])
    zone_top = round(plan["support"] * (1 + rules.REBUY_ZONE), 2)
    if step.event == "进入回补区":
        push(f"🔵 进入回补区 {_fmt(h)}",
             f"收盘 {close}，回补价 {plan['support']}（回补区 {plan['support']}–{zone_top}）。\n"
             f"开始观察：连续 {rules.REBUY_STABLE_DAYS} 天收盘不破 {plan['support']} 就可以买回。", "warn")
    elif step.event == "可买回":
        push(f"🟢 可以买回 {_fmt(h)}",
             f"连续 {plan['days']} 天收盘站在回补价 {plan['support']} 上方，已企稳。\n"
             f"今天收盘 {close}。买回后在面板点「已买回」，买回的仓位同样受止损纪律管。", "warn")
    elif step.event == "跌穿":
        lower = [p for p in rebuy_candidates(h["code"], plan["support"])][:3]
        alt = "\n".join(f"  · {p['level']} 支撑 {p['support']}（{p['start']}~{p['end']}）" for p in lower) or "  · 暂时没有"
        push(f"⛔ 回补价失效 {_fmt(h)}",
             f"收盘 {close} 跌破回补价 {plan['support']}，支撑失效，别接飞刀。\n"
             f"下方的平台：\n{alt}\n要等更低的位置，在面板点「下调回补价」。", "warn")


# ---------------- 自动检查 ----------------

def _sync_exright(h: dict) -> None:
    """分红送转后支撑位按比例下调。这是唯一允许的下调，自动完成、留记录，不算违规。"""
    ref = h.get("ref_date", h["added"])
    if ref >= data.today():
        return
    factor = data.exright_factor(h["code"], ref)
    if factor < 0.999:
        old = h["support"]
        h["support"] = round(old * factor, 2)
        if h.get("rebuy"):
            h["rebuy"]["support"] = round(h["rebuy"]["support"] * factor, 2)
            h["rebuy"]["sell_price"] = round(h["rebuy"]["sell_price"] * factor, 2)
        store.log("除权调整", h["code"], old=old, new=h["support"], factor=round(factor, 4))
        push(f"ℹ️ 除权调整 {_fmt(h)}", f"分红/送转，支撑位 {old} → {h['support']}（因子 {factor:.4f}）")
    h["ref_date"] = data.today()


def _nag(h: dict, record: bool) -> None:
    days = data.trading_days_between(h["sell_date"], data.today())
    if record and days > 0:
        store.log("违规", h["code"], note=f"卖出信号已过 {days} 个交易日仍未卖出")
    push(f"🔴 还没卖 {_fmt(h)}",
         f"{h['sell_date']} 已触发清仓信号（当时价 {h['sell_price']}），已经过了 {days} 个交易日。\n"
         f"立即卖出，卖完在纪律机器人里点「已卖出」。", "critical")


def _to_sell(h: dict, price: float, reason: str) -> None:
    h.update(status=SELL, sell_date=data.today(), sell_price=price)
    store.log("卖出信号", h["code"], price=price, support=h["support"], reason=reason)
    execute_sell(h, reason)


@locked
def check_close(final: bool = True) -> int:
    """收盘后：跌破支撑位进入观察期；漏跑了 14:40 的观察期按收盘价补判。

    final=False 时，只要有一只票的当天日K还没出来就整体不动、返回待重试的只数（不发任何提醒），
    避免数据没更新就误判成停牌。final=True（最后一次 / 手动触发）时没出来的按停牌处理。
    """
    state = _load()
    today = data.today()
    bars_by_code = {h["code"]: data.daily_bars(h["code"], days=30)
                    for h in state["holdings"].values() if h["status"] != SELL}
    stale = [c for c, b in bars_by_code.items() if b["date"].iloc[-1] != today]
    if stale and not final:
        return len(stale)

    for h in state["holdings"].values():
        if h["status"] == SELL:
            _nag(h, record=True)
            continue
        bars = bars_by_code[h["code"]]
        if h["code"] in stale:
            push(f"ℹ️ {_fmt(h)} 今天没有行情", "到现在还没有今天的日K，可能停牌，明天继续检查。")
            continue
        _sync_exright(h)
        bd = rules.check_close(bars, h["support"])
        h.update(last_price=bd.close, last_check=f"{today} 收盘")
        _check_rebuy(h, bd.close)

        if h["status"] == WATCH and h["watch_date"] < today:
            # 14:40 那次没跑成（电脑关了/断网），用收盘价补判，不让信号悄悄过期
            if bd.broken:
                _to_sell(h, bd.close, f"14:40 检查漏跑，按收盘价补判：收盘 {bd.close} 仍在支撑位下方")
            else:
                h["status"] = NORMAL
                store.log("收回", h["code"], price=bd.close, note="14:40 漏跑，按收盘补判")
            continue

        if h["status"] == NORMAL and bd.broken:
            h.update(status=WATCH, watch_date=today, heavy=bd.heavy_volume)
            ratio = f"{bd.volume_ratio:.2f}" if bd.volume_ratio else "-"
            store.log("跌破", h["code"], close=bd.close, support=h["support"], volume_ratio=ratio, heavy=bd.heavy_volume)
            tag = "🔴 放量跌破" if bd.heavy_volume else "🟠 跌破"
            push(f"{tag} {_fmt(h)}",
                 f"收盘 {bd.close} < 支撑位 {h['support']}（量比5日均量 {ratio}）\n"
                 f"明天是观察日。明天 14:40 必须同时满足：\n"
                 f"  · 现价 ≥ {h['support']}\n  · 现价 ≥ 分时均价\n否则清仓。", "warn")
    store.save(state)
    return 0


@locked
def check_intraday(final: bool = True) -> int:
    """观察日 14:40：判断是否有效收回。

    数据延迟超过 3 分钟（或拿不到）的票：final=False 时先不判断，返回待重试的只数；
    final=True（14:52 最后一次 / 手动触发）时用能拿到的最新数据判断，并在提醒里标明延迟。
    """
    state = _load()
    today = data.today()
    pending = 0
    for h in state["holdings"].values():
        if h["status"] != WATCH or h["watch_date"] >= today:
            continue
        minutes = data.today_minutes(h["code"])
        lag = data.minutes_lag(minutes)
        if lag > data.MAX_LAG and not final:
            pending += 1
            continue
        if minutes.empty:
            push(f"ℹ️ {_fmt(h)} 今天没有分时数据", "可能停牌，仍在观察期，下个交易日继续判断。", "warn")
            continue
        _sync_exright(h)
        r = rules.check_recovery(minutes, h["support"])
        h.update(last_price=r.price, last_check=f"{today} {minutes['time'].iloc[-1][11:16]}")
        detail = (f"现价 {r.price}（{minutes['time'].iloc[-1][11:16]}）  支撑位 {h['support']} "
                  f"{'✅' if r.above_support else '❌'}\n"
                  f"分时均价 {r.vwap} {'✅' if r.above_vwap else '❌'}")
        if lag > data.MAX_LAG:
            detail += f"\n⚠️ 行情数据延迟约 {lag / 60:.0f} 分钟，请在交易软件里核对现价"
        if r.recovered:
            h["status"] = NORMAL
            store.log("收回", h["code"], price=r.price, vwap=r.vwap, lag=round(lag))
            push(f"🟢 有效收回 {_fmt(h)}", detail + "\n解除观察，继续持有。")
        else:
            _to_sell(h, r.price, "观察日没有有效收回\n" + detail)
    store.save(state)
    return pending


@locked
def nag_pending() -> int:
    """14:40–14:57 之间反复强提醒还没卖的票（不记违规）。"""
    pending = [h for h in _load()["holdings"].values() if h["status"] == SELL]
    for h in pending:
        _nag(h, record=False)
    return len(pending)


# ---------------- 盘中监控 ----------------

def _intraday_file():
    return store.DIR / "intraday.json"


def _intraday_state() -> dict:
    f = _intraday_file()
    s = json.loads(f.read_text()) if f.exists() else {}
    if s.get("date") != data.today():
        s = {"date": data.today(), "sent": [], "queue": []}
    return s


@locked
def monitor_intraday() -> int:
    """交易时段每 5 分钟看一次盘。严重的立刻推；一般的上午攒到 13:00、下午直接推。同一情况一天一次。"""
    state = _load()
    seen = _intraday_state()
    today = data.today()
    now = data.now()
    morning = now.time() < dt.time(12, 0)
    count = 0
    for h in state["holdings"].values():
        minutes = data.today_minutes(h["code"])
        if minutes.empty or data.minutes_lag(minutes) > data.MAX_LAG:
            continue  # 数据不新鲜就这轮不看，不拿旧价格误报
        hist = data.daily_bars(h["code"], days=10)
        hist = hist[hist["date"] < today]
        price = float(minutes["close"].iloc[-1])
        h.update(last_price=price, last_check=f"盘中 {minutes['time'].iloc[-1][11:16]}")
        events = rules.intraday_events(
            price, float(hist["close"].iloc[-1]), float(minutes["volume"].sum()),
            float(hist["volume"].tail(5).mean()) if len(hist) >= 5 else None,
            h["support"], h["status"], h.get("rebuy"),
            overdue_sell=h["status"] == SELL and h.get("sell_date", today) < today, morning=morning)
        for e in events:
            key = f"{h['code']}|{e.kind}"
            if key in seen["sent"]:
                continue
            seen["sent"].append(key)
            count += 1
            store.log("盘中提醒", h["code"], note=f"{h['name']}：{e.kind}，{e.text}", severe=e.severe)
            stamp = minutes["time"].iloc[-1][11:16]
            if e.severe:
                push(f"🔴 盘中紧急 {_fmt(h)}", f"{stamp} {e.text}", "critical")
            elif morning:
                seen["queue"].append(f"{stamp} {_fmt(h)}：{e.text}")
            else:
                push(f"🟡 盘中提醒 {_fmt(h)}", f"{stamp} {e.text}", "warn")
    store.save(state)
    _intraday_file().write_text(json.dumps(seen, ensure_ascii=False))
    return count


@locked
def flush_morning_digest() -> int:
    """13:00 把上午攒下的一般情况一次发出去。没有就不发。"""
    seen = _intraday_state()
    queue = seen["queue"]
    if queue:
        push("📋 上午盘中情况（上午走势权重低，仅供参考）",
             "\n".join(queue) + "\n\n正式判断仍以 14:40 和收盘为准。", "info")
    seen["queue"] = []
    _intraday_file().write_text(json.dumps(seen, ensure_ascii=False))
    return len(queue)


# ---------------- 收盘简报 ----------------

def _briefing_file():
    return store.DIR / "briefing.json"


def last_briefing() -> dict | None:
    f = _briefing_file()
    return json.loads(f.read_text()) if f.exists() else None


@locked
def daily_briefing(final: bool = True) -> int:
    """收盘后逐只分析，推送简报。新出现的风险信号记进日志（月报会统计）。"""
    state = _load()
    today = data.today()
    # 标题用行情数据的日期（盘前手动生成时是上一个交易日的收盘）
    day = today
    lines = []
    try:
        idx = data.index_bars()
        c0, c1 = float(idx["close"].iloc[-2]), float(idx["close"].iloc[-1])
        day = str(idx["date"].iloc[-1]) if "date" in idx else today
        lines.append(f"沪深300 {c1:.2f}（{(c1 / c0 - 1) * 100:+.2f}%）")
    except Exception:
        pass
    lines.insert(0, f"📊 {day} 收盘简报")
    sections, summary = [], {"risk": [], "watch": []}
    for h in state["holdings"].values():
        try:
            bars = data.daily_bars(h["code"], days=300)
        except Exception:
            sections.append(f"【{_fmt(h)}】行情拿不到，今天跳过")
            continue
        t = analysis.trend(bars)
        f = analysis.fundamentals(data.financial_abstract(h["code"]))
        sig = analysis.signals(t, f, h["support"])
        new = [r for r in sig.risks if r not in h.get("risk_flags", [])]
        for r in new:
            store.log("风险提示", h["code"], note=f"{h['name']}：{r}")
        h["risk_flags"] = sig.risks
        sections.append(analysis.stock_section(h, t, f, sig, new))
        if sig.risks or h["status"] in (WATCH, SELL):
            summary["risk"].append(h["name"])
        elif sig.watches:
            summary["watch"].append(h["name"])
    if not state["holdings"]:
        sections.append("暂无持仓。")
    n = len(state["holdings"])
    head = f"持仓 {n} 只"
    if summary["risk"]:
        head += f"，⛔ 需要重点关注：{'、'.join(summary['risk'])}"
    if summary["watch"]:
        head += f"，⚠️ 注意：{'、'.join(summary['watch'])}"
    if n and not summary["risk"] and not summary["watch"]:
        head += "，全部正常"
    lines += [head, ""] + ["\n\n".join(sections)]
    lines.append("\n（看法基于均线和财报指标的规则判断，只是提醒，不构成投资建议；止损信号除外，那是纪律。）")
    text = "\n".join(lines)
    store.save(state)
    _briefing_file().write_text(json.dumps({"date": day, "time": f"{data.now():%m-%d %H:%M}", "text": text},
                                           ensure_ascii=False))
    push(f"📊 {day} 收盘简报", text, "warn" if summary["risk"] else "info")
    return 0


# ---------------- 月报 ----------------

def build_report(month: str | None = None) -> str:
    month = month or data.today()[:7]
    evs = store.events(month)
    signals = [e for e in evs if e["kind"] == "卖出信号"]
    all_sold = [e for e in store.events() if e["kind"] == "卖出"]
    violations = [e for e in evs if e["kind"] == "违规"]
    risks = [e for e in evs if e["kind"] == "风险提示"]

    lines = [f"📋 {month} 纪律报告", ""]
    lines.append(f"跌破 {sum(e['kind'] == '跌破' for e in evs)} 次，"
                 f"有效收回 {sum(e['kind'] == '收回' for e in evs)} 次，清仓信号 {len(signals)} 次")
    on_time = 0
    for s in signals:
        sold = next((e for e in all_sold if e["code"] == s["code"] and e["time"] >= s["time"]), None)
        try:
            now_price = float(data.daily_bars(s["code"], days=5)["close"].iloc[-1])
            after = f"现价 {now_price}（较信号价 {(now_price / s['price'] - 1) * 100:+.1f}%）"
        except Exception:
            after = ""
        if sold is None:
            lines.append(f"  ❌ {s['code']} {s['time'][:10]} 信号价 {s['price']}  至今未卖  {after}")
        elif sold.get("delay_days", 0) == 0:
            on_time += 1
            lines.append(f"  ✅ {s['code']} {s['time'][:10]} 信号价 {s['price']}  当天卖出 {sold['price'] or ''}  {after}")
        else:
            lines.append(f"  ⚠️ {s['code']} {s['time'][:10]} 信号价 {s['price']}  拖了{sold['delay_days']}天才卖 {sold['price'] or ''}  {after}")
    if signals:
        lines.append(f"\n按时执行率：{on_time}/{len(signals)}")
    if risks:
        lines.append(f"\n风险提示 {len(risks)} 条：")
        lines += [f"  · {e['time'][:10]} {e['note']}" for e in risks]
    lines.append(f"\n违规记录 {len(violations)} 条：")
    lines += [f"  · {e['time'][:10]} {e['code']} {e['note']}" for e in violations] or ["  无，保持住。"]
    return "\n".join(lines)


def _is_month_end() -> bool:
    nxt = next((d for d in data.trading_days() if d > data.today()), None)
    return nxt is not None and nxt[:7] != data.today()[:7]


@locked
def morning_call() -> None:
    """交易日早上报平安：没收到这条，说明机器人被关了。"""
    hs = list(_load()["holdings"].values())
    lines = [f"今天在岗，监控 {len(hs)} 只股票。"]
    for h in hs:
        note = {WATCH: "今天是观察日，14:40 判断是否有效收回",
                SELL: f"{h.get('sell_date')} 已触发清仓，还没卖！"}.get(h["status"], "")
        lines.append(f"  · {_fmt(h)} 支撑 {h['support']} [{h['status']}] {note}")
    push("☀️ 纪律机器人在岗", "\n".join(lines))


def missed_checks(last: dt.datetime, now: dt.datetime) -> list[str]:
    """停机期间 (last, now) 覆盖了哪些交易日的 14:40–15:30 检查时段。"""
    days = set(data.trading_days())
    out = []
    d = last.date()
    while d <= now.date():
        start = dt.datetime.combine(d, dt.time(14, 40), tzinfo=last.tzinfo)
        end = dt.datetime.combine(d, dt.time(15, 30), tzinfo=last.tzinfo)
        if str(d) in days and last < end and now > start:
            out.append(str(d))
        d += dt.timedelta(days=1)
    return out


# ---------------- 定时 ----------------

class Scheduler:
    """北京时间（交易日）：9:15 报平安，交易时段每 5 分钟盘中监控（13:00 发上午汇总），
    14:40 观察日检查，14:40–14:57 每 3 分钟催卖，15:30 收盘检查 → 收盘简报。

    晚开/重启：报平安在 14:40 前补发；14:40 检查在 14:57 前补跑；收盘检查当天 15:30 之后任何时候补跑。
    停机覆盖了 14:40–15:30 检查时段 → 重新上线时：人为停止记违规，平台休眠/故障记「故障」。
    """

    NAG_EVERY = 180
    GRACE = dt.timedelta(minutes=5)  # 心跳间隔 20 秒，短于这个的空档不算停机
    CLOCK_SYNC_EVERY = 600
    MONITOR_EVERY = 300

    def __init__(self):
        self.done: set = set()
        self.last_nag = 0.0
        self.last_sync = 0.0
        self.last_wall = 0.0
        self.log: list[str] = []
        self.thread: threading.Thread | None = None
        self._tick_lock = threading.Lock()
        self.last_try: dict[str, float] = {}
        self.last_monitor = 0.0

    @property
    def _file(self):
        return store.DIR / "scheduler.json"

    def _note(self, msg: str) -> None:
        self.log = ([f"{data.now():%m-%d %H:%M:%S} {msg}"] + self.log)[:30]

    def _save(self, stopped: bool = False) -> None:
        today = data.today()
        store.DIR.mkdir(parents=True, exist_ok=True)
        state = {"heartbeat": data.now().isoformat(),
                 "done": sorted(k for d, k in self.done if d == today), "date": today}
        if stopped:
            state["stopped"] = "manual"  # 收到 SIGTERM：有人执行了停止（systemctl stop 等）
        self._file.write_text(json.dumps(state))

    def mark_stopped(self) -> None:
        self._save(stopped=True)

    def check_downtime(self) -> None:
        """启动时：读上次心跳，看停机期间有没有错过检查；恢复当天已完成的任务。"""
        if not self._file.exists():
            return
        saved = json.loads(self._file.read_text())
        if saved.get("date") == data.today():
            self.done |= {(saved["date"], k) for k in saved.get("done", [])}
        last, now = dt.datetime.fromisoformat(saved["heartbeat"]), data.now()
        if now - last < self.GRACE:
            return
        missed = missed_checks(last, now)
        hours = (now - last).total_seconds() / 3600
        if missed and saved.get("stopped") == "manual":
            store.log("违规", note=f"机器人被人为停止 {hours:.1f} 小时，错过了 {'、'.join(missed)} 的检查")
            push("⚠️ 机器人被关掉，错过了检查",
                 f"上次在线 {last:%m-%d %H:%M}，被停止了 {hours:.1f} 小时。\n"
                 f"错过了 {'、'.join(missed)} 的 14:40/收盘检查，已记违规。", "warn")
        elif missed:
            # 没收到停止信号就断了：平台休眠没被唤醒、机器故障等，不算你违规，但要让你知道
            store.log("故障", note=f"机器人离线 {hours:.1f} 小时（非人为停止），错过了 {'、'.join(missed)} 的检查")
            push("⚠️ 机器人离线，错过了检查",
                 f"上次在线 {last:%m-%d %H:%M}，离线 {hours:.1f} 小时，错过了 {'、'.join(missed)} 的检查。\n"
                 f"看起来是服务器休眠没被唤醒或故障，不记违规。请检查定时唤醒是否正常。", "warn")
        self._note(f"重新上线，离线 {hours:.1f} 小时")

    def _frozen(self, last: dt.datetime, now: dt.datetime) -> None:
        """进程被冻结后恢复（平台休眠没被及时唤醒）：错过了检查时段就记故障。"""
        hours = (now - last).total_seconds() / 3600
        missed = missed_checks(last, now)
        if missed:
            store.log("故障", note=f"机器人休眠 {hours:.1f} 小时，错过了 {'、'.join(missed)} 的检查")
            push("⚠️ 机器人休眠，错过了检查",
                 f"{last:%m-%d %H:%M} 到 {now:%m-%d %H:%M} 机器在休眠，错过了 {'、'.join(missed)} 的检查。\n"
                 f"醒来后已补做能补的检查。请检查定时唤醒（cron-job.org）是否正常。", "warn")
        self._note(f"从休眠恢复，睡了 {hours:.1f} 小时")

    INTRADAY_FINAL = dt.time(14, 52)   # 这之后不再等数据，用最新能拿到的判断（还留 5 分钟下单）
    CLOSE_FINAL = dt.time(17, 0)       # 这之后还没有当天日K，才按停牌处理
    RETRY = {"intraday": 60, "close": 300, "morning": 300, "briefing": 300, "digest": 60}

    def _run(self, key, job, label: str, final: bool = True) -> None:
        """跑一次任务。没完成（数据没更新 / 出错）且不是最后一次 → 不标记完成，过一会儿重试。"""
        today = data.today()
        if time.time() - self.last_try.get(key, 0) < self.RETRY.get(key, 60):
            return
        self.last_try[key] = time.time()
        try:
            pending = job(final=final) if key in ("intraday", "close", "briefing") else job()
        except Exception:
            if final:
                self.done.add((today, key))
                push("⚠️ 纪律机器人出错", f"{label}失败，请检查网络。\n" + traceback.format_exc()[-800:], "warn")
            self._note(f"{label} 出错" + ("" if final else "，稍后重试"))
            return
        if pending and not final:
            self._note(f"{label}：{pending} 只数据还没更新，稍后重试")
            return
        self.done.add((today, key))
        self._note(f"{label} 完成")

    def tick(self) -> None:
        # 网址唤醒时会从网页请求里调用，和后台循环可能同时到达；同一时间只跑一个
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            self._tick()
        finally:
            self._tick_lock.release()

    def _tick(self) -> None:
        # 休眠恢复后（墙上时间跳了一大截）或每 10 分钟，用网络时间校正时钟
        wall = time.time()
        if wall - self.last_wall > 90 or wall - self.last_sync > self.CLOCK_SYNC_EVERY:
            offset = data.sync_clock()
            self.last_sync = wall
            if offset:
                self._note(f"系统时钟偏差 {offset:+.0f} 秒，已按网络时间校正")
        now = data.now()
        if self.last_wall and wall - self.last_wall > self.GRACE.total_seconds():
            self._frozen(now - dt.timedelta(seconds=wall - self.last_wall), now)
        self.last_wall = wall
        t = now.time()
        if data.is_trading_day():
            today = data.today()
            if dt.time(9, 15) <= t < dt.time(14, 40) and (today, "morning") not in self.done:
                self._run("morning", morning_call, "报平安")
            in_session = dt.time(9, 30) <= t < dt.time(11, 30) or dt.time(13, 0) <= t < dt.time(15, 0)
            if in_session and time.time() - self.last_monitor >= self.MONITOR_EVERY:
                self.last_monitor = time.time()
                try:
                    if n := monitor_intraday():
                        self._note(f"盘中监控：{n} 条新情况")
                except Exception:
                    self._note("盘中监控出错：" + traceback.format_exc().splitlines()[-1])
            if t >= dt.time(13, 0) and (today, "digest") not in self.done:
                self._run("digest", flush_morning_digest, "上午汇总")
            if dt.time(14, 40) <= t < dt.time(14, 57):
                if (today, "intraday") not in self.done:
                    self._run("intraday", check_intraday, "14:40 观察日检查", final=t >= self.INTRADAY_FINAL)
                    self.last_nag = time.time()  # 刚发过清仓信号，不紧接着再催
                elif time.time() - self.last_nag >= self.NAG_EVERY:
                    self.last_nag = time.time()
                    if nag_pending():
                        self._note("催卖提醒")
            if t >= dt.time(15, 30) and (today, "close") not in self.done:
                self._run("close", check_close, "收盘检查", final=t >= self.CLOSE_FINAL)
                if (today, "close") in self.done and _is_month_end():
                    push(f"{today[:7]} 纪律报告", build_report(), "warn")
            if (today, "close") in self.done and (today, "briefing") not in self.done:
                self._run("briefing", daily_briefing, "收盘简报", final=t >= self.CLOSE_FINAL)
        self._save()

    def loop(self) -> None:
        self._note("已启动")
        try:
            self.check_downtime()
        except Exception:
            self._note("停机检查出错：" + traceback.format_exc().splitlines()[-1])
        while True:
            try:
                self.tick()
            except Exception:
                self._note("调度出错：" + traceback.format_exc().splitlines()[-1])
            time.sleep(20)

    def start(self) -> None:
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
