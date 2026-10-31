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
    monkeypatch.setattr(store, "ROOT", tmp_path)
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
    monkeypatch.setattr(data, "clock_skew", lambda: 0.0)
    monkeypatch.setattr(data, "_clock_offset", 0.0)
    monkeypatch.setattr(data, "top10_holders", lambda code: {"date": "2026-06-30", "total": 60.0,
                                                             "top1_name": "某集团", "top1_pct": 40.0, "complete": True})
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
    (store.dir() / "state.json").write_text((store.dir() / "state.json").read_text().replace("10.0", "8.0"))
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
    store.dir().joinpath("intraday.json").unlink()
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
    t["now"] = dt.datetime(2026, 10, 23, 12, 10); s.tick(); assert calls == ["monitor", "monitor", "digest"]  # 午休：补看一轮后发上午汇总
    t["now"] = dt.datetime(2026, 10, 23, 12, 40); s.tick(); assert calls == ["monitor", "monitor", "digest"]  # 汇总只发一次
    t["now"] = dt.datetime(2026, 10, 23, 13, 0); s.tick(); assert calls == ["monitor", "monitor", "digest", "monitor"]


# ---------- 收盘后日K未更新：用分钟线补今天 ----------

def _raw_minutes(day, last="15:00"):
    return pd.DataFrame({"day": [f"{day} 09:31:00", f"{day} 14:00:00", f"{day} {last}:00"],
                         "open": ["10.0", "10.4", "10.2"], "high": ["10.5", "10.6", "10.2"],
                         "low": ["9.9", "10.3", "10.2"], "close": ["10.1", "10.5", "10.2"],
                         "volume": ["100", "200", "300"], "amount": ["1010", "2100", "3060"]})


def test_today_bar_synthesized_after_close(monkeypatch):
    import datetime as dt
    monkeypatch.setattr(data, "today", lambda: "2026-10-26")
    monkeypatch.setattr(data, "now", lambda: dt.datetime(2026, 10, 26, 15, 31, tzinfo=data.TZ))
    monkeypatch.setattr(data.ak, "stock_zh_a_minute", lambda **kw: _raw_minutes("2026-10-26"))
    old = daily_on("2026-10-23", [9.0] * 6)
    df = data._with_today_bar(old, "sh600000")
    assert df["date"].iloc[-1] == "2026-10-26"
    assert df.iloc[-1][["open", "high", "low", "close", "volume"]].tolist() == [10.0, 10.6, 9.9, 10.2, 600.0]


def test_no_synthetic_bar_during_session_or_incomplete_day(monkeypatch):
    import datetime as dt
    monkeypatch.setattr(data, "today", lambda: "2026-10-26")
    monkeypatch.setattr(data.ak, "stock_zh_a_minute", lambda **kw: _raw_minutes("2026-10-26", last="14:30"))
    old = daily_on("2026-10-23", [9.0] * 6)
    monkeypatch.setattr(data, "now", lambda: dt.datetime(2026, 10, 26, 14, 30, tzinfo=data.TZ))
    assert len(data._with_today_bar(old, "sh600000")) == len(old)      # 盘中不合成
    monkeypatch.setattr(data, "now", lambda: dt.datetime(2026, 10, 26, 15, 31, tzinfo=data.TZ))
    assert len(data._with_today_bar(old, "sh600000")) == len(old)      # 分钟线不到 15:00 也不合成


def test_briefing_flags_stale_index_instead_of_mislabeling(env, monkeypatch):
    clock, feed, pushes = env
    clock["today"] = "2026-10-26"
    feed["daily"] = daily_on("2026-10-26", [10.5] * 300)
    monkeypatch.setattr(data, "financial_abstract", lambda code: None)
    monkeypatch.setattr(data, "index_bars", lambda symbol="sh000300": pd.DataFrame(
        {"date": ["2026-10-22", "2026-10-23"], "close": [4000.0, 4040.0]}))
    core.daily_briefing()
    text = core.last_briefing()["text"]
    assert text.startswith("📊 2026-10-26 收盘简报")
    assert "这是 2026-10-23 的数据，2026-10-26 的还没更新" in text


