"""持仓状态 + 事件日志 + 防篡改封条，按账号隔离。

主控台的数据在 ROOT 下（升级前就在这里，不用迁移），分控台的在 ROOT/tenants/<账号>/ 下：
state.json  当前持仓和状态
events.jsonl 只追加的事件记录（月报从这里统计）
.seal       state.json 的哈希；手动改文件会被发现并记违规

当前是哪个账号由 as_tenant() 设定（网页请求按登录账号设，定时任务逐个账号设），默认主控台。
"""
import hashlib
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from data import now

# 服务器上放到持久化磁盘（install.sh 会设置 DISCIPLINE_DATA）
ROOT = Path(os.environ.get("DISCIPLINE_DATA") or Path(__file__).parent / "data")
MAIN = "main"
_tenant: ContextVar[str] = ContextVar("tenant", default=MAIN)


def tenant() -> str:
    return _tenant.get()


@contextmanager
def as_tenant(tid: str):
    token = _tenant.set(tid)
    try:
        yield
    finally:
        _tenant.reset(token)


def enter(tid: str):
    """网页请求开始时切到登录账号（请求结束用 leave 还原）。"""
    return _tenant.set(tid)


def leave(token) -> None:
    _tenant.reset(token)


def dir() -> Path:  # noqa: A001 —— 当前账号的数据目录
    d = ROOT if tenant() == MAIN else ROOT / "tenants" / tenant()
    d.mkdir(parents=True, exist_ok=True)
    return d


# 持仓状态
NORMAL = "持有"
WATCH = "观察期"        # 第1天收盘跌破
SELL = "待卖出"         # 第2天没收回，必须清仓


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load() -> tuple[dict, bool]:
    """返回 (state, tampered)。"""
    state_f, seal_f = dir() / "state.json", dir() / ".seal"
    if not state_f.exists():
        return {"holdings": {}}, False
    text = state_f.read_text()
    tampered = not seal_f.exists() or seal_f.read_text().strip() != _digest(text)
    return json.loads(text), tampered


def save(state: dict) -> None:
    text = json.dumps(state, ensure_ascii=False, indent=2)
    (dir() / "state.json").write_text(text)
    (dir() / ".seal").write_text(_digest(text))


def log(kind: str, code: str = "", **fields) -> dict:
    event = {"time": now().strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "code": code, **fields}
    with (dir() / "events.jsonl").open("a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def events(month: str | None = None) -> list[dict]:
    f = dir() / "events.jsonl"
    if not f.exists():
        return []
    out = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return [e for e in out if not month or e["time"].startswith(month)]
