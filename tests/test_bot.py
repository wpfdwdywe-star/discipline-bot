import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import app  # noqa: E402
import core  # noqa: E402
import data  # noqa: E402
import rules  # noqa: E402
import store  # noqa: E402


def bars(closes, volumes=None, start="2026-10-01"):
    dates = pd.bdate_range(start, periods=len(closes)).strftime("%Y-%m-%d")
    volumes = volumes or [1000] * len(closes)
    return pd.DataFrame({"date": dates, "open": closes, "high": [c * 1.01 for c in closes],
                         "low": [c * 0.99 for c in closes], "close": closes,
                         "volume": volumes, "amount": [c * v for c, v in zip(closes, volumes)]})


def minutes(prices, volumes=None, last="14:40"):
    volumes = volumes or [100] * len(prices)
    times = [f"2026-10-26 {last}:00"] * len(prices)
    return pd.DataFrame({"time": times, "close": prices, "volume": volumes,
                         "amount": [p * v for p, v in zip(prices, volumes)]})


# ---------- rules ----------

def test_platform_found_with_support_at_lowest_close():
    b = bars([20, 18, 16, 14] + [10, 10.3, 10.1, 10.4, 10.2])
    p = rules.find_platforms(b)[0]
    assert p.days == 5 and p.support == 10.0 and p.level == "小支撑"


def test_platform_levels():
    assert rules.find_platforms(bars([30] + [10] * 12))[0].level == "中支撑"
    assert rules.find_platforms(bars([30] + [10] * 25))[0].level == "大平台"


def test_wide_range_is_not_a_platform():
    assert rules.find_platforms(bars([10, 12, 10, 12, 10, 12])) == []


def test_breakdown_counts_even_on_low_volume():
    bd = rules.check_close(bars([10] * 5 + [9.8], [1000] * 5 + [300]), support=10)
    assert bd.broken and not bd.heavy_volume


def test_heavy_volume_vs_previous_5_day_average():
    bd = rules.check_close(bars([10] * 5 + [9.8], [1000] * 5 + [1500]), support=10)
    assert bd.broken and bd.heavy_volume and bd.volume_ratio == 1.5


def test_close_exactly_at_support_is_not_broken():
    assert not rules.check_close(bars([10] * 6), support=10).broken


def test_recovery_needs_support_and_vwap():
    assert rules.check_recovery(minutes([9.9, 10.1, 10.2]), 10).recovered
    # 站上支撑但尾盘偷拉，低于分时均价 → 不算
    r = rules.check_recovery(minutes([10.5, 10.5, 10.05], [100, 100, 100]), 10)
    assert r.above_support and not r.above_vwap and not r.recovered
    # 均价上方但没站上支撑 → 不算
    assert not rules.check_recovery(minutes([9.5, 9.6, 9.9]), 10).recovered