def test_requests_without_timeout_do_not_hang_forever(monkeypatch):
    # 10-27：同花顺接口接受连接却不回数据，akshare 不带超时的请求把调度线程卡死
    import socket
    import time
    import requests
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    monkeypatch.setattr(data, "REQUEST_TIMEOUT", 1)
    t0 = time.time()
    with pytest.raises(requests.exceptions.Timeout):
        requests.get(f"http://127.0.0.1:{srv.getsockname()[1]}/")
    assert time.time() - t0 < 5
    srv.close()


# ---------- 买前体检 ----------
import checkup  # noqa: E402


def fin_full(last, ly, name_period=("2026-06-30", "2025-06-30"), annual_cfo=(1.0, 1.0)):
    """last / ly: 字段字典（不带 % 的数值），生成同花顺格式的财务摘要。"""
    base = {"净利润": "10亿", "净利润同比增长率": "10%", "扣非净利润同比增长率": "10%", "营业总收入同比增长率": "10%",
            "基本每股收益": "1.0", "每股经营现金流": "1.5", "净资产收益率": "10%", "资产负债率": "50%",
            "流动比率": "1.5", "销售毛利率": "30%", "存货周转率": "3.0", "销售净利率": "10%"}
    rows = [{**base, "报告期": "2024-12-31", "每股经营现金流": str(annual_cfo[0])},
            {**base, "报告期": "2025-12-31", "每股经营现金流": str(annual_cfo[1])}]
    rows.append({**base, **ly, "报告期": name_period[1]})
    rows.append({**base, **last, "报告期": name_period[0]})
    return pd.DataFrame(rows).sort_values("报告期")


HOLDERS_OK = {"date": "2026-06-30", "total": 60.0, "top1_name": "某集团", "top1_pct": 40.0, "complete": True}


def flat_bars(n=300, price=10.0):
    return bars([price] * n)


def plat(support):
    return rules.Platform("2026-09-01", "2026-09-20", 14, support, support * 1.08, support * 0.99)


def test_amount_parsing():
    assert checkup.amount("439.45亿") == 439.45e8 and checkup.amount("-867.31万") == -8673100
    assert checkup.amount("--") is None


def test_healthy_company_worth_considering():
    good = {"净资产收益率": "12%", "资产负债率": "45%", "流动比率": "1.6", "销售毛利率": "32%", "存货周转率": "3.2"}
    c = checkup.evaluate("600000", "好公司", fin_full(good, {}), flat_bars(), [plat(9.6)], None, None, HOLDERS_OK)
    assert c.verdict == checkup.VERDICT_GOOD and not c.vetoes and c.pass_rate >= 0.67


def test_hard_flaws_block():
    loss = checkup.evaluate("600000", "亏损股", fin_full({"净利润": "-5.37亿"}, {}), flat_bars(), [plat(9.6)], None, None, HOLDERS_OK)
    assert loss.verdict == checkup.VERDICT_NO and "亏损" in loss.vetoes[0]
    st = checkup.evaluate("600000", "*ST某某", fin_full({}, {}), flat_bars(), [plat(9.6)], None, None, HOLDERS_OK)
    assert st.verdict == checkup.VERDICT_NO
    debt = checkup.evaluate("600000", "高负债", fin_full({"资产负债率": "85%"}, {}), flat_bars(), [plat(9.6)], None, None, HOLDERS_OK)
    assert any("资产负债率" in v for v in debt.vetoes)
    cash = checkup.evaluate("600000", "烧钱", fin_full({}, {}, annual_cfo=(-0.5, -0.2)), flat_bars(), [plat(9.6)], None, None, HOLDERS_OK)
    assert any("经营现金流" in v for v in cash.vetoes)
    nodata = checkup.evaluate("600000", "某某", None, flat_bars(), [plat(9.6)], None, None, HOLDERS_OK)
    assert nodata.verdict == checkup.VERDICT_NO


def test_bank_skips_industrial_ratios_and_high_debt_not_vetoed():
    f = fin_full({"资产负债率": "92%"}, {}).drop(columns=["销售毛利率", "存货周转率", "流动比率"])
    c = checkup.evaluate("601398", "某银行", f, flat_bars(), [plat(9.6)], None, None, HOLDERS_OK)
    names = [s.name for s in c.signals]
    assert not c.vetoes and "毛利率改善" not in names and "净利率改善" in names


