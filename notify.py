"""提醒：Mac 系统弹窗/语音 + 微信推送（config.json 里填 pushplus_token 或 serverchan_key）。

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

CONFIG = Path(__file__).parent / "config.json"

# App 页面轮询这个列表来显示最近的提醒
recent: list[dict] = []


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
    cfg = _config()
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
    recent.insert(0, {"time": f"{now():%m-%d %H:%M:%S}", "title": title, "body": body, "level": level})
    del recent[50:]
    _mac(title, body, level)
    _wechat(title, body)