# ---------- state machine ----------

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DIR", tmp_path)
    monkeypatch.setattr(store, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(store, "EVENTS", tmp_path / "events.jsonl")
    monkeypatch.setattr(store, "SEAL", tmp_path / ".seal")
    pushes = []
    monkeypatch.setattr(core, "push", lambda t, b, level="info": pushes.append((t, level)))
    clock = {"today": "2026-10-23"}
    monkeypatch.setattr(data, "today", lambda: clock["today"])
    cal = ["2026-10-22", "2026-10-23", "2026-10-26", "2026-10-27", "2026-10-28"]
    monkeypatch.setattr(data, "trading_days", lambda: cal)
    feed = {}
    monkeypatch.setattr(data, "daily_bars", lambda code, days=200: feed["daily"])
    monkeypatch.setattr(data, "today_minutes", lambda code: feed["minutes"])
    feed["factor"] = 1.0
    monkeypatch.setattr(data, "exright_factor", lambda code, since: feed["factor"])
    monkeypatch.setattr(data, "sync_clock", lambda: 0.0)
    store.save({"holdings": {"600000": {"code": "600000", "name": "测试", "support": 10.0,
                                         "level": "小支撑", "added": "2026-10-01", "status": store.NORMAL}}})
    return clock, feed, pushes


def daily_on(day, closes, volumes=None):
    b = bars(closes, volumes)
    b.loc[b.index[-1], "date"] = day
    return b


def holding():
    return store.load()[0]["holdings"]["600000"]


def run(cmd):
    {"close": core.check_close, "intraday": core.check_intraday}[cmd]()


def test_break_then_recover(env):
    clock, feed, pushes = env
    feed["daily"] = daily_on("2026-10-23", [10.2] * 5 + [9.8])
    run("close")
    assert holding()["status"] == store.WATCH

    run("intraday")  # 同一天不判断
    assert holding()["status"] == store.WATCH

    clock["today"] = "2026-10-26"
    feed["minutes"] = minutes([9.9, 10.1, 10.2])
    run("intraday")
    assert holding()["status"] == store.NORMAL


def test_break_then_fail_then_nag_and_report_violation(env):
    clock, feed, pushes = env
    feed["daily"] = daily_on("2026-10-23", [10.2] * 5 + [9.8])
    run("close")
    clock["today"] = "2026-10-26"
    feed["minutes"] = minutes([9.7, 9.8, 9.75])
    run("intraday")
    h = holding()
    assert h["status"] == store.SELL and h["sell_date"] == "2026-10-26"
    assert ("🔴 立即清仓 测试(600000)", "critical") in pushes

    run("close")  # 当天收盘还没卖：提醒，但当天不算违规
    assert not [e for e in store.events() if e["kind"] == "违规"]

    clock["today"] = "2026-10-27"
    run("close")  # 过了一天还没卖 → 违规
    assert [e for e in store.events() if e["kind"] == "违规"]

    core.record_sold("600000", 9.5)
    sold = [e for e in store.events() if e["kind"] == "卖出"][0]
    assert sold["delay_days"] == 1 and "600000" not in store.load()[0]["holdings"]


def test_missed_intraday_is_judged_at_close(env):
    clock, feed, pushes = env
    feed["daily"] = daily_on("2026-10-23", [10.2] * 5 + [9.8])
    run("close")
    clock["today"] = "2026-10-26"
    feed["daily"] = daily_on("2026-10-26", [10.2] * 5 + [9.6])
    run("close")  # 14:40 没跑，收盘补判
    assert holding()["status"] == store.SELL


def test_support_cannot_move_down(env):
    with pytest.raises(core.Refused):
        core.raise_support("600000", 9.5)
    assert holding()["support"] == 10.0
    assert [e for e in store.events() if e["kind"] == "违规"]
    core.raise_support("600000", 10.8)
    assert holding()["support"] == 10.8


def test_hand_edited_state_is_flagged(env):
    store.STATE.write_text(store.STATE.read_text().replace("10.0", "8.0"))
    core._load()
    assert [e for e in store.events() if e["kind"] == "违规"]
    assert store.load()[1] is False  # 记录后重新封存


def test_dividend_lowers_support_without_violation(env):
    clock, feed, pushes = env
    feed["factor"] = 0.95  # 分红 5%，价格从 10.2 机械性跳到 9.69
    feed["daily"] = daily_on("2026-10-23", [10.2] * 5 + [9.75])
    run("close")
    h = holding()
    assert h["support"] == 9.5 and h["status"] == store.NORMAL
    assert not [e for e in store.events() if e["kind"] == "违规"]
    feed["factor"] = 1.0


def test_nag_pending_is_critical_and_not_a_violation(env):
    clock, feed, pushes = env
    feed["daily"] = daily_on("2026-10-23", [10.2] * 5 + [9.8])
    run("close")
    clock["today"] = "2026-10-26"
    feed["minutes"] = minutes([9.7, 9.8, 9.75])
    run("intraday")
    pushes.clear()
    assert core.nag_pending() == 1
    assert pushes[0][1] == "critical"
    assert not [e for e in store.events() if e["kind"] == "违规"]


def test_scheduler_windows(env, monkeypatch):
    import datetime as dt
    calls = []
    monkeypatch.setattr(core, "check_intraday", lambda final=True: calls.append("intraday") or 0)
    monkeypatch.setattr(core, "check_close", lambda final=True: calls.append("close") or 0)
    monkeypatch.setattr(core, "nag_pending", lambda: calls.append("nag") or 0)
    monkeypatch.setattr(core, "morning_call", lambda: calls.append("morning"))
    monkeypatch.setattr(core, "_is_month_end", lambda: False)
    monkeypatch.setattr(data, "is_trading_day", lambda d=None: True)
    t = {"now": dt.datetime(2026, 10, 23, 14, 39)}
    monkeypatch.setattr(data, "now", lambda: t["now"])
    s = core.Scheduler()
    s.NAG_EVERY = 0
    s.tick(); s.tick(); assert calls == ["morning"]
    t["now"] = dt.datetime(2026, 10, 23, 14, 52)  # 晚开机也补跑
    s.tick(); s.tick(); assert calls == ["morning", "intraday", "nag"]
    t["now"] = dt.datetime(2026, 10, 23, 14, 58)
    s.tick(); assert calls == ["morning", "intraday", "nag"]  # 14:57 后不再判断
    t["now"] = dt.datetime(2026, 10, 23, 19, 0)  # 晚上开机补跑收盘检查
    s.tick(); s.tick(); assert calls == ["morning", "intraday", "nag", "close"]

    # 重启后不重复跑当天已完成的任务
    s2 = core.Scheduler()
    s2.check_downtime(); s2.tick()
    assert calls == ["morning", "intraday", "nag", "close"]


def test_web_api_flow(env):
    clock, feed, pushes = env
    feed["daily"] = daily_on("2026-10-23", [10.2] * 6)
    c = app.app.test_client()
    assert c.get("/").status_code == 200
    st = c.get("/api/state").get_json()
    assert st["ok"] and st["holdings"][0]["code"] == "600000" and st["auto_trade"] is False
    r = c.post("/api/raise", json={"code": "600000", "price": 9})
    assert r.status_code == 400 and "只能上移" in r.get_json()["error"]
    assert c.post("/api/add", json={"code": "600000", "support": 9}).status_code == 400  # 已持有
    r = c.post("/api/sold", json={"code": "600000", "price": ""})
    assert r.get_json()["ok"]
    assert c.get("/api/state").get_json()["holdings"] == []


def test_missed_checks_window(env):
    import datetime as dt
    d = dt.datetime
    assert core.missed_checks(d(2026, 10, 23, 10), d(2026, 10, 23, 14, 0)) == []
    assert core.missed_checks(d(2026, 10, 23, 15, 0), d(2026, 10, 23, 16, 0)) == ["2026-10-23"]
    assert core.missed_checks(d(2026, 10, 23, 16), d(2026, 10, 26, 9)) == []  # 跨周末，没错过
    assert core.missed_checks(d(2026, 10, 23, 16), d(2026, 10, 27, 20)) == ["2026-10-26", "2026-10-27"]


def test_downtime_manual_stop_is_violation_platform_pause_is_not(env, monkeypatch):
    import datetime as dt
    clock, feed, pushes = env
    t = {"now": dt.datetime(2026, 10, 23, 14, 0)}
    monkeypatch.setattr(data, "now", lambda: t["now"])
    monkeypatch.setattr(data, "is_trading_day", lambda d=None: False)
    kinds = lambda k: [e for e in store.events() if e["kind"] == k]

    core.Scheduler().tick()  # 心跳停在 14:00，之后没有停止信号 = 平台休眠/故障
    t["now"] = dt.datetime(2026, 10, 23, 14, 3)
    core.Scheduler().check_downtime()  # 3 分钟：不算
    assert not kinds("违规") and not kinds("故障")
    t["now"] = dt.datetime(2026, 10, 23, 18, 0)
    core.Scheduler().check_downtime()
    assert not kinds("违规") and len(kinds("故障")) == 1

    t["now"] = dt.datetime(2026, 10, 26, 14, 0)
    core.Scheduler().mark_stopped()  # 收到 SIGTERM = 有人把它关了
    t["now"] = dt.datetime(2026, 10, 26, 18, 0)
    core.Scheduler().check_downtime()
    v = kinds("违规")
    assert len(v) == 1 and "2026-10-26" in v[0]["note"]
    assert ("⚠️ 机器人被关掉，错过了检查", "warn") in pushes


def test_clock_offset_applied(monkeypatch):
    import datetime as dt
    monkeypatch.setattr(data, "_clock_offset", 3600.0)
    assert abs((data.now() - dt.datetime.now(data.TZ)).total_seconds() - 3600) < 2


def test_healthz_needs_no_password(env, monkeypatch, tmp_path):
    import json as _json
    cfg = tmp_path / "config.json"
    cfg.write_text(_json.dumps({"password": "s3cret-pass"}))
    monkeypatch.setattr(app.notify, "CONFIG", cfg)
    ticks = []
    monkeypatch.setattr(app.scheduler, "tick", lambda: ticks.append(1))
    r = app.app.test_client().get("/healthz")
    assert r.status_code == 200 and set(r.get_json()) == {"ok", "time"}
    import time as _t; _t.sleep(0.2)
    assert ticks == [1]


def test_morning_call_lists_holdings(env):
    clock, feed, pushes = env
    core.morning_call()
    assert pushes == [("☀️ 纪律机器人在岗", "info")]


def test_password_required_when_set(env, monkeypatch, tmp_path):
    import base64, json as _json
    cfg = tmp_path / "config.json"
    cfg.write_text(_json.dumps({"password": "s3cret-pass"}))
    monkeypatch.setattr(app.notify, "CONFIG", cfg)
    monkeypatch.setattr(app.time, "sleep", lambda s: None)
    c = app.app.test_client()
    assert c.get("/api/state").status_code == 401
    bad = base64.b64encode(b"u:wrong").decode()
    assert c.get("/api/state", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    good = base64.b64encode(b"u:s3cret-pass").decode()
    assert c.get("/api/state", headers={"Authorization": f"Basic {good}"}).status_code == 200


# ---------- 减仓 & 回补 ----------

def test_rebuy_rule_sequence():
    s, d = rules.WAITING, 0
    out = []
    for close in [12.0, 10.2, 10.1, 10.6]:  # 回补价 10：进区 → 第2天 → 反弹也算站稳 → 可买回
        st = rules.check_rebuy(s, d, close, 10.0)
        s, d = st.status, st.days
        out.append((st.status, st.event))
    assert out == [(rules.WAITING, None), (rules.IN_ZONE, "进入回补区"), (rules.IN_ZONE, None), (rules.READY, "可买回")]
    assert rules.check_rebuy(rules.IN_ZONE, 2, 9.99, 10.0).event == "跌穿"
    assert rules.check_rebuy(rules.WAITING, 0, 10.31, 10.0).status == rules.WAITING  # 3% 外不算进区


def _trim_and_plan(feed, support=10.5):
    feed["daily"] = daily_on("2026-10-23", [11.0] * 5 + [12.0])
    r = core.trim_position("600000", 12.0)
    assert r["sell_price"] == 12.0
    core.set_rebuy_plan("600000", support, 12.0, "小支撑")


def test_trim_keeps_holding_and_rebuy_flow_to_buyback(env):
    clock, feed, pushes = env
    _trim_and_plan(feed)
    assert holding()["status"] == store.NORMAL and holding()["rebuy"]["status"] == rules.WAITING

    days = ["2026-10-23", "2026-10-26", "2026-10-27"]
    for day, close in zip(days, [10.7, 10.6, 10.9]):
        clock["today"] = day
        feed["daily"] = daily_on(day, [11.0] * 5 + [close])
        run("close")
    assert holding()["rebuy"]["status"] == rules.READY
    assert ("🟢 可以买回 测试(600000)", "warn") in pushes

    msg = core.record_rebuy("600000", 10.9)
    h = holding()
    assert "rebuy" not in h and h["support"] == 10.5 and "止损位 10.0 → 10.5" in msg


def test_rebuy_breaks_then_can_only_lower(env):
    clock, feed, pushes = env
    _trim_and_plan(feed)
    feed["daily"] = daily_on("2026-10-23", [11.0] * 5 + [10.4])  # 收盘跌破回补价 10.5
    run("close")
    assert holding()["rebuy"]["status"] == rules.FAILED
    assert any(t.startswith("⛔ 回补价失效") for t, _ in pushes)

    with pytest.raises(core.Refused):
        core.lower_rebuy("600000", 11.0)  # 上调 = 追高
    assert [e for e in store.events() if e["kind"] == "违规"]
    core.lower_rebuy("600000", 10.1)
    assert holding()["rebuy"] == {**holding()["rebuy"], "support": 10.1, "status": rules.WAITING, "days": 0}


def test_rebuy_plan_must_be_below_sell_price_and_trim_blocked_when_stop_triggered(env):
    clock, feed, pushes = env
    feed["daily"] = daily_on("2026-10-23", [11.0] * 5 + [12.0])
    core.trim_position("600000", 12.0)
    with pytest.raises(core.Refused):
        core.set_rebuy_plan("600000", 12.5, 12.0)
    s, _ = store.load()
    s["holdings"]["600000"].update(status=store.SELL, sell_date="2026-10-23", sell_price=9.8)
    store.save(s)
    with pytest.raises(core.Refused, match="不是减仓"):
        core.trim_position("600000", 9.8)


def test_frozen_process_resume_logs_fault(env, monkeypatch):
    import datetime as dt
    clock, feed, pushes = env
    t = {"now": dt.datetime(2026, 10, 23, 14, 0), "wall": 1_000_000.0}
    monkeypatch.setattr(data, "now", lambda: t["now"])
    monkeypatch.setattr(core.time, "time", lambda: t["wall"])
    monkeypatch.setattr(data, "is_trading_day", lambda d=None: False)
    s = core.Scheduler()
    s.tick()
    t["now"], t["wall"] = dt.datetime(2026, 10, 23, 16, 0), t["wall"] + 7200  # 冻结两小时后原样恢复
    s.tick()
    f = [e for e in store.events() if e["kind"] == "故障"]
    assert len(f) == 1 and "2026-10-23" in f[0]["note"]
    assert not [e for e in store.events() if e["kind"] == "违规"]



# ---------- 数据新鲜度 ----------

def test_stale_minutes_deferred_until_final(env, monkeypatch):
    clock, feed, pushes = env
    feed["daily"] = daily_on("2026-10-23", [10.2] * 5 + [9.8])
    run("close")
    clock["today"] = "2026-10-26"
    feed["minutes"] = minutes([9.7, 9.8, 9.75], last="14:30")
    monkeypatch.setattr(data, "minutes_lag", lambda df: 600.0)  # 落后 10 分钟
    pushes.clear()
    assert core.check_intraday(final=False) == 1
    assert holding()["status"] == store.WATCH and pushes == []  # 不判断、不打扰
    assert core.check_intraday(final=True) == 0
    assert holding()["status"] == store.SELL
    assert any("延迟" in e["reason"] for e in store.events() if e["kind"] == "卖出信号")


def test_close_waits_for_todays_bar(env):
    clock, feed, pushes = env
    feed["daily"] = daily_on("2026-10-22", [10.2] * 5 + [9.8])  # 今天 10-23 的日K还没出来
    pushes.clear()
    assert core.check_close(final=False) == 1
    assert holding()["status"] == store.NORMAL and pushes == []
    feed["daily"] = daily_on("2026-10-23", [10.2] * 5 + [9.8])
    assert core.check_close(final=False) == 0
    assert holding()["status"] == store.WATCH


def test_scheduler_retries_until_data_fresh(env, monkeypatch):
    import datetime as dt
    attempts = []
    monkeypatch.setattr(core, "check_intraday", lambda final=True: attempts.append(final) or (0 if final else 1))
    monkeypatch.setattr(core, "nag_pending", lambda: 0)
    monkeypatch.setattr(core, "morning_call", lambda: None)
    monkeypatch.setattr(data, "is_trading_day", lambda d=None: True)
    t = {"now": dt.datetime(2026, 10, 23, 14, 40, 5)}
    monkeypatch.setattr(data, "now", lambda: t["now"])
    s = core.Scheduler()
    s.RETRY = {"intraday": 0}
    s.tick(); s.tick()
    assert attempts == [False, False] and ("2026-10-23", "intraday") not in s.done
    t["now"] = dt.datetime(2026, 10, 23, 14, 52, 0)
    s.tick()
    assert attempts[-1] is True and ("2026-10-23", "intraday") in s.done


def test_minutes_lag(monkeypatch):
    import datetime as dt
    monkeypatch.setattr(data, "now", lambda: dt.datetime(2026, 10, 26, 14, 41, 0, tzinfo=data.TZ))
    assert data.minutes_lag(minutes([10.0], last="14:40")) == 60
    assert data.minutes_lag(pd.DataFrame(columns=["time"])) == float("inf")


# ---------- 收盘简报 ----------
import analysis  # noqa: E402


def fin(rows):
    """rows: [(报告期, 净利润同比, 扣非同比, 营收同比, ROE)]"""
    return pd.DataFrame([{"报告期": p, "净利润同比增长率": f"{a}%", "扣非净利润同比增长率": f"{b}%",
                          "营业总收入同比增长率": f"{c}%", "净资产收益率": f"{r}%"} for p, a, b, c, r in rows])


def test_fundamental_flags():
    f = analysis.fundamentals(fin([("2025-06-30", 5, 5, 3, 12.0), ("2026-03-31", -2, -3, -1, 4.0),
                                   ("2026-06-30", -4, -5, 2, 8.5)]))
    assert f["roe_ly"] == 12.0 and f["np_yoy"] == [-2.0, -4.0]
    t = {"date": "2026-10-22", "close": 10.0, "ma20": None, "ma60": None, "ma250": None}
    s = analysis.signals(t, f, support=8.0)
    assert s.risks == ["业绩恶化", "ROE 下降"]  # 营收最新一期为正，不算萎缩
    assert "清仓风险偏高" in analysis.opinion("持有", s)
    assert analysis.opinion("待卖出", s).startswith("按纪律")


def test_year_line_risk_only_on_friday():
    t = {"date": "2026-10-22", "close": 9.0, "ma20": 9.5, "ma60": 9.8, "ma250": 10.0}  # 周四
    s = analysis.signals(t, None, support=8.0)
    assert not s.risks and any("年线" in w for w in s.watches) and "短期走弱" in s.watches
    t["date"] = "2026-10-23"  # 周五 = 周线收盘
    assert analysis.signals(t, None, support=8.0).risks == ["跌破年线"]


def test_near_stop_watch_and_pct_parsing():
    t = {"date": "2026-10-22", "close": 10.2, "ma20": None, "ma60": None, "ma250": None}
    assert any("止损位" in w for w in analysis.signals(t, None, support=10.0).watches)
    assert analysis.pct("14.03%") == 14.03 and analysis.pct("--") is None and analysis.pct(False) is None


def test_daily_briefing_logs_new_risks_once(env, monkeypatch):
    clock, feed, pushes = env
    closes = [12.0] * 250 + [9.5] * 49 + [10.5]  # 远在年线下方，周五
    feed["daily"] = daily_on("2026-10-23", closes)
    monkeypatch.setattr(data, "financial_abstract", lambda code: fin([("2026-06-30", 10, 10, 5, 10)]))
    monkeypatch.setattr(data, "index_bars", lambda symbol="sh000300": pd.DataFrame(
        {"date": ["2026-10-22", "2026-10-23"], "close": [4000.0, 4040.0]}))
    core.daily_briefing()
    b = core.last_briefing()
    assert b["date"] == "2026-10-23" and "沪深300 4040.00（+1.00%）" in b["text"] and "跌破年线（新）" in b["text"]
    assert pushes[-1] == ("📊 2026-10-23 收盘简报", "warn")
    core.daily_briefing()  # 第二天还在年线下：不重复记
    assert len([e for e in store.events() if e["kind"] == "风险提示"]) == 1
    assert "（新）" not in core.last_briefing()["text"]


def test_briefing_runs_after_close_in_scheduler(env, monkeypatch):
    import datetime as dt
    calls = []
    monkeypatch.setattr(core, "check_close", lambda final=True: calls.append("close") or 0)
    monkeypatch.setattr(core, "daily_briefing", lambda final=True: calls.append("briefing") or 0)
    monkeypatch.setattr(core, "_is_month_end", lambda: False)
    monkeypatch.setattr(core, "morning_call", lambda: None)
    monkeypatch.setattr(data, "is_trading_day", lambda d=None: True)
    monkeypatch.setattr(data, "now", lambda: dt.datetime(2026, 10, 23, 15, 31))
    s = core.Scheduler()
    s.tick(); s.tick()
    assert calls == ["close", "briefing"]



def test_shares_tracked_through_trim_and_rebuy(env):
    clock, feed, pushes = env
    s, _ = store.load(); s["holdings"]["600000"]["shares"] = 800; store.save(s)
    feed["daily"] = daily_on("2026-10-23", [11.0] * 5 + [12.0])
    core.trim_position("600000", 11.5, 400)
    core.trim_position("600000", 11.8, 300)  # 两笔减仓，还没设回补计划
    assert holding()["shares"] == 100
    with pytest.raises(core.Refused, match="清仓"):
        core.trim_position("600000", 11.8, 100)
    core.set_rebuy_plan("600000", 10.5, 11.5)
    core.record_rebuy("600000", 10.6, 300)
    assert holding()["shares"] == 400
    assert [e["qty"] for e in store.events() if e["kind"] == "减仓"] == [400, 300]


# ---------- 盘中监控 ----------

def test_morning_weights_lower_than_afternoon():
    args = dict(prev_close=12.96, cum_volume=0, avg5_volume=None, support=12.42, status="持有",
                rebuy=None, overdue_sell=False)
    am = rules.intraday_events(11.92, morning=True, **args)   # 跌破 4%、跌 8%
    pm = rules.intraday_events(11.92, morning=False, **args)
    assert [e.severe for e in am] == [False, False] and [e.severe for e in pm] == [True, True]
    # 上午也有真正严重的：跌破 5% 以上
    assert rules.intraday_events(11.7, morning=True, **args)[0].severe
    # 逾期未卖跟走势无关，始终严重
    od = rules.intraday_events(13.0, morning=True, **{**args, "overdue_sell": True})
    assert od[0].kind == "逾期未卖" and od[0].severe


def _intraday_env(env, monkeypatch, hhmm, price, prev=10.2):
    import datetime as dt
    clock, feed, pushes = env
    h, m = hhmm
    monkeypatch.setattr(data, "now", lambda: dt.datetime(2026, 10, 23, h, m, tzinfo=data.TZ))
    monkeypatch.setattr(data, "minutes_lag", lambda df: 30.0)
    feed["minutes"] = minutes([price], last=f"{h:02d}:{m:02d}")
    feed["daily"] = daily_on("2026-10-22", [prev] * 6)
    return pushes


def test_morning_minor_events_queued_until_digest(env, monkeypatch):
    pushes = _intraday_env(env, monkeypatch, (10, 5), 9.9)   # 止损 10.0，跌破 1%：一般
    pushes.clear()
    assert core.monitor_intraday() == 1 and pushes == []      # 上午不打扰
    assert core.monitor_intraday() == 0                       # 一天只记一次
    assert core.flush_morning_digest() == 1
    assert pushes[0][0].startswith("📋 上午盘中情况")
    assert core.flush_morning_digest() == 0


def test_severe_events_pushed_immediately_and_afternoon_minor_too(env, monkeypatch):
    pushes = _intraday_env(env, monkeypatch, (10, 5), 9.4)    # 跌破 6%：上午也算严重
    pushes.clear()
    core.monitor_intraday()
    assert ("🔴 盘中紧急 测试(600000)", "critical") in pushes
    pushes = _intraday_env(env, monkeypatch, (13, 30), 9.9, prev=10.0)  # 下午的一般情况直接发
    s, _ = store.load(); s["holdings"]["600000"]["support"] = 10.0; store.save(s)
    import json as _json
    store.DIR.joinpath("intraday.json").unlink()
    pushes.clear()
    core.monitor_intraday()
    assert ("🟡 盘中提醒 测试(600000)", "warn") in pushes


def test_stale_data_never_alerts(env, monkeypatch):
    pushes = _intraday_env(env, monkeypatch, (10, 5), 9.0)
    monkeypatch.setattr(data, "minutes_lag", lambda df: 900.0)
    pushes.clear()
    assert core.monitor_intraday() == 0 and pushes == []


def test_scheduler_monitors_in_session_only_and_sends_digest(env, monkeypatch):
    import datetime as dt
    calls = []
    monkeypatch.setattr(core, "monitor_intraday", lambda: calls.append("monitor") or 0)
    monkeypatch.setattr(core, "flush_morning_digest", lambda: calls.append("digest") or 0)
    monkeypatch.setattr(core, "morning_call", lambda: None)
    monkeypatch.setattr(core, "check_intraday", lambda final=True: 0)
    monkeypatch.setattr(data, "is_trading_day", lambda d=None: True)
    t = {"now": dt.datetime(2026, 10, 23, 9, 25)}
    monkeypatch.setattr(data, "now", lambda: t["now"])
    s = core.Scheduler(); s.MONITOR_EVERY = 0
    s.tick(); assert calls == []                    # 9:25 还没开盘
    t["now"] = dt.datetime(2026, 10, 23, 10, 0); s.tick(); assert calls == ["monitor"]
    t["now"] = dt.datetime(2026, 10, 23, 12, 10); s.tick(); assert calls == ["monitor"]  # 午休
    t["now"] = dt.datetime(2026, 10, 23, 13, 0); s.tick(); assert calls == ["monitor", "monitor", "digest"]
