#!/bin/bash
# 双击启动纪律机器人。这个窗口开着 = 机器人在监控；关掉窗口就停止。
cd "$(dirname "$0")" || exit 1

PY=$(command -v python3)
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  osascript -e 'display alert "需要 Python 3.10 以上" message "请先到 python.org 下载安装 Python（选最新版），装好后再双击启动。"'
  open "https://www.python.org/downloads/macos/"
  exit 1
fi

if ! .venv/bin/python -c 'import flask, akshare' 2>/dev/null; then
  echo "第一次启动，正在安装依赖（需要几分钟）…"
  "$PY" -m venv --clear .venv && .venv/bin/pip install -q -r requirements.txt || {
    echo "安装失败，请检查网络后重试。"; read -r; exit 1; }
fi
[ -f config.json ] || cp config.example.json config.json

# caffeinate：机器人运行期间不让 Mac 睡眠（合上盖子仍会睡）
caffeinate -i .venv/bin/python app.py
