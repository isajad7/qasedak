#!/usr/bin/env bash
set -Eeuo pipefail

# ===== TARGET =====
SERVER_IP=${SERVER_IP:-45.195.200.38}
SERVER_PATH=${SERVER_PATH:-/var/www/vpn_store}
APP_DOMAIN=${APP_DOMAIN:-botsell2.panelwpvideo.ir}
APP_EXTRA_DOMAINS=${APP_EXTRA_DOMAINS:-botsell2.panlelwpvideo.ir,botsell.panelwpvideo.ir,45.195.200.38}
APP_URL=${APP_URL:-http://${SERVER_IP}/}
TELEGRAM_PROXY_URL=${TELEGRAM_PROXY_URL-__preserve__}
TELEGRAM_PROXY_PROTOCOL=${TELEGRAM_PROXY_PROTOCOL-__preserve__}
TELEGRAM_PROXY_HOST=${TELEGRAM_PROXY_HOST-__preserve__}
TELEGRAM_PROXY_PORT=${TELEGRAM_PROXY_PORT-__preserve__}
TELEGRAM_PROXY_USERNAME=${TELEGRAM_PROXY_USERNAME-__preserve__}
TELEGRAM_PROXY_PASSWORD=${TELEGRAM_PROXY_PASSWORD-__preserve__}
XUI_PANEL_PROXY_URL=${XUI_PANEL_PROXY_URL:-}
SERVICE_NAME=${SERVICE_NAME:-gunicorn-sajad.service}
TELEGRAM_POLLING_SERVICE_NAME=${TELEGRAM_POLLING_SERVICE_NAME:-telegram-polling-sajad.service}
SSH_USER=${SSH_USER:-root}
SSH_PORT=${SSH_PORT:-22}
PYTHON_BIN=${PYTHON_BIN:-python3.12}
ORIGIN_SSL_CERT=${ORIGIN_SSL_CERT:-}
ORIGIN_SSL_KEY=${ORIGIN_SSL_KEY:-}
PIP_INDEX_URL=${PIP_INDEX_URL:-https://mirror2.chabokan.net/pypi/simple/}
USE_SSH_PROXY=${USE_SSH_PROXY:-true}
SSH_PROXY_HOST=${SSH_PROXY_HOST:-127.0.0.1}
SSH_PROXY_PORT=${SSH_PROXY_PORT:-12334}
SSH_PROXY_TYPE=${SSH_PROXY_TYPE:-5}
SSH_PROXY_COMMAND=${SSH_PROXY_COMMAND:-}
SSH_PROXY_FDPASS=${SSH_PROXY_FDPASS:-}

if [ -z "$SSH_PROXY_COMMAND" ] && [ "$USE_SSH_PROXY" = "true" ]; then
  SSH_PROXY_COMMAND="nc -F -X $SSH_PROXY_TYPE -x $SSH_PROXY_HOST:$SSH_PROXY_PORT %h %p"
  SSH_PROXY_FDPASS=${SSH_PROXY_FDPASS:-true}
fi
SSH_PROXY_FDPASS=${SSH_PROXY_FDPASS:-false}

echo "Deploying to $SSH_USER@$SERVER_IP:$SERVER_PATH"
echo "Primary domain: $APP_DOMAIN"
echo "Extra domains: $APP_EXTRA_DOMAINS"
echo "Health check URL: $APP_URL"
echo "Systemd service: $SERVICE_NAME"
echo "Telegram polling service: $TELEGRAM_POLLING_SERVICE_NAME"
echo "Preferred Python: $PYTHON_BIN"
if [ -n "$SSH_PROXY_COMMAND" ]; then
  echo "SSH proxy: $SSH_PROXY_HOST:$SSH_PROXY_PORT"
fi

SSH_CONFIG=$(mktemp)
SSH_CONTROL_PATH=$(mktemp -u "/tmp/vpn-store-sajad-ssh-XXXXXXXX")
SSH_DEST=vpn-store-sajad-target

cleanup() {
  if [ -f "$SSH_CONFIG" ]; then
    ssh -F "$SSH_CONFIG" -O exit "$SSH_DEST" >/dev/null 2>&1 || true
  fi
  rm -f "$SSH_CONFIG" "$SSH_CONTROL_PATH"
}
trap cleanup EXIT

cat > "$SSH_CONFIG" << EOF
Host $SSH_DEST
    HostName $SERVER_IP
    User $SSH_USER
    Port $SSH_PORT
    StrictHostKeyChecking accept-new
    ConnectTimeout 60
    ConnectionAttempts 3
    ServerAliveInterval 15
    ServerAliveCountMax 2
    ControlMaster auto
    ControlPersist 10m
    ControlPath $SSH_CONTROL_PATH
EOF

if [ -n "$SSH_PROXY_COMMAND" ]; then
  printf '    ProxyCommand %s\n' "$SSH_PROXY_COMMAND" >> "$SSH_CONFIG"
  if [ "$SSH_PROXY_FDPASS" = "true" ]; then
    printf '    ProxyUseFdpass yes\n' >> "$SSH_CONFIG"
  fi
fi

SSH_CMD=(ssh -F "$SSH_CONFIG")
RSYNC_SSH="ssh -F $SSH_CONFIG"

# ===== REMOTE PREP =====
"${SSH_CMD[@]}" "$SSH_DEST" bash -se -- "$SERVER_PATH" "$PYTHON_BIN" << 'EOF'
set -Eeuo pipefail

SERVER_PATH=$1
PYTHON_BIN=$2

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y python3 python3-venv python3-pip nginx rsync curl
  if [ "$PYTHON_BIN" = "python3.12" ] && ! command -v python3.12 >/dev/null 2>&1; then
    apt-get install -y python3.12 python3.12-venv || true
  fi
fi

mkdir -p "$SERVER_PATH"
EOF

# ===== RSYNC =====
rsync -az --delete \
  -e "$RSYNC_SSH" \
  --exclude 'venv/' \
  --exclude 'node_modules/' \
  --exclude 'media/' \
  --exclude 'static_root/' \
  --exclude 'data/' \
  --exclude 'wheels/' \
  --exclude '.git/' \
  --exclude '.env' \
  --exclude '.env.*' \
  --exclude '__pycache__/' \
  --exclude 'db.sqlite3' \
  --exclude '*.pyc' \
  --exclude 'gunicorn.sock' \
  ./ "$SSH_DEST:$SERVER_PATH"

# ===== REMOTE COMMANDS =====
ORIGIN_SSL_CERT_ARG=${ORIGIN_SSL_CERT:-__auto__}
ORIGIN_SSL_KEY_ARG=${ORIGIN_SSL_KEY:-__auto__}
TELEGRAM_PROXY_URL_ARG=${TELEGRAM_PROXY_URL:-__empty__}
XUI_PANEL_PROXY_ARG=${XUI_PANEL_PROXY_URL:-__empty__}

"${SSH_CMD[@]}" "$SSH_DEST" bash -se -- \
  "$SERVER_PATH" "$APP_DOMAIN" "$APP_EXTRA_DOMAINS" "$APP_URL" "$TELEGRAM_PROXY_URL_ARG" "$XUI_PANEL_PROXY_ARG" "$SERVICE_NAME" "$TELEGRAM_POLLING_SERVICE_NAME" "$PYTHON_BIN" "$ORIGIN_SSL_CERT_ARG" "$ORIGIN_SSL_KEY_ARG" "$SERVER_IP" \
  "$TELEGRAM_PROXY_PROTOCOL" "$TELEGRAM_PROXY_HOST" "$TELEGRAM_PROXY_PORT" "$TELEGRAM_PROXY_USERNAME" "$TELEGRAM_PROXY_PASSWORD" "$PIP_INDEX_URL" << 'EOF'
set -Eeuo pipefail

SERVER_PATH=$1
APP_DOMAIN=$2
APP_EXTRA_DOMAINS=$3
APP_URL=$4
TELEGRAM_PROXY_URL=$5
XUI_PANEL_PROXY_URL=$6
SERVICE_NAME=$7
TELEGRAM_POLLING_SERVICE_NAME=$8
PYTHON_BIN=$9
ORIGIN_SSL_CERT=${10:-}
ORIGIN_SSL_KEY=${11:-}
SERVER_IP=${12:-}
TELEGRAM_PROXY_PROTOCOL=${13:-}
TELEGRAM_PROXY_HOST=${14:-}
TELEGRAM_PROXY_PORT=${15:-}
TELEGRAM_PROXY_USERNAME=${16:-}
TELEGRAM_PROXY_PASSWORD=${17:-}
PIP_INDEX_URL=${18:-}

if [ "$ORIGIN_SSL_CERT" = "__auto__" ]; then
  ORIGIN_SSL_CERT=
fi
if [ "$ORIGIN_SSL_KEY" = "__auto__" ]; then
  ORIGIN_SSL_KEY=
fi
if [ "$TELEGRAM_PROXY_URL" = "__empty__" ]; then
  TELEGRAM_PROXY_URL=
fi
if [ "$XUI_PANEL_PROXY_URL" = "__empty__" ]; then
  XUI_PANEL_PROXY_URL=
fi
if [ -n "$PIP_INDEX_URL" ]; then
  export PIP_INDEX_URL
fi
PIP_INDEX_ARGS=()
if [ -n "$PIP_INDEX_URL" ]; then
  PIP_INDEX_ARGS=(--index-url "$PIP_INDEX_URL")
fi

SERVICE_ID=${SERVICE_NAME%.service}
RUN_DIR=/run/$SERVICE_ID
SOCKET_PATH=$RUN_DIR/gunicorn.sock
ENV_FILE=$SERVER_PATH/.env

cd "$SERVER_PATH"

if command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON=$PYTHON_BIN
elif command -v python3.12 >/dev/null 2>&1; then
  PYTHON=python3.12
else
  PYTHON=python3
fi

"$PYTHON" - << 'PY'
import sys

if sys.version_info < (3, 12):
    raise SystemExit(
        "Django 6.0 requires Python 3.12+. Install python3.12/python3.12-venv "
        "or rerun with PYTHON_BIN=/path/to/python3.12."
    )
PY

if [ -d venv ]; then
  if [ ! -x venv/bin/python ] || ! venv/bin/python - << 'PY'
import sys

raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
  then
    rm -rf venv
  fi
fi

if [ ! -d venv ]; then
  "$PYTHON" -m venv venv
fi

source venv/bin/activate

python - << 'PY'
import sys

if sys.version_info < (3, 12):
    raise SystemExit("Existing venv uses Python older than 3.12.")
PY

python -m pip install "${PIP_INDEX_ARGS[@]}" --upgrade --disable-pip-version-check pip setuptools wheel
pip install "${PIP_INDEX_ARGS[@]}" --disable-pip-version-check -r requirements.txt

mkdir -p data media static_root /var/www/letsencrypt

if id -u www-data >/dev/null 2>&1; then
  WEB_USER=www-data
elif id -u nginx >/dev/null 2>&1; then
  WEB_USER=nginx
else
  WEB_USER=root
fi
WEB_GROUP=$(id -gn "$WEB_USER")

if [ -f db.sqlite3 ] && [ ! -f data/db.sqlite3 ]; then
  cp db.sqlite3 data/db.sqlite3
fi

python - "$ENV_FILE" "$SERVER_PATH" "$APP_DOMAIN" "$APP_EXTRA_DOMAINS" "$SERVER_IP" "$TELEGRAM_PROXY_URL" "$XUI_PANEL_PROXY_URL" \
  "$TELEGRAM_PROXY_PROTOCOL" "$TELEGRAM_PROXY_HOST" "$TELEGRAM_PROXY_PORT" "$TELEGRAM_PROXY_USERNAME" "$TELEGRAM_PROXY_PASSWORD" << 'PY'
import pathlib
import shlex
import sys

try:
    from django.core.management.utils import get_random_secret_key
except Exception:
    import secrets
    import string

    def get_random_secret_key():
        chars = string.ascii_letters + string.digits + "!@#$%^&*(-_=+)"
        return "".join(secrets.choice(chars) for _ in range(50))

env_path = pathlib.Path(sys.argv[1])
(
    server_path,
    domain,
    extra_domains,
    server_ip,
    telegram_proxy_url,
    xui_panel_proxy_url,
    telegram_proxy_protocol,
    telegram_proxy_host,
    telegram_proxy_port,
    telegram_proxy_username,
    telegram_proxy_password,
) = sys.argv[2:13]
data = {}


def unique_csv(*chunks):
    seen = set()
    values = []
    for chunk in chunks:
        for item in str(chunk or "").replace(" ", ",").split(","):
            value = item.strip()
            if value and value not in seen:
                seen.add(value)
                values.append(value)
    return values

if env_path.exists():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        data[key] = value.strip().strip("'").strip('"')

if not data.get("DJANGO_SECRET_KEY"):
    data["DJANGO_SECRET_KEY"] = get_random_secret_key()
allowed_hosts = unique_csv(domain, extra_domains, server_ip, "127.0.0.1", "localhost")
csrf_hosts = unique_csv(domain, extra_domains, server_ip)
csrf_origins = [f"{scheme}://{host}" for host in csrf_hosts for scheme in ("https", "http")]
data.update(
    {
        "DJANGO_SETTINGS_MODULE": "core.settings.production",
        "DJANGO_ALLOWED_HOSTS": ",".join(allowed_hosts),
        "DJANGO_CSRF_TRUSTED_ORIGINS": ",".join(csrf_origins),
        "DJANGO_SECURE_SSL_REDIRECT": "False",
        "DJANGO_SESSION_COOKIE_SECURE": "True",
        "DJANGO_CSRF_COOKIE_SECURE": "True",
        "SQLITE_DATABASE_PATH": f"{server_path}/data/db.sqlite3",
        "TELEGRAM_WEBHOOK_RESPONSE_ENABLED": "False",
    }
)
if telegram_proxy_url != "__preserve__":
    data["TELEGRAM_PROXY_URL"] = telegram_proxy_url
else:
    data.setdefault("TELEGRAM_PROXY_URL", "")
for key, value in {
    "TELEGRAM_PROXY_PROTOCOL": telegram_proxy_protocol,
    "TELEGRAM_PROXY_HOST": telegram_proxy_host,
    "TELEGRAM_PROXY_PORT": telegram_proxy_port,
    "TELEGRAM_PROXY_USERNAME": telegram_proxy_username,
    "TELEGRAM_PROXY_PASSWORD": telegram_proxy_password,
}.items():
    if value != "__preserve__":
        data[key] = value
    else:
        data.setdefault(key, "")
if xui_panel_proxy_url:
    data["XUI_PANEL_PROXY_URL"] = xui_panel_proxy_url
else:
    data.setdefault("XUI_PANEL_PROXY_URL", "")

ordered_keys = [
    "DJANGO_SETTINGS_MODULE",
    "DJANGO_SECRET_KEY",
    "DJANGO_ALLOWED_HOSTS",
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    "DJANGO_SECURE_SSL_REDIRECT",
    "DJANGO_SESSION_COOKIE_SECURE",
    "DJANGO_CSRF_COOKIE_SECURE",
    "SQLITE_DATABASE_PATH",
    "TELEGRAM_PROXY_PROTOCOL",
    "TELEGRAM_PROXY_HOST",
    "TELEGRAM_PROXY_PORT",
    "TELEGRAM_PROXY_USERNAME",
    "TELEGRAM_PROXY_PASSWORD",
    "TELEGRAM_PROXY_URL",
    "TELEGRAM_WEBHOOK_RESPONSE_ENABLED",
    "XUI_PANEL_PROXY_URL",
]
remaining_keys = sorted(key for key in data if key not in ordered_keys)

lines = ["# Managed by deploy-sajad.sh"]
for key in [*ordered_keys, *remaining_keys]:
    lines.append(f"{key}={shlex.quote(str(data[key]))}")
env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

set -a
. "$ENV_FILE"
set +a

python manage.py collectstatic --noinput

python manage.py migrate

chown -R "$WEB_USER:$WEB_GROUP" data media static_root
chmod 640 "$ENV_FILE"

python - "$SERVER_PATH" "$SERVICE_NAME" "$TELEGRAM_POLLING_SERVICE_NAME" "$SERVICE_ID" "$WEB_USER" "$WEB_GROUP" << 'PY'
import sys

server_path, service_name, telegram_polling_service_name, service_id, web_user, web_group = sys.argv[1:7]
service_path = f"/etc/systemd/system/{service_name}"
polling_service_path = f"/etc/systemd/system/{telegram_polling_service_name}"

content = f"""[Unit]
Description=Gunicorn for vpn_store sajad
After=network.target

[Service]
Type=simple
User={web_user}
Group={web_group}
WorkingDirectory={server_path}
EnvironmentFile={server_path}/.env
RuntimeDirectory={service_id}
RuntimeDirectoryMode=0755
ExecStart={server_path}/venv/bin/gunicorn --access-logfile - --workers 3 --bind unix:/run/{service_id}/gunicorn.sock core.wsgi:application
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

with open(service_path, "w", encoding="utf-8") as file:
    file.write(content)

polling_content = f"""[Unit]
Description=Telegram long polling for vpn_store sajad
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={web_user}
Group={web_group}
WorkingDirectory={server_path}
EnvironmentFile={server_path}/.env
Environment=PYTHONUNBUFFERED=1
ExecStart={server_path}/venv/bin/python manage.py run_telegram_polling
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

with open(polling_service_path, "w", encoding="utf-8") as file:
    file.write(polling_content)
PY

cat > /etc/nginx/conf.d/django-forwarded-proto.conf << 'NGINXMAP'
map $http_x_forwarded_proto $django_forwarded_proto {
    default $http_x_forwarded_proto;
    ""      $scheme;
}
NGINXMAP

if [ -z "$ORIGIN_SSL_CERT" ] || [ -z "$ORIGIN_SSL_KEY" ]; then
  SERVER_IP_DASHED=${SERVER_IP//./-}
  CERT_NAMES=$(printf '%s,%s,%s,%s\n' "$APP_DOMAIN" "$APP_EXTRA_DOMAINS" "$SERVER_IP_DASHED.sslip.io" "$SERVER_IP.sslip.io" | tr ',' ' ')
  for CERT_NAME in $CERT_NAMES; do
    CANDIDATE_CERT="/etc/letsencrypt/live/$CERT_NAME/fullchain.pem"
    CANDIDATE_KEY="/etc/letsencrypt/live/$CERT_NAME/privkey.pem"
    if [ -f "$CANDIDATE_CERT" ] && [ -f "$CANDIDATE_KEY" ]; then
      ORIGIN_SSL_CERT=$CANDIDATE_CERT
      ORIGIN_SSL_KEY=$CANDIDATE_KEY
      break
    fi
  done
fi

if [ -n "$ORIGIN_SSL_CERT" ] && [ -n "$ORIGIN_SSL_KEY" ]; then
  echo "Origin TLS: enabled with $ORIGIN_SSL_CERT"
else
  echo "Origin TLS: no certificate found; nginx will serve $APP_DOMAIN on HTTP only"
fi

python - "$SERVER_PATH" "$APP_DOMAIN" "$APP_EXTRA_DOMAINS" "$SERVER_IP" "$SOCKET_PATH" "$ORIGIN_SSL_CERT" "$ORIGIN_SSL_KEY" << 'PY'
from pathlib import Path
import sys

server_path, domain, extra_domains, server_ip, socket_path, origin_ssl_cert, origin_ssl_key = sys.argv[1:8]
nginx_path = "/etc/nginx/sites-available/vpn_store_sajad"


def unique_hosts(*chunks):
    seen = set()
    hosts = []
    for chunk in chunks:
        for item in str(chunk or "").replace(" ", ",").split(","):
            host = item.strip()
            if host and host not in seen:
                seen.add(host)
                hosts.append(host)
    return hosts


hosts = unique_hosts(domain, extra_domains, server_ip)
server_names = " ".join(hosts)

locations = f"""
    client_max_body_size 10m;

    location /static/ {{
        alias {server_path}/static_root/;
        expires 30d;
        add_header Cache-Control "public";
    }}

    location /media/ {{
        alias {server_path}/media/;
    }}

    location /.well-known/acme-challenge/ {{
        root /var/www/letsencrypt;
    }}

    location / {{
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $django_forwarded_proto;
        proxy_redirect off;
        proxy_pass http://unix:{socket_path};
    }}
"""

content = f"""server {{
    listen 80;
    server_name {server_names};
{locations}}}
"""

with open(nginx_path, "w", encoding="utf-8") as file:
    file.write(content)
PY

ln -sfn /etc/nginx/sites-available/vpn_store_sajad /etc/nginx/sites-enabled/vpn_store_sajad
rm -f /etc/nginx/sites-enabled/default
rm -f /etc/nginx/sites-enabled/vpn_store_ip
rm -f /etc/nginx/sites-enabled/vpn-store-ip
nginx -t

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl enable "$TELEGRAM_POLLING_SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
systemctl restart "$TELEGRAM_POLLING_SERVICE_NAME"

for _ in $(seq 1 20); do
  if systemctl is-active --quiet "$SERVICE_NAME" && systemctl is-active --quiet "$TELEGRAM_POLLING_SERVICE_NAME" && [ -S "$SOCKET_PATH" ]; then
    break
  fi
  sleep 1
done

systemctl is-active --quiet "$SERVICE_NAME"
systemctl is-active --quiet "$TELEGRAM_POLLING_SERVICE_NAME"
test -S "$SOCKET_PATH"

systemctl reload nginx

if command -v curl >/dev/null 2>&1; then
  HEALTH_HOSTS=$(printf '%s,%s,%s\n' "$APP_DOMAIN" "$APP_EXTRA_DOMAINS" "$SERVER_IP" | tr ',' ' ')
  for HEALTH_HOST in $HEALTH_HOSTS; do
    curl -fsSI --max-time 15 -H "Host: $HEALTH_HOST" http://127.0.0.1/ >/dev/null
  done
  curl -fsSI --max-time 15 -H "Host: $APP_DOMAIN" http://127.0.0.1/admin/ >/dev/null

  set +e
  curl -fsSI --max-time 20 "$APP_URL" >/dev/null
  PUBLIC_STATUS=$?
  set -e

  if [ "$PUBLIC_STATUS" -ne 0 ]; then
    echo "WARNING: public health check failed for $APP_URL"
    echo "Check DNS/CDN origin settings for $APP_DOMAIN -> $SERVER_IP."

    HTTP_APP_URL="http://$APP_DOMAIN/"
    if [ "$APP_URL" != "$HTTP_APP_URL" ]; then
      set +e
      curl -fsSI --max-time 20 "$HTTP_APP_URL" >/dev/null
      PUBLIC_HTTP_STATUS=$?
      set -e
      if [ "$PUBLIC_HTTP_STATUS" -eq 0 ]; then
        echo "Public HTTP check passed for $HTTP_APP_URL; HTTPS/CDN SSL still needs attention."
      fi
    fi
  fi
fi

echo "DONE"

EOF
