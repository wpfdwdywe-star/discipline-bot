"""提醒：Mac 系统弹窗/语音 + 微信推送。按账号隔离：主控台用 config.json 的 token，
分控台用自己在面板里填的 token（tenants/<账号>/settings.json），提醒永远只发给当前账号自己。

level:
  info      普通通知
  warn      通知 + 提示音
  critical  强提醒：弹窗一直挂着直到你点掉 + 警报音 + 语音播报 + 微信
"""
import json
import subprocess
import sys
from pathlib import Path

import requests

import store
import tenants

CONFIG = Path(__file__).parent / "config.json"

# 页面轮询这个来显示最近的提醒，按账号分开
_recent: dict[str, list[dict]] = {}


def recent() -> list[dict]:
    return _recent.get(store.tenant(), [])


def _config() -> dict:
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def _osa(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _mac(title: str, body: str, level: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        if level == "critical":
            subprocess.Popen(["afplay", "/System/Library/Sounds/Sosumi.aiff"])
            subprocess.Popen(["say", title.lstrip("🔴⚠️ℹ️🟢🟠 ")])
            subprocess.Popen(["osascript", "-e", "tell me to activate", "-e",
                              f'display alert "{_osa(title)}" message "{_osa(body)}" as critical '
                              f'buttons {{"知道了，马上去卖"}} default button 1'])
        else:
            sound = ' sound name "Glass"' if level == "warn" else ""
            subprocess.Popen(["osascript", "-e",
                              f'display notification "{_osa(body[:200])}" with title "{_osa(title)}"{sound}'])
    except Exception as e:
        print(f"[系统提醒失败] {e}")


def _wechat(title: str, body: str) -> None:
    if store.tenant() == store.MAIN:
        cfg = _config()
    else:
        cfg = tenants.settings(store.tenant())
    try:
        if cfg.get("pushplus_token"):
            requests.post("https://www.pushplus.plus/send", timeout=10, json={
                "token": cfg["pushplus_token"], "title": title,
                "content": body.replace("\n", "<br>"), "template": "html"})
        if cfg.get("serverchan_key"):
            requests.post(f"https://sctapi.ftqq.com/{cfg['serverchan_key']}.send", timeout=10,
                          data={"title": title, "desp": body.replace("\n", "\n\n")})
    except Exception as e:
        print(f"[推送失败] {e}")


def push(title: str, body: str, level: str = "info") -> None:
    from data import now
    print(f"\n==== {title} ====\n{body}\n")
    items = _recent.setdefault(store.tenant(), [])
    items.insert(0, {"time": f"{now():%m-%d %H:%M:%S}", "title": title, "body": body, "level": level})
    del items[50:]
    if store.tenant() == store.MAIN:
        _mac(title, body, level)  # Mac 弹窗只给本机主人
    _wechat(title, body)