def test_growth_company_not_hard_blocked_by_fscore_alone():
    # 高增长但毛利率、周转、负债都变差（宁德时代那种）：谨慎，不硬拦
    worse = {"资产负债率": "60%", "流动比率": "1.2", "销售毛利率": "25%", "存货周转率": "2.5", "净资产收益率": "9%",
             "每股经营现金流": "0.9", "营业总收入同比增长率": "56%", "扣非净利润同比增长率": "32%"}
    c = checkup.evaluate("300750", "增长股", fin_full(worse, {}), flat_bars(), [plat(9.6)], None, None, HOLDERS_OK)
    assert c.verdict == checkup.VERDICT_CAUTION and c.cautions[0].startswith("财务体检一般")


def test_valuation_and_stop_room_cautions():
    pe = pd.DataFrame({"date": [str(i) for i in range(100)], "value": [10.0 + i * 0.1 for i in range(100)]})
    c = checkup.evaluate("600000", "贵", fin_full({}, {}), flat_bars(), [plat(9.6)], pe, None, HOLDERS_OK)
    assert any("估值偏高" in x for x in c.cautions)
    far = checkup.evaluate("600000", "远", fin_full({}, {}), flat_bars(), [plat(8.0)], None, None, HOLDERS_OK)
    assert any("止损空间太大" in x for x in far.cautions)
    none = checkup.evaluate("600000", "无", fin_full({}, {}), flat_bars(), [], None, None, HOLDERS_OK)
    assert any("没有横盘平台" in x for x in none.cautions)


def test_add_holding_blocked_when_not_recommended_unless_existing(env, monkeypatch):
    clock, feed, pushes = env
    feed["daily"] = daily_on("2026-10-23", [10.2] * 300)
    monkeypatch.setattr(data, "stock_name", lambda code: "亏损股")
    monkeypatch.setattr(data, "financial_abstract", lambda code: fin_full({"净利润": "-1亿"}, {}))
    monkeypatch.setattr(data, "valuation", lambda code, ind: None)
    with pytest.raises(core.Refused, match="拦截"):
        core.add_holding("600111", 9.8)
    assert "600111" not in store.load()[0]["holdings"]
    assert [e for e in store.events() if e["kind"] == "拦截建仓"]
    core.add_holding("600111", 9.8, existing=True)  # 补录已有持仓：不拦，但记录
    h = store.load()[0]["holdings"]["600111"]
    assert h["checkup"]["verdict"] == "不建议"
    assert [e for e in store.events() if e["kind"] == "补录不建议"]


def test_checkup_cached_per_day(env, monkeypatch):
    clock, feed, pushes = env
    feed["daily"] = daily_on("2026-10-23", [10.2] * 300)
    calls = []
    monkeypatch.setattr(data, "stock_name", lambda code: "好公司")
    monkeypatch.setattr(data, "financial_abstract", lambda code: calls.append(1) or fin_full({}, {}))
    monkeypatch.setattr(data, "valuation", lambda code, ind: None)
    core.pre_buy_checkup("600222"); core.pre_buy_checkup("600222")
    assert len(calls) == 1
    clock["today"] = "2026-10-26"
    core.pre_buy_checkup("600222")
    assert len(calls) == 2



def test_top10_concentration_rule():
    f = fin_full({}, {})
    ok = checkup.evaluate("600000", "集中", f, flat_bars(), [plat(9.6)], None, None, HOLDERS_OK)
    assert not ok.vetoes and any("前十大股东合计 60.0%" in n for n in ok.notes)
    low = {**HOLDERS_OK, "total": 48.0}
    c = checkup.evaluate("600615", "分散", f, flat_bars(), [plat(9.6)], None, None, low)
    assert c.verdict == checkup.VERDICT_NO and "股权太分散" in c.vetoes[0]
    edge = checkup.evaluate("600000", "刚好", f, flat_bars(), [plat(9.6)], None, None, {**HOLDERS_OK, "total": 51.0})
    assert not edge.vetoes
    gone = checkup.evaluate("600000", "没数据", f, flat_bars(), [plat(9.6)], None, None, None)
    assert gone.verdict == checkup.VERDICT_NO and "拿不到" in gone.vetoes[0]
    partial = checkup.evaluate("600000", "不全", f, flat_bars(), [plat(9.6)], None, None,
                               {**HOLDERS_OK, "total": 45.0, "complete": False})
    assert "数据不全" in partial.vetoes[0]
    partial_ok = checkup.evaluate("600000", "不全但够", f, flat_bars(), [plat(9.6)], None, None,
                                  {**HOLDERS_OK, "total": 66.0, "complete": False})
    assert not partial_ok.vetoes  # 能拿到的部分已经超过 51%，缺的只会更多


