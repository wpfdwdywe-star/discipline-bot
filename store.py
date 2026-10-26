"""持仓状态 + 事件日志 + 防篡改封条。

state.json  当前持仓和状态
events.jsonl 只追加的事件记录（月报从这里统计）
.seal       state.json 的哈希；手动改文件会被发现并记违规
"""
import hashlib
import json
import os
from pathlib import Path

from data import now

# 服务器上放到持久化磁盘（install.sh 会设置 DISCIPLINE_DATA）
DIR = Path(os.environ.get("DISCIPLINE_DATA") or Path(__file__).parent / "data")
STATE = DIR / "state.json"
EVENTS = DIR / "events.jsonl"
SEAL = DIR / ".seal"

# 持仓状态
NORMAL = "持有"
WATCH = "观察期"        # 第1天收盘跌破
SELL = "待卖出"         # 第2天没收回，必须清仓


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load() -> tuple[dict, bool]:
    """返回 (state, tampered)。"""
    DIR.mkdir(exist_ok=True)
    if not STATE.exists():
        return {"holdings": {}}, False
    text = STATE.read_text()
    tampered = not SEAL.exists() or SEAL.read_text().strip() != _digest(text)
    return json.loads(text), tampered


def save(state: dict) -> None:
    DIR.mkdir(exist_ok=True)
    text = json.dumps(state, ensure_ascii=False, indent=2)
    STATE.write_text(text)
    SEAL.write_text(_digest(text))


def log(kind: str, code: str = "", **fields) -> dict:
    DIR.mkdir(exist_ok=True)
    event = {"time": now().strftime("%Y-%m-%d %H:%M:%S"), "kind": kind, "code": code, **fields}
    with EVENTS.open("a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def events(month: str | None = None) -> list[dict]:
    if not EVENTS.exists():
        return []
    out = [json.loads(line) for line in EVENTS.read_text().splitlines() if line.strip()]
    return [e for e in out if not month or e["time"].startswith(month)]
