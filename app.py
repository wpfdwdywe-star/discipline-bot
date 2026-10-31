"""交易纪律机器人 · 网页界面 + 定时检查。

Mac 本机：双击「启动纪律机器人.command」，打开 http://127.0.0.1:8765
服务器：  sudo bash install.sh 安装成系统服务；config.json 里 host=0.0.0.0 且必须设 password

登录：主控台 = 任意用户名 + config.json 的 password；分控台 = 主控台创建时给的账号 + 密码。
每个请求按登录账号切换数据目录，分控台访问不到别人的数据。
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import signal
import threading
import time
import webbrowser
from collections import defaultdict, deque
from functools import wraps
from pathlib import Path

from flask import Flask, Response, g, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

import core
import data
import notify
import store
import tenants
from core import Refused

app = Flask(__name__)
scheduler = core.Scheduler()


def config() -> dict:
    return json.loads(notify.CONFIG.read_text()) if notify.CONFIG.exists() else {}


class RateGuard:
    """分控台防刷：短时间大量操作、或同一个操作反复点，不像真人 → 暂停 2 分钟，页面弹窗提醒，通知主控台。

    只数会触发取数的操作（POST），页面自动刷新（GET）不算；主控台不受限制。
    """
    PER_MINUTE = 15
    PER_10_MINUTES = 60
    SAME_PER_MINUTE = 6
    COOLDOWN = 120

    def __init__(self):
        self.hits: dict[str, deque] = defaultdict(deque)
        self.blocked_until: dict[str, float] = {}
        self.alerted: dict[str, str] = {}

    def check(self, tid: str, signature: str, now: float) -> str | None:
        """返回 None = 放行；返回提示文字 = 拦住。"""
        if now < self.blocked_until.get(tid, 0):
            return f"操作太频繁，已暂停，请 {int(self.blocked_until[tid] - now) + 1} 秒后再试。"
        hits = self.hits[tid]
        hits.append((now, signature))
        while hits and now - hits[0][0] > 600:
            hits.popleft()
        last_min = [sig for t, sig in hits if now - t <= 60]
        reason = (f"1 分钟内操作了 {len(last_min)} 次" if len(last_min) > self.PER_MINUTE else
                  f"10 分钟内操作了 {len(hits)} 次" if len(hits) > self.PER_10_MINUTES else
                  f"同一个操作 1 分钟内重复了 {last_min.count(signature)} 次"
                  if last_min.count(signature) >= self.SAME_PER_MINUTE else None)
        if not reason:
            return None
        self.blocked_until[tid] = now + self.COOLDOWN
        hits.clear()
        times = tenants.note_rate_limited(tid)
        if self.alerted.get(tid) != data.today():  # 每个分控台每天只通知主控台一次
            self.alerted[tid] = data.today()
            push_admin(f"⚠️ 分控台「{tenants.name(tid)}」操作异常频繁",
                       f"{reason}，不像真人在用，已暂停它 {self.COOLDOWN // 60} 分钟（累计第 {times} 次）。\n"
                       f"如果是恶意使用，可以在主控台停用这个分控台。")
        return (f"检测到短时间内大量重复操作（{reason}），不像正常使用。\n"
                f"为了防止数据接口被限流，已暂停 {self.COOLDOWN // 60} 分钟，请稍后再试。")


guard = RateGuard()


def push_admin(title: str, body: str) -> None:
    with store.as_tenant(store.MAIN):
        notify.push(title, body, "warn")


@app.before_request
def login():
    if request.path == "/healthz":
        return None
    password = config().get("password")
    auth = request.authorization
    if not password:
        tid = store.MAIN  # 本机无密码模式（Mac 本地），只有主控台
    elif auth and hmac.compare_digest((auth.password or "").encode(), password.encode()):
        tid = store.MAIN
    elif auth and tenants.verify(auth.username, auth.password or ""):
        tid = auth.username
    else:
        if auth:
            time.sleep(1)  # 拖慢暴力猜密码
        return Response("需要登录（分控台已停用的话也会进不来）", 401,
                        {"WWW-Authenticate": 'Basic realm="discipline-bot", charset="UTF-8"'})
    g.tenant_token = store.enter(tid)
    if tid != store.MAIN:
        tenants.touch(tid)
        if request.method == "POST":
            sig = request.path + hashlib.sha1(request.get_data()).hexdigest()
            if msg := guard.check(tid, sig, time.time()):
                return jsonify(ok=False, error=msg, rate_limited=True), 429
    return None


@app.teardown_request
def logout(exc):
    if "tenant_token" in g:
        store.leave(g.tenant_token)


def main_only(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if store.tenant() != store.MAIN:
            return jsonify(ok=False, error="只有主控台能做这个操作"), 403
        return fn(*a, **kw)
    return wrapper


def friend_only(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if store.tenant() == store.MAIN:
            return jsonify(ok=False, error="这是分控台的功能"), 400
        return fn(*a, **kw)
    return wrapper


def _int(x) -> int | None:
    return int(x) if x not in (None, "") else None


def ok(**kw):
    return jsonify(ok=True, **kw)


@app.errorhandler(Refused)
@app.errorhandler(tenants.TenantError)
def refused(e):
    return jsonify(ok=False, error=str(e)), 400


@app.errorhandler(Exception)
def failed(e):
    if isinstance(e, HTTPException):
        return e
    return jsonify(ok=False, error=f"出错了：{e}"), 500


@app.get("/healthz")
def healthz():
    """给外部定时唤醒用（不需要密码，不返回任何持仓信息）。访问时顺便立刻跑一次调度。"""
    threading.Thread(target=scheduler.tick, daemon=True).start()
    return jsonify(ok=True, time=f"{data.now():%Y-%m-%d %H:%M:%S}")


@app.get("/")
def index():
    return send_file(Path(__file__).parent / "ui.html")


@app.get("/api/state")
def state():
    now = data.now()
    try:
        trading = data.is_trading_day()
    except Exception:
        trading = None
    tid = store.tenant()
    return ok(
        now=f"{now:%Y-%m-%d %H:%M:%S}", trading_day=trading,
        role="main" if tid == store.MAIN else "friend", account=tenants.name(tid),
        has_token=bool(config().get("pushplus_token") if tid == store.MAIN
                       else tenants.settings(tid).get("pushplus_token")),
        pushplus_ack=tid == store.MAIN or bool(tenants.settings(tid).get("pushplus_ack")),
        holdings=core.holdings(),
        alerts=notify.recent()[:20],
        events=list(reversed(store.events()))[:40],
        scheduler=scheduler.notes()[:10],
        briefing=core.last_briefing(),
        auto_trade=False,
    )


# ---------------- 主控台：分控台管理 ----------------

@app.get("/api/admin/tenants")
@main_only
def admin_tenants():
    return ok(tenants=tenants.overview(), feedback=list(reversed(tenants.feedback()))[:100])


@app.post("/api/admin/tenants")
@main_only
def admin_create():
    if not config().get("password"):
        raise Refused("主控台还没设密码（config.json 的 password），没法区分账号，先设密码再创建分控台。")
    return ok(**tenants.create(request.json.get("name", "")), url=request.host_url)


@app.post("/api/admin/tenants/<tid>/<action>")
@main_only
def admin_action(tid, action):
    if action == "reset":
        return ok(password=tenants.reset_password(tid))
    {"enable": lambda: tenants.set_enabled(tid, True), "disable": lambda: tenants.set_enabled(tid, False),
     "delete": lambda: tenants.delete(tid)}[action]()
    return ok()


# ---------------- 分控台：设置和意见 ----------------

@app.post("/api/settings")
@friend_only
def save_settings():
    """只改传了的项：单独点"已开通"不会把已经填好的 token 清掉。"""
    j, values = request.json or {}, {}
    if "pushplus_token" in j:
        values["pushplus_token"] = j["pushplus_token"].strip()
    if j.get("pushplus_ack"):
        values["pushplus_ack"] = True
    tenants.save_settings(store.tenant(), values)
    return ok()


@app.post("/api/feedback")
@friend_only
def feedback():
    item = tenants.add_feedback(store.tenant(), request.json.get("text", ""))
    push_admin(f"💬 {item['name']} 提了意见", item["text"])
    return ok()


@app.post("/api/platforms")
def platforms():
    code = request.json["code"].strip()
    if not (code.isdigit() and len(code) == 6):
        raise Refused("请输入 6 位股票代码")
    return ok(**core.candidate_platforms(code))


@app.post("/api/checkup")
def checkup_():
    code = request.json["code"].strip()
    if not (code.isdigit() and len(code) == 6):
        raise Refused("请输入 6 位股票代码")
    return ok(**core.pre_buy_checkup(code))


@app.post("/api/add")
def add():
    j = request.json
    h = core.add_holding(j["code"], float(j["support"]), j.get("level") or "手动", j.get("platform"),
                         _int(j.get("shares")), existing=bool(j.get("existing")))
    return ok(holding=h)


@app.post("/api/raise")
def raise_():
    j = request.json
    return ok(holding=core.raise_support(j["code"], float(j["price"])))


@app.post("/api/sold")
def sold():
    j = request.json
    price = float(j["price"]) if j.get("price") not in (None, "") else None
    return ok(message=core.record_sold(j["code"], price))


@app.post("/api/trim")
def trim():
    j = request.json
    return ok(**core.trim_position(j["code"], float(j["price"]), _int(j.get("qty"))))


@app.post("/api/rebuy-plan")
def rebuy_plan():
    j = request.json
    h = core.set_rebuy_plan(j["code"], float(j["support"]), float(j["sell_price"]),
                            j.get("level") or "手动", j.get("platform"))
    return ok(holding=h)


@app.post("/api/rebuy-candidates")
def rebuy_candidates():
    j = request.json
    return ok(platforms=core.rebuy_candidates(j["code"], float(j["below"])))


@app.post("/api/rebuy-lower")
def rebuy_lower():
    j = request.json
    return ok(holding=core.lower_rebuy(j["code"], float(j["price"])))


@app.post("/api/rebuy-cancel")
def rebuy_cancel():
    return ok(holding=core.cancel_rebuy(request.json["code"]))


@app.post("/api/rebuy")
def rebuy():
    j = request.json
    price = float(j["price"]) if j.get("price") not in (None, "") else None
    return ok(message=core.record_rebuy(j["code"], price, _int(j.get("qty"))))


@app.post("/api/run/<job>")
def run(job):
    if job == "close":
        # 收盘前手动跑收盘检查，会因为"今天还没有日K"误报停牌
        if not data.is_trading_day():
            raise Refused("今天休市，没有收盘检查可做。")
        if data.now().time() < dt.time(15, 0):
            raise Refused("还没收盘（15:00），现在做收盘检查会误判。收盘后再点，或者等 15:30 自动检查。")
    {"close": core.check_close, "intraday": core.check_intraday, "briefing": core.daily_briefing}[job]()
    return ok()


@app.get("/api/report")
def report():
    return ok(text=core.build_report(request.args.get("month")))


@app.post("/api/test-alert")
def test_alert():
    notify.push("🔴 测试强提醒", "这是一条测试。真正触发清仓时就是这样提醒你：\n"
                "微信推送；在 Mac 上运行时还有不点掉不会消失的弹窗、警报声和语音。", "critical")
    return ok()


def main():
    cfg = config()
    host = cfg.get("host", "127.0.0.1")
    port = int(os.environ.get("DISCIPLINE_PORT", cfg.get("port", 8765)))
    if host not in ("127.0.0.1", "localhost") and not cfg.get("password"):
        raise SystemExit("对外开放（host 不是 127.0.0.1）时必须在 config.json 里设置 password。")
    def on_stop(signum, frame):
        scheduler.mark_stopped()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, on_stop)
    scheduler.start()
    # 启动时如果还有没卖的票，先催一次
    threading.Timer(2, core.nag_pending).start()
    if host in ("127.0.0.1", "localhost"):
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    print(f"纪律机器人已启动：http://{host}:{port}")
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