# ---------- 分控台 ----------
import base64  # noqa: E402

import tenants  # noqa: E402


@pytest.fixture
def multi(env, monkeypatch, tmp_path):
    """主控台密码 main-pass，外加一个分控台。返回 (client, friend, 登录头函数)。"""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"password": "main-pass", "pushplus_token": "MAIN-TOKEN"}))
    monkeypatch.setattr(app.notify, "CONFIG", cfg)
    monkeypatch.setattr(app.time, "sleep", lambda s: None)
    monkeypatch.setattr(app, "guard", app.RateGuard())
    friend = tenants.create("老王")

    def auth(user, pw):
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}
    return app.app.test_client(), friend, auth


import json  # noqa: E402


def test_friend_data_is_isolated(multi, env):
    c, friend, auth = multi
    clock, feed, pushes = env
    fa = auth(friend["id"], friend["password"])
    st = c.get("/api/state", headers=fa).get_json()
    assert st["role"] == "friend" and st["account"] == "老王" and st["holdings"] == []  # 看不到主控台的 600000
    with store.as_tenant(friend["id"]):
        store.save({"holdings": {"000333": {"code": "000333", "name": "美的", "support": 80.0, "level": "手动",
                                             "added": "2026-10-23", "status": store.NORMAL}}})
    main_st = c.get("/api/state", headers=auth("x", "main-pass")).get_json()
    assert [h["code"] for h in main_st["holdings"]] == ["600000"]               # 主控台也看不到朋友的
    assert [h["code"] for h in c.get("/api/state", headers=fa).get_json()["holdings"]] == ["000333"]
    # 朋友不能碰管理功能，也不能操作别人的票
    assert c.get("/api/admin/tenants", headers=fa).status_code == 403
    assert c.post("/api/sold", json={"code": "600000", "price": 1}, headers=fa).status_code == 400
    assert "600000" in store.load()[0]["holdings"]


def test_admin_overview_has_no_holding_details(multi):
    c, friend, auth = multi
    with store.as_tenant(friend["id"]):
        store.save({"holdings": {"000333": {"code": "000333", "name": "美的", "support": 80.0}}})
    ov = c.get("/api/admin/tenants", headers=auth("x", "main-pass")).get_json()["tenants"][0]
    assert ov["holdings"] == 1 and "000333" not in json.dumps(ov) and "美的" not in json.dumps(ov)


def test_revoke_and_restore_access(multi):
    c, friend, auth = multi
    fa, ma = auth(friend["id"], friend["password"]), auth("x", "main-pass")
    assert c.get("/api/state", headers=fa).status_code == 200
    c.post(f"/api/admin/tenants/{friend['id']}/disable", headers=ma)
    assert c.get("/api/state", headers=fa).status_code == 401                    # 立刻进不来
    assert friend["id"] not in tenants.active_ids()                              # 定时任务也不替他跑
    c.post(f"/api/admin/tenants/{friend['id']}/enable", headers=ma)
    assert c.get("/api/state", headers=fa).status_code == 200
    new_pw = c.post(f"/api/admin/tenants/{friend['id']}/reset", headers=ma).get_json()["password"]
    assert c.get("/api/state", headers=fa).status_code == 401
    assert c.get("/api/state", headers=auth(friend["id"], new_pw)).status_code == 200
    with store.as_tenant(friend["id"]):
        store.log("建仓", "000333")
    c.post(f"/api/admin/tenants/{friend['id']}/delete", headers=ma)
    assert not (store.ROOT / "tenants" / friend["id"]).exists()
    assert c.get("/api/state", headers=auth(friend["id"], new_pw)).status_code == 401


