#!/bin/bash
# 在 Ubuntu 服务器上安装纪律机器人（系统服务：开机自启、崩溃自动重启）。
# 用法：sudo bash install.sh
# 再次运行 = 更新代码，保留 config.json 和 data/（持仓、日志）。
set -euo pipefail

APP=/opt/discipline_bot
SERVICE=discipline-bot
SRC=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || { echo "请用 sudo 运行：sudo bash install.sh"; exit 1; }

echo "==> 安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl >/dev/null
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || {
  echo "需要 Python 3.10 以上，请用 Ubuntu 22.04+ 或 Debian 12+。"; exit 1; }

echo "==> 复制程序到 $APP"
id discipline >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin discipline
mkdir -p "$APP"
cp "$SRC"/*.py "$SRC"/ui.html "$SRC"/requirements.txt "$SRC"/config.example.json "$APP"/

# 有独立的持久化磁盘 /data（如 instabox）就把持仓和日志放过去，否则放在程序目录里
if mountpoint -q /data 2>/dev/null; then DATA=/data/discipline_bot; else DATA=$APP/data; fi
mkdir -p "$DATA"
if [ "$DATA" != "$APP/data" ] && [ -d "$APP/data" ] && [ ! -e "$DATA/state.json" ]; then
  cp -a "$APP/data/." "$DATA/" && rm -rf "$APP/data"
  echo "    已把旧数据迁移到 $DATA"
fi

echo "==> 安装 Python 依赖（第一次要几分钟）"
[ -x "$APP/.venv/bin/python" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

if [ ! -f "$APP/config.json" ]; then
  echo
  echo "==> 首次配置"
  read -rp "PushPlus token（微信推送，必填）：" TOKEN
  while true; do
    read -rsp "设置网页登录密码（至少 8 位）：" PW; echo
    [ ${#PW} -ge 8 ] && break
    echo "太短了，再来。"
  done
  TOKEN="$TOKEN" PW="$PW" "$APP/.venv/bin/python" - "$APP/config.json" <<'EOF'
import json, os, sys
json.dump({"pushplus_token": os.environ["TOKEN"], "serverchan_key": "", "password": os.environ["PW"],
           "host": "0.0.0.0", "port": 8765}, open(sys.argv[1], "w"), indent=2)
EOF
fi
chmod 600 "$APP/config.json"
chown -R discipline:discipline "$APP" "$DATA"

echo "==> 注册系统服务"
cat > /etc/systemd/system/$SERVICE.service <<EOF
[Unit]
Description=交易纪律机器人
After=network-online.target
Wants=network-online.target

[Service]
User=discipline
WorkingDirectory=$APP
ExecStart=$APP/.venv/bin/python app.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
Environment=DISCIPLINE_DATA=$DATA

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now $SERVICE
systemctl restart $SERVICE

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 8765/tcp >/dev/null
fi

sleep 3
PORT=$("$APP/.venv/bin/python" -c "import json; print(json.load(open('$APP/config.json')).get('port', 8765))")
URL=""
if command -v instabox-port >/dev/null; then
  # instabox：发布公开 HTTPS 网址（访问它还能唤醒休眠中的机器）
  # 已经发布过就沿用原网址；输出里 JSON 后面还跟着一行提示，只解析第一个 JSON 对象
  PARSE="import json,sys; d=json.JSONDecoder().raw_decode(sys.stdin.read().lstrip())[0]
ps=d.get('ports', [d]); print(next((x['url'] for x in ps if x.get('port')==$PORT), ''))"
  IBP="sudo -u ${SUDO_USER:-root} instabox-port"
  URL=$($IBP list 2>/dev/null | python3 -c "$PARSE" 2>/dev/null || true)
  [ -n "$URL" ] || URL=$($IBP publish "$PORT" --name "纪律机器人" --json 2>/dev/null | python3 -c "$PARSE" 2>/dev/null || true)
fi
[ -n "$URL" ] || URL="http://$(curl -s -m 5 https://api.ipify.org || echo 服务器IP):$PORT"
echo
if systemctl is-active --quiet $SERVICE; then
  echo "✅ 安装完成，机器人已在运行。数据目录：$DATA"
  echo "   网页：$URL  （用户名随便填，密码是刚才设的）"
  echo "   定时唤醒地址（不需要密码）：${URL%/}/healthz"
  echo "   如果打不开：去云服务商控制台的 防火墙/安全组 放行 TCP $PORT 端口；"
  echo "   或者不开端口，用 SSH 隧道：ssh -L $PORT:127.0.0.1:$PORT 你的服务器，然后打开 http://127.0.0.1:$PORT"
  echo "   查看运行日志：sudo journalctl -u $SERVICE -f"
else
  echo "❌ 服务没有启动成功，看日志：sudo journalctl -u $SERVICE -n 50"
  exit 1
fi
