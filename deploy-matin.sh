#!/bin/bash
set -Eeuo pipefail

# ===== INPUTS =====
read -p "Server IP: " SERVER_IP
read -p "Server Path [/var/www/matin_panel]: " SERVER_PATH

SERVER_PATH=${SERVER_PATH:-/var/www/matin_panel}
APP_URL=${APP_URL:-http://matin.panelwpvideo.ir/}
TELEGRAM_PROXY_URL=${TELEGRAM_PROXY_URL:-}
TELEGRAM_PROXY_PROTOCOL=${TELEGRAM_PROXY_PROTOCOL:-}
TELEGRAM_PROXY_HOST=${TELEGRAM_PROXY_HOST:-}
TELEGRAM_PROXY_PORT=${TELEGRAM_PROXY_PORT:-}
TELEGRAM_PROXY_USERNAME=${TELEGRAM_PROXY_USERNAME:-}
TELEGRAM_PROXY_PASSWORD=${TELEGRAM_PROXY_PASSWORD:-}
TELEGRAM_POLLING_SERVICE_NAME=${TELEGRAM_POLLING_SERVICE_NAME:-telegram-polling-matin.service}

USER=root

echo "Deploying to $USER@$SERVER_IP:$SERVER_PATH"
echo "Health check URL: $APP_URL"
echo "Telegram polling service: $TELEGRAM_POLLING_SERVICE_NAME"

# ===== RSYNC =====
rsync -avz --delete \
  --exclude 'venv/' \
  --exclude 'node_modules/' \
  --exclude 'media/' \
  --exclude '.git/' \
  --exclude '__pycache__/' \
  --exclude 'db.sqlite3' \
  --exclude '*.pyc' \
  --exclude 'gunicorn.sock' \
  ./ "$USER@$SERVER_IP:$SERVER_PATH"

# ===== REMOTE COMMANDS =====
ssh "$USER@$SERVER_IP" bash -se -- "$SERVER_PATH" "$APP_URL" "$TELEGRAM_PROXY_URL" "$TELEGRAM_PROXY_PROTOCOL" "$TELEGRAM_PROXY_HOST" "$TELEGRAM_PROXY_PORT" "$TELEGRAM_PROXY_USERNAME" "$TELEGRAM_PROXY_PASSWORD" "$TELEGRAM_POLLING_SERVICE_NAME" << 'EOF'
set -Eeuo pipefail

SERVER_PATH=$1
APP_URL=$2
TELEGRAM_PROXY_URL=$3
TELEGRAM_PROXY_PROTOCOL=$4
TELEGRAM_PROXY_HOST=$5
TELEGRAM_PROXY_PORT=$6
TELEGRAM_PROXY_USERNAME=$7
TELEGRAM_PROXY_PASSWORD=$8
TELEGRAM_POLLING_SERVICE_NAME=$9
SERVICE_NAME=gunicorn-matin.service

cd "$SERVER_PATH"

source venv/bin/activate

pip install --disable-pip-version-check -r requirements.txt

python manage.py collectstatic --noinput

python manage.py migrate

mkdir -p "/etc/systemd/system/$SERVICE_NAME.d"
python - "$TELEGRAM_PROXY_URL" "$TELEGRAM_PROXY_PROTOCOL" "$TELEGRAM_PROXY_HOST" "$TELEGRAM_PROXY_PORT" "$TELEGRAM_PROXY_USERNAME" "$TELEGRAM_PROXY_PASSWORD" "/etc/systemd/system/$SERVICE_NAME.d/telegram-proxy.conf" << 'PY'
import sys

proxy_url, protocol, host, port, username, password, output_path = sys.argv[1:8]

def escaped(value):
    return value.replace("\\", "\\\\").replace('"', '\\"')

with open(output_path, "w", encoding="utf-8") as file:
    file.write('[Service]\n')
    for key, value in {
        "TELEGRAM_PROXY_URL": proxy_url,
        "TELEGRAM_PROXY_PROTOCOL": protocol,
        "TELEGRAM_PROXY_HOST": host,
        "TELEGRAM_PROXY_PORT": port,
        "TELEGRAM_PROXY_USERNAME": username,
        "TELEGRAM_PROXY_PASSWORD": password,
        "TELEGRAM_WEBHOOK_RESPONSE_ENABLED": "False",
    }.items():
        file.write(f'Environment="{key}={escaped(value)}"\n')
PY

python - "$SERVER_PATH" "$TELEGRAM_POLLING_SERVICE_NAME" "$TELEGRAM_PROXY_URL" "$TELEGRAM_PROXY_PROTOCOL" "$TELEGRAM_PROXY_HOST" "$TELEGRAM_PROXY_PORT" "$TELEGRAM_PROXY_USERNAME" "$TELEGRAM_PROXY_PASSWORD" << 'PY'
import sys

server_path, service_name, proxy_url, protocol, host, port, username, password = sys.argv[1:9]

def escaped(value):
    return value.replace("\\", "\\\\").replace('"', '\\"')

env_lines = "\n".join(
    f'Environment="{key}={escaped(value)}"'
    for key, value in {
        "TELEGRAM_PROXY_URL": proxy_url,
        "TELEGRAM_PROXY_PROTOCOL": protocol,
        "TELEGRAM_PROXY_HOST": host,
        "TELEGRAM_PROXY_PORT": port,
        "TELEGRAM_PROXY_USERNAME": username,
        "TELEGRAM_PROXY_PASSWORD": password,
        "TELEGRAM_WEBHOOK_RESPONSE_ENABLED": "False",
    }.items()
)

content = f"""[Unit]
Description=Telegram long polling for vpn_store matin
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={server_path}
EnvironmentFile=-{server_path}/.env
{env_lines}
ExecStart={server_path}/venv/bin/python manage.py run_telegram_polling
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

with open(f"/etc/systemd/system/{service_name}", "w", encoding="utf-8") as file:
    file.write(content)
PY
systemctl daemon-reload
systemctl enable "$TELEGRAM_POLLING_SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
systemctl restart "$TELEGRAM_POLLING_SERVICE_NAME"

for _ in $(seq 1 20); do
  if systemctl is-active --quiet "$SERVICE_NAME" && systemctl is-active --quiet "$TELEGRAM_POLLING_SERVICE_NAME" && [ -S "$SERVER_PATH/gunicorn.sock" ]; then
    break
  fi
  sleep 1
done

systemctl is-active --quiet "$SERVICE_NAME"
systemctl is-active --quiet "$TELEGRAM_POLLING_SERVICE_NAME"
test -S "$SERVER_PATH/gunicorn.sock"

systemctl reload nginx

if command -v curl >/dev/null 2>&1; then
  curl -fsSI --max-time 15 "$APP_URL" >/dev/null
  curl -fsSI --max-time 15 "${APP_URL%/}/admin/" >/dev/null
fi

echo "DONE"

EOF