def test_passwords_stored_hashed_only(multi):
    c, friend, auth = multi
    assert friend["password"] not in (store.ROOT / "tenants.json").read_text()
    assert not tenants.verify(friend["id"], "wrong") and tenants.verify(friend["id"], friend["password"])


def test_friend_alerts_go_to_friend_wechat_only(multi, monkeypatch):
    c, friend, auth = multi
    sent = []
    monkeypatch.setattr(app.notify.requests, "post", lambda url, **kw: sent.append(kw["json"]["token"]))
    fa = auth(friend["id"], friend["password"])
    c.post("/api/test-alert", headers=fa)
    assert sent == []                                                            # 朋友还没填 token：谁都不发
    c.post("/api/settings", json={"pushplus_token": "FRIEND-TOKEN"}, headers=fa)
    c.post("/api/test-alert", headers=fa)
    assert sent == ["FRIEND-TOKEN"]
    c.post("/api/test-alert", headers=auth("x", "main-pass"))
    assert sent == ["FRIEND-TOKEN", "MAIN-TOKEN"]
    # 提醒记录也分开
    assert len(c.get("/api/state", headers=fa).get_json()["alerts"]) == 2


def test_feedback_reaches_admin(multi, monkeypatch):
    c, friend, auth = multi
    sent = []
    monkeypatch.setattr(app.notify.requests, "post", lambda url, **kw: sent.append(kw["json"]))
    fa = auth(friend["id"], friend["password"])
    assert c.post("/api/feedback", json={"text": "回补区 3% 有点窄"}, headers=fa).get_json()["ok"]
    fb = c.get("/api/admin/tenants", headers=auth("x", "main-pass")).get_json()["feedback"]
    assert fb[0]["name"] == "老王" and fb[0]["text"] == "回补区 3% 有点窄"
    assert sent[0]["token"] == "MAIN-TOKEN" and "老王" in sent[0]["title"]


def test_rate_guard_rules():
    g = app.RateGuard()
    t = 1000.0
    for i in range(15):
        assert g.check("f1", f"sig{i}", t + i) is None                         # 1 分钟 15 次不同操作：放行
    assert "1 分钟内操作了 16 次" in g.check("f1", "sig15", t + 15)
    assert "暂停" in g.check("f1", "sig16", t + 30)                              # 冷却期内全拦
    assert g.check("f1", "sigX", t + 15 + 121) is None                          # 2 分钟后恢复
    g2 = app.RateGuard()
    for i in range(5):
        assert g2.check("f2", "same", t + i * 5) is None
    assert "同一个操作" in g2.check("f2", "same", t + 30)                        # 同一个操作重复 6 次
    assert g2.check("f3", "same", t + 30) is None                               # 各分控台互不影响


def test_rate_limit_popup_and_admin_notified_once(multi, monkeypatch):
    c, friend, auth = multi
    sent = []
    monkeypatch.setattr(app.notify.requests, "post", lambda url, **kw: sent.append(kw["json"]["title"]))
    fa = auth(friend["id"], friend["password"])
    codes = [c.post("/api/feedback", json={"text": f"刷{i}"}, headers=fa).status_code for i in range(20)]
    assert 429 in codes
    r = c.post("/api/feedback", json={"text": "再来"}, headers=fa)
    assert r.status_code == 429 and r.get_json()["rate_limited"]
    assert sum("操作异常频繁" in t for t in sent) == 1                             # 主控台只收到一次
    assert tenants.overview()[0]["rate_limited"] >= 1
    ma = auth("x", "main-pass")
    assert all(c.post("/api/test-alert", headers=ma).status_code == 200 for _ in range(20))  # 主控台不限


