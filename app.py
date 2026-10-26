"""交易纪律机器人 · 网页界面 + 定时检查。

Mac 本机：双击「启动纪律机器人.command」，打开 http://127.0.0.1:8765
服务器：  sudo bash install.sh 安装成系统服务；config.json 里 host=0.0.0.0 且必须设 password
"""
import hmac
import json
import os
import signal
import threading
import time
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

import core
import data
import notify
import store
from core import Refused

app = Flask(__name__)
scheduler = core.Scheduler()


def config() -> dict:
    return json.loads(notify.CONFIG.read_text()) if notify.CONFIG.exists() else {}


@app.before_request
def require_password():
    password = config().get("password")
    if not password or request.path == "/healthz":
        return None
    auth = request.authorization
    if auth and hmac.compare_digest((auth.password or "").encode(), password.encode()):
        return None
    if auth:
        time.sleep(1)  # 拖慢暴力猜密码
    return Response("需要密码", 401, {"WWW-Authenticate": 'Basic realm="discipline-bot", charset="UTF-8"'})


def _int(x) -> int | None:
    return int(x) if x not in (None, "") else None


def ok(**kw):
    return jsonify(ok=True, **kw)


@app.errorhandler(Refused)
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
    return ok(
        now=f"{now:%Y-%m-%d %H:%M:%S}", trading_day=trading,
        holdings=core.holdings(),
        alerts=notify.recent[:20],
        events=list(reversed(store.events()))[:40],
        scheduler=scheduler.log[:10],
        briefing=core.last_briefing(),
        auto_trade=False,
    )


@app.post("/api/platforms")
def platforms():
    code = request.json["code"].strip()
    if not (code.isdigit() and len(code) == 6):
        raise Refused("请输入 6 位股票代码")
    return ok(**core.candidate_platforms(code))


@app.post("/api/add")
def add():
    j = request.json
    h = core.add_holding(j["code"], float(j["support"]), j.get("level") or "手动", j.get("platform"),
                         _int(j.get("shares")))
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
