"""分控台账号：主控台创建、停用、恢复、删除、重置密码；朋友的意见反馈。

账号表在 ROOT/tenants.json。密码只存加盐的 scrypt 哈希，明文只在创建/重置时给主控台看一次。
停用 = 立刻登不进去、定时任务也不再替他跑，数据保留；删除 = 连数据一起清掉。
主控台只看使用情况（持仓只数、最近登录、意见数），看不到朋友的持仓内容。
"""
import hashlib
import hmac
import json
import secrets
import shutil
import threading

import store
from data import now

_lock = threading.RLock()


class TenantError(Exception):
    pass


def _file():
    return store.ROOT / "tenants.json"


def _read() -> dict:
    f = _file()
    return json.loads(f.read_text()) if f.exists() else {}


def _write(reg: dict) -> None:
    store.ROOT.mkdir(parents=True, exist_ok=True)
    _file().write_text(json.dumps(reg, ensure_ascii=False, indent=2))


def _hash(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex()


def _new_password() -> tuple[str, str, str]:
    pw = secrets.token_urlsafe(9)
    salt = secrets.token_hex(16)
    return pw, salt, _hash(pw, salt)


def active_ids() -> list[str]:
    return [tid for tid, t in _read().items() if t["enabled"]]


def verify(tid: str, password: str) -> bool:
    t = _read().get(tid or "")
    return bool(t and t["enabled"] and hmac.compare_digest(_hash(password, t["salt"]), t["hash"]))


def name(tid: str) -> str:
    return "主控台" if tid == store.MAIN else _read().get(tid, {}).get("name", tid)


def touch(tid: str) -> None:
    """记最近访问时间（每分钟最多写一次文件）。"""
    with _lock:
        reg = _read()
        if tid in reg and reg[tid].get("last_seen", "")[:16] != f"{now():%Y-%m-%d %H:%M}":
            reg[tid]["last_seen"] = f"{now():%Y-%m-%d %H:%M}"
            _write(reg)


def create(display_name: str) -> dict:
    display_name = display_name.strip()[:20]
    if not display_name:
        raise TenantError("请给分控台起个名字（比如朋友的称呼）")
    with _lock:
        reg = _read()
        tid = next(t for t in (f"f{secrets.token_hex(2)}" for _ in range(100)) if t not in reg)
        pw, salt, h = _new_password()
        reg[tid] = {"name": display_name, "salt": salt, "hash": h, "enabled": True,
                    "created": f"{now():%Y-%m-%d %H:%M}", "last_seen": "", "rate_limited": 0}
        _write(reg)
    return {"id": tid, "name": display_name, "password": pw}


def set_enabled(tid: str, enabled: bool) -> None:
    with _lock:
        reg = _read()
        if tid not in reg:
            raise TenantError("没有这个分控台")
        reg[tid]["enabled"] = enabled
        _write(reg)


def reset_password(tid: str) -> str:
    with _lock:
        reg = _read()
        if tid not in reg:
            raise TenantError("没有这个分控台")
        pw, reg[tid]["salt"], reg[tid]["hash"] = _new_password()
        _write(reg)
    return pw


def delete(tid: str) -> None:
    with _lock:
        reg = _read()
        if tid not in reg:
            raise TenantError("没有这个分控台")
        del reg[tid]
        _write(reg)
        shutil.rmtree(store.ROOT / "tenants" / tid, ignore_errors=True)


def note_rate_limited(tid: str) -> int:
    with _lock:
        reg = _read()
        if tid not in reg:
            return 0
        reg[tid]["rate_limited"] = reg[tid].get("rate_limited", 0) + 1
        reg[tid]["last_rate_limited"] = f"{now():%Y-%m-%d %H:%M}"
        _write(reg)
        return reg[tid]["rate_limited"]


def overview() -> list[dict]:
    """主控台看到的使用情况：不含任何持仓内容，只有只数。"""
    fb = feedback()
    out = []
    for tid, t in _read().items():
        state_f = store.ROOT / "tenants" / tid / "state.json"
        n = len(json.loads(state_f.read_text())["holdings"]) if state_f.exists() else 0
        out.append({"id": tid, "name": t["name"], "enabled": t["enabled"], "created": t["created"],
                    "last_seen": t.get("last_seen", ""), "holdings": n,
                    "feedback": sum(f["tenant"] == tid for f in fb),
                    "rate_limited": t.get("rate_limited", 0), "last_rate_limited": t.get("last_rate_limited", ""),
                    "has_token": bool(settings(tid).get("pushplus_token"))})
    return out


# ---- 分控台自己的设置（微信推送 token） ----

def settings(tid: str) -> dict:
    f = store.ROOT / "tenants" / tid / "settings.json"
    return json.loads(f.read_text()) if f.exists() else {}


def save_settings(tid: str, values: dict) -> None:
    d = store.ROOT / "tenants" / tid
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text(json.dumps({**settings(tid), **values}, ensure_ascii=False))


# ---- 意见反馈 ----

def add_feedback(tid: str, text: str) -> dict:
    text = text.strip()[:2000]
    if not text:
        raise TenantError("意见不能为空")
    item = {"time": f"{now():%Y-%m-%d %H:%M}", "tenant": tid, "name": name(tid), "text": text}
    with _lock:
        with (store.ROOT / "feedback.jsonl").open("a") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    return item


def feedback() -> list[dict]:
    f = store.ROOT / "feedback.jsonl"
    if not f.exists():
        return []
    return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