def test_scheduler_runs_each_active_account(multi, monkeypatch):
    import datetime as dt
    c, friend, auth = multi
    other = tenants.create("老李")
    tenants.set_enabled(other["id"], False)
    ran = []
    monkeypatch.setattr(core, "morning_call", lambda: ran.append(store.tenant()))
    monkeypatch.setattr(core, "monitor_intraday", lambda: 0)
    monkeypatch.setattr(data, "is_trading_day", lambda d=None: True)
    monkeypatch.setattr(data, "now", lambda: dt.datetime(2026, 10, 23, 9, 20))
    s = core.Scheduler()
    s.tick(); s.tick()
    assert ran == [store.MAIN, friend["id"]]                                    # 停用的老李不跑，各跑一次
    assert ("2026-10-23", "morning") in s.done and ("2026-10-23", f"{friend['id']}|morning") in s.done


def test_outage_violation_only_for_main_when_manually_stopped(multi, monkeypatch):
    import datetime as dt
    c, friend, auth = multi
    t = {"now": dt.datetime(2026, 10, 26, 14, 0)}
    monkeypatch.setattr(data, "now", lambda: t["now"])
    core.Scheduler().mark_stopped()
    t["now"] = dt.datetime(2026, 10, 26, 18, 0)
    core.Scheduler().check_downtime()
    assert [e["kind"] for e in store.events() if e["kind"] in ("违规", "故障")] == ["违规"]
    with store.as_tenant(friend["id"]):
        assert [e["kind"] for e in store.events()] == ["故障"]                  # 朋友关不了服务器，不算他违规


def test_pushplus_prompt_ack_persists_and_keeps_token(multi):
    c, friend, auth = multi
    fa = auth(friend["id"], friend["password"])
    st = c.get("/api/state", headers=fa).get_json()
    assert st["pushplus_ack"] is False and st["has_token"] is False          # 新分控台：要弹
    c.post("/api/settings", json={"pushplus_token": "FRIEND-TOKEN"}, headers=fa)
    c.post("/api/settings", json={"pushplus_ack": True}, headers=fa)         # 只点"已开通"
    st = c.get("/api/state", headers=fa).get_json()
    assert st["pushplus_ack"] is True and st["has_token"] is True             # token 没被清掉
    assert c.get("/api/state", headers=auth("x", "main-pass")).get_json()["pushplus_ack"] is True  # 主控台永不弹



# ---------- 2026-10-28 的事故：唤醒瞬间时钟没校准 ----------

def test_stale_clock_after_wake_skips_tick_until_calibrated(env, monkeypatch):
    import datetime as dt
    calls = []
    monkeypatch.setattr(core, "check_close", lambda final=True: calls.append(("close", final)) or 0)
    monkeypatch.setattr(core, "daily_briefing", lambda final=True: calls.append(("briefing", final)) or 0)
    monkeypatch.setattr(core, "morning_call", lambda: None)
    monkeypatch.setattr(core, "monitor_intraday", lambda: 0)
    monkeypatch.setattr(core, "_is_month_end", lambda: False)
    monkeypatch.setattr(data, "is_trading_day", lambda d=None: True)
    # 系统时钟停在昨晚 23:50，真实时间是今天 01:34（网络时间快 1 小时 44 分）
    monkeypatch.setattr(data, "now", lambda: dt.datetime(2026, 10, 27, 23, 50, tzinfo=data.TZ))
    monkeypatch.setattr(data, "clock_skew", lambda: 6240.0)
    s = core.Scheduler()
    s.tick(); s.tick()
    assert calls == [] and not s.done                                          # 时钟不可信：什么都不跑、什么都不标记
    # 平台把时钟校准好了：今天 01:34，离收盘还早
    monkeypatch.setattr(data, "now", lambda: dt.datetime(2026, 10, 28, 1, 34, tzinfo=data.TZ))
    monkeypatch.setattr(data, "clock_skew", lambda: 0.3)
    s.tick()
    assert calls == [] and ("2026-10-28", "close") not in s.done               # 15:30 的收盘检查没有被提前"做掉"
    monkeypatch.setattr(data, "now", lambda: dt.datetime(2026, 10, 28, 15, 30, tzinfo=data.TZ))
    s.tick(); s.tick()
    assert calls == [("close", False), ("briefing", False)]


def test_persistently_wrong_clock_falls_back_to_network_time(monkeypatch):
    monkeypatch.setattr(data, "_clock_offset", 0.0)
    monkeypatch.setattr(data, "clock_skew", lambda: 3600.0)
    s = core.Scheduler()
    assert not s._clock_ok() and not s._clock_ok()                             # 先等平台校准
    assert s._clock_ok() and data._clock_offset == 3600                         # 第 3 次还不准：改用网络时间
    monkeypatch.setattr(data, "clock_skew", lambda: 0.0)                        # 后来系统时钟被修好了
    assert not s._clock_ok() and not s._clock_ok() and s._clock_ok()
    assert data._clock_offset == 0                                              # 校正自动撤掉，不会双重校正


def test_manual_close_check_refused_before_close(multi, monkeypatch):
    import datetime as dt
    c, friend, auth = multi
    monkeypatch.setattr(data, "is_trading_day", lambda d=None: True)
    monkeypatch.setattr(data, "now", lambda: dt.datetime(2026, 10, 28, 1, 34, tzinfo=data.TZ))
    r = c.post("/api/run/close", headers=auth("x", "main-pass"))
    assert r.status_code == 400 and "还没收盘" in r.get_json()["error"]


def test_no_morning_or_briefing_push_without_holdings(env, monkeypatch):
    clock, feed, pushes = env
    store.save({"holdings": {}})
    monkeypatch.setattr(data, "index_bars", lambda symbol="sh000300": pd.DataFrame({"date": ["2026-10-23"], "close": [1.0]}))
    pushes.clear()
    core.morning_call(); core.daily_briefing()
    assert pushes == []



def test_monthly_report_sent_once_on_last_trading_day(env, monkeypatch):
    import datetime as dt
    clock, feed, pushes = env
    cal = ["2026-10-29", "2026-10-30", "2026-11-02"]
    monkeypatch.setattr(data, "trading_days", lambda: cal)
    monkeypatch.setattr(data, "is_trading_day", lambda d=None: True)
    monkeypatch.setattr(core, "check_close", lambda final=True: 0)
    monkeypatch.setattr(core, "daily_briefing", lambda final=True: 0)
    monkeypatch.setattr(core, "morning_call", lambda: None)
    monkeypatch.setattr(core, "monitor_intraday", lambda: 0)
    monkeypatch.setattr(data, "daily_bars", lambda code, days=200: daily_on("2026-10-30", [10.0] * 6))
    t = {"now": dt.datetime(2026, 10, 29, 15, 31, tzinfo=data.TZ)}
    monkeypatch.setattr(data, "now", lambda: t["now"])
    clock["today"] = "2026-10-29"
    s = core.Scheduler()
    s.tick(); s.tick()
    assert not any("纪律报告" in p[0] for p in pushes)                          # 10-29 不是月末
    clock["today"] = "2026-10-30"
    t["now"] = dt.datetime(2026, 10, 30, 15, 31, tzinfo=data.TZ)
    s.tick(); s.tick(); s.tick()
    assert [p for p in pushes if "纪律报告" in p[0]] == [("📋 2026-10 纪律报告", "warn")]  # 发且只发一次


def test_monthly_report_still_sent_if_close_done_before_restart(env, monkeypatch):
    import datetime as dt
    clock, feed, pushes = env
    monkeypatch.setattr(data, "trading_days", lambda: ["2026-10-30", "2026-11-02"])
    monkeypatch.setattr(data, "is_trading_day", lambda d=None: True)
    monkeypatch.setattr(core, "daily_briefing", lambda final=True: 0)
    monkeypatch.setattr(data, "daily_bars", lambda code, days=200: daily_on("2026-10-30", [10.0] * 6))
    monkeypatch.setattr(data, "now", lambda: dt.datetime(2026, 10, 30, 16, 0, tzinfo=data.TZ))
    clock["today"] = "2026-10-30"
    s = core.Scheduler()
    s.done |= {("2026-10-30", "close"), ("2026-10-30", "briefing")}           # 重启前收盘检查已做完
    s.tick()
    assert ("📋 2026-10 纪律报告", "warn") in pushes


def test_no_monthly_report_for_empty_account(env):
    clock, feed, pushes = env
    store.save({"holdings": {}})
    pushes.clear()
    core.monthly_report()
    assert pushes == []
