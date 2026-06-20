#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

DRY_RUN=0
YES=0
ADVANCED=0
INSTALL_DIR=""
CONFIG_PATH=""
DEFAULT_INSTALL_DIR="/opt/qasedak"
ADMIN_PASSWORD_ENV_NAME="QASEDAK_ADMIN_PASSWORD"
TELEGRAM_BOT_TOKEN_ENV_NAME="QASEDAK_TELEGRAM_BOT_TOKEN"
XUI_PASSWORD_ENV_NAME="QASEDAK_XUI_PASSWORD"
APT_PACKAGES=(
  python3
  python3-venv
  python3-pip
  curl
  rsync
  sqlite3
  ca-certificates
  openssl
)
NGINX_PACKAGES=(nginx)
TLS_PACKAGES=(certbot python3-certbot-nginx)
SUDO_CMD=()
ORIGINAL_ARGS=()
SYSTEMD_MODE="auto"
NGINX_MODE="auto"
TLS_MODE="auto"
ENABLE_SYSTEMD=0
ENABLE_NGINX=0
ENABLE_TLS=0
RUN_LIVE_CHECKS=0
SERVICE_PREFIX="vpn-store"
WEB_SERVICE_NAME="vpn-store-web"
TELEGRAM_SERVICE_NAME="vpn-store-telegram"
NGINX_SITE_NAME="vpn-store"
GUNICORN_PORT="8000"
SERVICE_USER="root"
SERVICE_GROUP="root"
SERVER_IP=""

on_error() {
  local line="$1"
  local command="$2"
  printf 'ERROR: %s failed at line %s while running: %s\n' "$SCRIPT_NAME" "$line" "$command" >&2
}
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR

usage() {
  cat <<'EOF'
Usage:
  scripts/install.sh [--dry-run] [--yes] [--install-dir DIR] [--advanced]
  scripts/install.sh [--dry-run] [--yes] [--install-dir DIR] --config install.config.json

Options:
  --dry-run          Print planned actions only. No files, packages, DB, or network writes.
  --yes              Accept prompts and use safe defaults where prompting is needed.
  --install-dir DIR  Install target directory. Default: /opt/qasedak
  --advanced         Ask optional Store, payment, Telegram, X-UI, inbound, and plan prompts.
  --config FILE      Use an existing install config JSON instead of interactive prompts.
  --live-checks      Explicitly run Telegram/X-UI live checks during bootstrap and doctor.
  --with-systemd     Render, enable, and start systemd services.
  --without-systemd  Skip systemd services.
  --with-nginx       Render and enable nginx HTTP reverse-proxy config.
  --without-nginx    Skip nginx config.
  --with-tls         Request certbot nginx TLS when a domain and nginx are available.
  --without-tls      Skip TLS/certbot.
  --service-prefix NAME
  --web-service-name NAME
  --telegram-service-name NAME
  --nginx-site-name NAME
  -h, --help         Show this help.
EOF
}

log() {
  printf '%s\n' "$*"
}

warn() {
  printf 'WARNING: %s\n' "$*" >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

run_cmd() {
  if (( DRY_RUN )); then
    printf 'DRY-RUN:'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

confirm() {
  local question="$1"
  local default="${2:-no}"
  local answer=""

  if (( YES )); then
    return 0
  fi
  [[ -t 0 ]] || die "$question requires an interactive terminal or --yes."

  if [[ "$default" == "yes" ]]; then
    read -r -p "$question [Y/n]: " answer
    [[ -z "$answer" || "$answer" =~ ^[Yy]$ || "$answer" =~ ^[Yy][Ee][Ss]$ ]]
  else
    read -r -p "$question [y/N]: " answer
    [[ "$answer" =~ ^[Yy]$ || "$answer" =~ ^[Yy][Ee][Ss]$ ]]
  fi
}

prompt_value() {
  local label="$1"
  local default="${2:-}"
  local required="${3:-no}"
  local value=""

  if (( YES )); then
    printf '%s' "$default"
    return 0
  fi
  [[ -t 0 ]] || die "$label requires an interactive terminal or --yes."

  if [[ -n "$default" ]]; then
    read -r -p "$label [$default]: " value
    value="${value:-$default}"
  else
    read -r -p "$label: " value
  fi
  if [[ "$required" == "yes" && -z "$value" ]]; then
    die "$label is required."
  fi
  printf '%s' "$value"
}

prompt_secret_or_generate() {
  local label="$1"
  local value=""
  local confirm_value=""

  if (( YES )); then
    printf '%s' "__GENERATE__"
    return 0
  fi
  [[ -t 0 ]] || die "$label requires an interactive terminal or --yes."

  read -r -s -p "$label (leave empty to auto-generate): " value
  printf '\n'
  if [[ -z "$value" ]]; then
    printf '%s' "__GENERATE__"
    return 0
  fi
  read -r -s -p "Confirm $label: " confirm_value
  printf '\n'
  [[ "$value" == "$confirm_value" ]] || die "$label confirmation did not match."
  printf '%s' "$value"
}

prompt_secret_required() {
  local label="$1"
  local value=""

  if (( YES )); then
    die "$label is required in --yes mode. Provide --config or run interactively."
  fi
  [[ -t 0 ]] || die "$label requires an interactive terminal."
  read -r -s -p "$label: " value
  printf '\n'
  [[ -n "$value" ]] || die "$label is required."
  printf '%s' "$value"
}

prompt_yes_no() {
  local label="$1"
  local default="${2:-no}"
  if (( YES )); then
    if [[ "$default" == "yes" ]]; then
      printf '1'
    else
      printf '0'
    fi
    return 0
  fi
  if confirm "$label" "$default"; then
    printf '1'
  else
    printf '0'
  fi
}

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 36 | tr -d '\n'
  else
    python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(36), end="")
PY
  fi
}

parse_args() {
  while (($#)); do
    case "$1" in
      --dry-run)
        DRY_RUN=1
        ;;
      --yes)
        YES=1
        ;;
      --advanced)
        ADVANCED=1
        ;;
      --install-dir)
        shift
        [[ $# -gt 0 ]] || die "--install-dir requires a value."
        INSTALL_DIR="$1"
        ;;
      --config)
        shift
        [[ $# -gt 0 ]] || die "--config requires a value."
        CONFIG_PATH="$1"
        ;;
      --live-checks)
        RUN_LIVE_CHECKS=1
        ;;
      --with-systemd)
        SYSTEMD_MODE="with"
        ;;
      --without-systemd)
        SYSTEMD_MODE="without"
        ;;
      --with-nginx)
        NGINX_MODE="with"
        ;;
      --without-nginx)
        NGINX_MODE="without"
        ;;
      --with-tls)
        TLS_MODE="with"
        ;;
      --without-tls)
        TLS_MODE="without"
        ;;
      --service-prefix)
        shift
        [[ $# -gt 0 ]] || die "--service-prefix requires a value."
        SERVICE_PREFIX="$1"
        WEB_SERVICE_NAME="$SERVICE_PREFIX-web"
        TELEGRAM_SERVICE_NAME="$SERVICE_PREFIX-telegram"
        NGINX_SITE_NAME="$SERVICE_PREFIX"
        ;;
      --web-service-name)
        shift
        [[ $# -gt 0 ]] || die "--web-service-name requires a value."
        WEB_SERVICE_NAME="$1"
        ;;
      --telegram-service-name)
        shift
        [[ $# -gt 0 ]] || die "--telegram-service-name requires a value."
        TELEGRAM_SERVICE_NAME="$1"
        ;;
      --nginx-site-name)
        shift
        [[ $# -gt 0 ]] || die "--nginx-site-name requires a value."
        NGINX_SITE_NAME="$1"
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "Unknown argument: $1"
        ;;
    esac
    shift
  done
}

read_install_dir_from_config() {
  local path="$1"
  python3 - "$path" <<'PY'
import json
import sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
except Exception:
    print("")
else:
    print((data.get("app") or {}).get("install_dir") or "")
PY
}

resolve_install_dir() {
  if [[ -z "$INSTALL_DIR" && -n "$CONFIG_PATH" && -r "$CONFIG_PATH" ]] && command -v python3 >/dev/null 2>&1; then
    INSTALL_DIR="$(read_install_dir_from_config "$CONFIG_PATH")"
  fi
  INSTALL_DIR="${INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
}

load_config_metadata() {
  local path="$1"
  python3 - "$path" <<'PY'
import json
import sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
except Exception:
    print("")
    print("0")
else:
    app = data.get("app") or {}
    store = data.get("store") or {}
    print(store.get("domain") or app.get("domain") or "")
    print("1" if app.get("enable_tls") else "0")
PY
}

detect_server_ip() {
  SERVER_IP=""
  if command -v hostname >/dev/null 2>&1; then
    SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  fi
  if [[ -z "$SERVER_IP" ]] && command -v ip >/dev/null 2>&1; then
    SERVER_IP="$(ip route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}' || true)"
  fi
}

csv_join() {
  local output=""
  local item=""
  for item in "$@"; do
    [[ -n "$item" ]] || continue
    if [[ -n "$output" ]]; then
      output="$output,$item"
    else
      output="$item"
    fi
  done
  printf '%s' "$output"
}

build_allowed_hosts() {
  if [[ -n "${APP_DOMAIN:-}" ]]; then
    csv_join "$APP_DOMAIN" "$SERVER_IP" "127.0.0.1" "localhost"
  else
    csv_join "$SERVER_IP" "127.0.0.1" "localhost"
  fi
}

build_csrf_origins() {
  local tls_active="${1:-0}"
  if [[ -n "${APP_DOMAIN:-}" ]]; then
    if [[ "$tls_active" == "1" ]]; then
      printf 'https://%s' "$APP_DOMAIN"
    else
      printf 'http://%s' "$APP_DOMAIN"
    fi
  elif [[ -n "$SERVER_IP" ]]; then
    printf 'http://%s' "$SERVER_IP"
  else
    printf ''
  fi
}

preflight_root_sudo() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    SUDO_CMD=()
    return 0
  fi
  if (( ! DRY_RUN )); then
    command -v sudo >/dev/null 2>&1 || die "Run as root or install sudo."
    log "Re-running installer through sudo."
    exec sudo -E bash "$0" "${ORIGINAL_ARGS[@]}"
  fi
  if command -v sudo >/dev/null 2>&1; then
    SUDO_CMD=(sudo)
    log "DRY-RUN: sudo is available for privileged operations."
    return 0
  fi
  warn "sudo is not available; a real install would require root or sudo."
}

preflight_os() {
  if [[ ! -r /etc/os-release ]]; then
    (( DRY_RUN )) && warn "Could not read /etc/os-release." && return 0
    die "Could not read /etc/os-release."
  fi
  # shellcheck source=/dev/null
  . /etc/os-release
  local family="${ID:-} ${ID_LIKE:-}"
  if [[ "$family" != *debian* && "$family" != *ubuntu* ]]; then
    (( DRY_RUN )) && warn "This host is not detected as Ubuntu/Debian: $family" && return 0
    die "This installer supports Ubuntu/Debian only. Detected: $family"
  fi
  log "OS check: Ubuntu/Debian compatible."
}

install_base_packages() {
  local packages=("${APT_PACKAGES[@]}")
  if (( ENABLE_NGINX )); then
    packages+=("${NGINX_PACKAGES[@]}")
  fi
  if (( ENABLE_TLS )); then
    packages+=("${TLS_PACKAGES[@]}")
  fi
  log "Base packages: ${packages[*]}"
  if (( ! DRY_RUN )); then
    confirm "Install/update base packages with apt?" "yes" || die "Package installation cancelled."
  fi
  run_cmd "${SUDO_CMD[@]}" apt-get update
  run_cmd "${SUDO_CMD[@]}" apt-get install -y "${packages[@]}"
}

check_python_version() {
  if (( DRY_RUN )); then
    log "Python check: require python3 >= 3.12."
  fi
  command -v python3 >/dev/null 2>&1 || die "python3 is required."
  python3 - <<'PY'
import sys
if sys.version_info < (3, 12):
    raise SystemExit("python3 >= 3.12 is required.")
PY
}

ensure_repo_mode() {
  [[ -f "$REPO_DIR/manage.py" ]] || die "Local repo mode requires running this installer from inside the project repo."
}

ensure_can_write_file() {
  local path="$1"
  if [[ -e "$path" ]]; then
    confirm "Overwrite existing $path?" "no" || die "Refusing to overwrite $path."
  fi
}

resolve_optional_layers() {
  case "$SYSTEMD_MODE" in
    with)
      ENABLE_SYSTEMD=1
      ;;
    without)
      ENABLE_SYSTEMD=0
      ;;
    auto)
      ENABLE_SYSTEMD="$(prompt_yes_no "Create and enable systemd services?" "yes")"
      ;;
    *)
      die "Invalid systemd mode: $SYSTEMD_MODE"
      ;;
  esac

  case "$NGINX_MODE" in
    with)
      ENABLE_NGINX=1
      ;;
    without)
      ENABLE_NGINX=0
      ;;
    auto)
      ENABLE_NGINX="$(prompt_yes_no "Create and enable nginx HTTP config?" "yes")"
      ;;
    *)
      die "Invalid nginx mode: $NGINX_MODE"
      ;;
  esac

  if [[ "$TLS_MODE" == "without" ]]; then
    ENABLE_TLS=0
  elif (( ! ENABLE_NGINX )); then
    if [[ "$TLS_MODE" == "with" ]]; then
      warn "TLS requires nginx; skipping TLS."
    fi
    ENABLE_TLS=0
  elif [[ -z "${APP_DOMAIN:-}" ]]; then
    if [[ "$TLS_MODE" == "with" ]]; then
      warn "TLS requires a domain; skipping TLS for domain-less install."
    fi
    ENABLE_TLS=0
  elif [[ "$TLS_MODE" == "with" ]]; then
    ENABLE_TLS=1
  elif [[ "$TLS_MODE" == "auto" ]]; then
    ENABLE_TLS="$(prompt_yes_no "Enable TLS with certbot/nginx now?" "no")"
  else
    die "Invalid TLS mode: $TLS_MODE"
  fi

  APP_ENABLE_TLS="$ENABLE_TLS"
  log "Optional layers: systemd=$ENABLE_SYSTEMD nginx=$ENABLE_NGINX tls=$ENABLE_TLS"
}

precheck_tls_dns() {
  if (( ! ENABLE_TLS )); then
    return 0
  fi
  if [[ -z "${APP_DOMAIN:-}" ]]; then
    warn "TLS requested without a domain; TLS is disabled."
    ENABLE_TLS=0
    APP_ENABLE_TLS=0
    return 0
  fi
  if (( DRY_RUN )); then
    run_cmd getent hosts "$APP_DOMAIN"
    return 0
  fi
  if ! getent hosts "$APP_DOMAIN" >/dev/null 2>&1; then
    warn "DNS lookup failed for $APP_DOMAIN; skipping TLS. The install will continue in HTTP mode."
    ENABLE_TLS=0
    APP_ENABLE_TLS=0
  fi
}

collect_config() {
  if [[ -n "$CONFIG_PATH" ]]; then
    [[ -f "$CONFIG_PATH" ]] || die "Config file not found: $CONFIG_PATH"
    mapfile -t config_metadata < <(load_config_metadata "$CONFIG_PATH")
    APP_DOMAIN="${config_metadata[0]:-}"
    APP_ENABLE_TLS="${config_metadata[1]:-0}"
    resolve_optional_layers
    RUN_DOCTOR="$(prompt_yes_no "Run non-live doctor after install?" "yes")"
    log "Using provided install config: $CONFIG_PATH"
    return 0
  fi

  INSTALL_DIR="$(prompt_value "Install directory" "$INSTALL_DIR" "yes")"
  APP_DOMAIN="$(prompt_value "Public domain (optional)" "" "no")"
  APP_ENABLE_TLS="0"
  resolve_optional_layers
  APP_TIMEZONE="Asia/Tehran"
  APP_LANGUAGE="fa"
  ADMIN_USERNAME="$(prompt_value "Admin username" "admin" "yes")"
  ADMIN_EMAIL="$(prompt_value "Admin email (optional)" "" "no")"
  ADMIN_PASSWORD="$(prompt_secret_or_generate "Admin password")"
  DATABASE_ENGINE="sqlite"
  SQLITE_PATH="$(prompt_value "SQLite database path" "$INSTALL_DIR/data/db.sqlite3" "yes")"
  STORE_NAME="Qasedak"
  STORE_ENGLISH_NAME="Qasedak"
  STORE_CARD_NUMBER=""
  STORE_CARD_OWNER=""
  TELEGRAM_ENABLED="0"
  TELEGRAM_BOT_TOKEN=""
  TELEGRAM_BOT_USERNAME=""
  TELEGRAM_ADMIN_IDS=""
  TELEGRAM_PROXY_ENABLED="0"
  TELEGRAM_PROXY_PROTOCOL="http"
  TELEGRAM_PROXY_HOST=""
  TELEGRAM_PROXY_PORT=""
  TELEGRAM_PROXY_USERNAME=""
  TELEGRAM_PROXY_PASSWORD=""
  XUI_CONFIGURE_NOW="0"
  XUI_PANEL_NAME="Primary X-UI panel"
  XUI_PANEL_URL=""
  XUI_USERNAME=""
  XUI_PASSWORD=""
  XUI_PROXY_URL=""
  XUI_INBOUND_KEY="primary-vless"
  XUI_INBOUND_ID="1"
  XUI_INBOUND_REMARK="Primary VLESS"
  XUI_INBOUND_PROTOCOL="vless"
  XUI_INBOUND_SERVER=""
  XUI_INBOUND_PORT="443"
  XUI_INBOUND_CONFIG_PARAMS="type=tcp&security=none"
  XUI_INBOUND_NETWORK="tcp"
  XUI_INBOUND_SECURITY="none"
  PLAN_KEY="starter-30d"
  PLAN_NAME="Starter 30D"
  PLAN_TRAFFIC_GB="30"
  PLAN_DURATION_DAYS="30"
  PLAN_PRICE="100000"
  PLAN_CURRENCY="TOMAN"
  PLAN_DEVICE_LIMIT="2"

  if (( ADVANCED )); then
    STORE_NAME="$(prompt_value "Store name" "$STORE_NAME" "yes")"
    STORE_ENGLISH_NAME="$(prompt_value "Store English name" "$STORE_NAME" "yes")"

    if [[ "$(prompt_yes_no "Configure payment/card settings now? Optional." "no")" == "1" ]]; then
      STORE_CARD_NUMBER="$(prompt_value "Manual payment card number" "" "no")"
      STORE_CARD_OWNER="$(prompt_value "Manual payment card owner" "" "no")"
    else
      STORE_CARD_NUMBER="0000000000000000"
      STORE_CARD_OWNER="Configure Payment Owner"
    fi

    TELEGRAM_ENABLED="$(prompt_yes_no "Enable Telegram bot?" "no")"
    if [[ "$TELEGRAM_ENABLED" == "1" ]]; then
      TELEGRAM_BOT_TOKEN="$(prompt_secret_required "Telegram bot token")"
      TELEGRAM_BOT_USERNAME="$(prompt_value "Telegram bot username without @" "" "yes")"
      TELEGRAM_ADMIN_IDS="$(prompt_value "Telegram admin IDs, comma-separated" "" "yes")"
      TELEGRAM_PROXY_ENABLED="$(prompt_yes_no "Use Telegram proxy?" "no")"
      if [[ "$TELEGRAM_PROXY_ENABLED" == "1" ]]; then
        TELEGRAM_PROXY_PROTOCOL="$(prompt_value "Telegram proxy protocol" "http" "yes")"
        TELEGRAM_PROXY_HOST="$(prompt_value "Telegram proxy host" "" "yes")"
        TELEGRAM_PROXY_PORT="$(prompt_value "Telegram proxy port" "" "yes")"
        TELEGRAM_PROXY_USERNAME="$(prompt_value "Telegram proxy username (optional)" "" "no")"
        TELEGRAM_PROXY_PASSWORD="$(prompt_secret_or_generate "Telegram proxy password (leave empty only if proxy has a password)")"
        [[ "$TELEGRAM_PROXY_PASSWORD" != "__GENERATE__" ]] || TELEGRAM_PROXY_PASSWORD=""
      fi
    fi

    XUI_CONFIGURE_NOW="$(prompt_yes_no "Configure X-UI panel now?" "no")"
    if [[ "$XUI_CONFIGURE_NOW" == "1" ]]; then
      XUI_PANEL_NAME="$(prompt_value "X-UI panel name" "$XUI_PANEL_NAME" "yes")"
      XUI_PANEL_URL="$(prompt_value "X-UI panel URL" "" "yes")"
      XUI_USERNAME="$(prompt_value "X-UI username" "" "yes")"
      XUI_PASSWORD="$(prompt_secret_required "X-UI password")"
      XUI_PROXY_URL="$(prompt_value "X-UI panel proxy URL (optional)" "" "no")"
      XUI_INBOUND_KEY="$(prompt_value "Inbound key" "$XUI_INBOUND_KEY" "yes")"
      XUI_INBOUND_ID="$(prompt_value "X-UI inbound ID" "$XUI_INBOUND_ID" "yes")"
      XUI_INBOUND_REMARK="$(prompt_value "Inbound remark" "$XUI_INBOUND_REMARK" "yes")"
      XUI_INBOUND_SERVER="$(prompt_value "Inbound server IP/domain" "${APP_DOMAIN:-vpn.example.com}" "yes")"
      XUI_INBOUND_PORT="$(prompt_value "Inbound port" "$XUI_INBOUND_PORT" "yes")"
      XUI_INBOUND_CONFIG_PARAMS="$(prompt_value "Inbound config params" "$XUI_INBOUND_CONFIG_PARAMS" "yes")"
      PLAN_KEY="$(prompt_value "Plan key" "$PLAN_KEY" "yes")"
      PLAN_NAME="$(prompt_value "Plan name" "$PLAN_NAME" "yes")"
      PLAN_TRAFFIC_GB="$(prompt_value "Plan traffic GB" "$PLAN_TRAFFIC_GB" "yes")"
      PLAN_DURATION_DAYS="$(prompt_value "Plan duration days" "$PLAN_DURATION_DAYS" "yes")"
      PLAN_PRICE="$(prompt_value "Plan price" "$PLAN_PRICE" "yes")"
      PLAN_DEVICE_LIMIT="$(prompt_value "Plan device limit" "$PLAN_DEVICE_LIMIT" "yes")"
    fi
  fi

  REVENUE_ENGINE_ENABLED="1"
  REVENUE_ENGINE_DRY_RUN="1"
  RUN_DOCTOR="$(prompt_yes_no "Run non-live doctor after install?" "yes")"
  log "Revenue Engine is enabled and locked to dry-run for fresh installs."
}

materialize_generated_secrets() {
  if [[ "${ADMIN_PASSWORD:-}" == "__GENERATE__" ]]; then
    ADMIN_PASSWORD="$(generate_secret)"
    log "Admin password generated and stored in .env as $ADMIN_PASSWORD_ENV_NAME. It was not printed."
  fi
}

prepare_install_tree() {
  log "Preparing install directory: $INSTALL_DIR"
  run_cmd "${SUDO_CMD[@]}" install -d -m 0755 "$INSTALL_DIR"
  for dirname in data media static_root backups logs; do
    run_cmd "${SUDO_CMD[@]}" install -d -m 0755 "$INSTALL_DIR/$dirname"
  done
}

rsync_repo() {
  ensure_repo_mode
  log "Copying local repo to install directory with rsync excludes."
  run_cmd rsync -a \
    --exclude '.git' \
    --exclude '.env' \
    --exclude 'install.config.json' \
    --exclude 'data/' \
    --exclude 'db.sqlite3' \
    --exclude 'db.sqlite3.backup' \
    --exclude 'media/' \
    --exclude 'venv/' \
    --exclude '.venv/' \
    --exclude 'backups/' \
    --exclude 'logs/' \
    --exclude 'node_modules/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    "$REPO_DIR/" "$INSTALL_DIR/"
}

write_env_file() {
  local target="$INSTALL_DIR/.env"
  if (( DRY_RUN )); then
    log "DRY-RUN: would write $target with mode 600 (secrets redacted)."
    return 0
  fi
  ensure_can_write_file "$target"
  materialize_generated_secrets

  local tls_active="${ENABLE_TLS:-0}"
  local allowed_hosts
  local csrf_origins
  allowed_hosts="$(build_allowed_hosts)"
  csrf_origins="$(build_csrf_origins "$tls_active")"
  local ssl_redirect="False"
  local session_secure="False"
  local csrf_secure="False"
  if [[ "$tls_active" == "1" ]]; then
    ssl_redirect="True"
    session_secure="True"
    csrf_secure="True"
  fi

  umask 077
  {
    printf '%s=%q\n' DJANGO_SETTINGS_MODULE "core.settings.production"
    printf '%s=%q\n' DJANGO_SECRET_KEY "$(generate_secret)"
    printf '%s=%q\n' DJANGO_DEBUG "False"
    printf '%s=%q\n' DJANGO_ALLOWED_HOSTS "$allowed_hosts"
    printf '%s=%q\n' DJANGO_CSRF_TRUSTED_ORIGINS "$csrf_origins"
    printf '%s=%q\n' DJANGO_USE_X_FORWARDED_HOST "True"
    printf '%s=%q\n' SQLITE_DATABASE_PATH "${SQLITE_PATH:-$INSTALL_DIR/data/db.sqlite3}"
    printf '%s=%q\n' DJANGO_LANGUAGE_CODE "${APP_LANGUAGE:-fa}"
    printf '%s=%q\n' DJANGO_TIME_ZONE "${APP_TIMEZONE:-Asia/Tehran}"
    printf '%s=%q\n' "$ADMIN_PASSWORD_ENV_NAME" "$ADMIN_PASSWORD"
    if [[ "${TELEGRAM_ENABLED:-0}" == "1" ]]; then
      printf '%s=%q\n' "$TELEGRAM_BOT_TOKEN_ENV_NAME" "$TELEGRAM_BOT_TOKEN"
      printf '%s=%q\n' TELEGRAM_BOT_USERNAME "$TELEGRAM_BOT_USERNAME"
    else
      printf '%s=%q\n' TELEGRAM_BOT_USERNAME ""
    fi
    printf '%s=%q\n' TELEGRAM_WEBHOOK_RESPONSE_ENABLED "False"
    printf '%s=%q\n' BOT_API_CONNECT_TIMEOUT_SECONDS "3"
    printf '%s=%q\n' BOT_API_READ_TIMEOUT_SECONDS "8"
    printf '%s=%q\n' TELEGRAM_PROXY_URL ""
    printf '%s=%q\n' TELEGRAM_PROXY_PROTOCOL "$TELEGRAM_PROXY_PROTOCOL"
    printf '%s=%q\n' TELEGRAM_PROXY_HOST "$TELEGRAM_PROXY_HOST"
    printf '%s=%q\n' TELEGRAM_PROXY_PORT "$TELEGRAM_PROXY_PORT"
    printf '%s=%q\n' TELEGRAM_PROXY_USERNAME "$TELEGRAM_PROXY_USERNAME"
    printf '%s=%q\n' TELEGRAM_PROXY_PASSWORD "$TELEGRAM_PROXY_PASSWORD"
    printf '%s=%q\n' XUI_PANEL_PROXY_URL ""
    if [[ "${XUI_CONFIGURE_NOW:-0}" == "1" ]]; then
      printf '%s=%q\n' "$XUI_PASSWORD_ENV_NAME" "$XUI_PASSWORD"
    fi
    printf '%s=%q\n' SMSFORWARDER_WEBHOOK_TOKEN ""
    printf '%s=%q\n' PAYMENT_SMS_TIME_ZONE "${APP_TIMEZONE:-Asia/Tehran}"
    printf '%s=%q\n' PAYMENT_RECEIPT_MAX_UPLOAD_SIZE "5242880"
    printf '%s=%q\n' INSTALL_REVENUE_ENGINE_ENABLED "true"
    printf '%s=%q\n' INSTALL_REVENUE_ENGINE_DRY_RUN "true"
    printf '%s=%q\n' INSTALL_RUN_LIVE_BOT_CHECK "false"
    printf '%s=%q\n' INSTALL_RUN_LIVE_XUI_CHECK "false"
    printf '%s=%q\n' INSTALL_SEND_TELEGRAM_TEST_MESSAGE "false"
    printf '%s=%q\n' DJANGO_SECURE_SSL_REDIRECT "$ssl_redirect"
    printf '%s=%q\n' DJANGO_SESSION_COOKIE_SECURE "$session_secure"
    printf '%s=%q\n' DJANGO_CSRF_COOKIE_SECURE "$csrf_secure"
    printf '%s=%q\n' DJANGO_SECURE_HSTS_SECONDS "0"
    printf '%s=%q\n' DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS "False"
    printf '%s=%q\n' DJANGO_SECURE_HSTS_PRELOAD "False"
  } > "$target"
  chmod 600 "$target"
  log "Wrote $target with mode 600."
}

write_env_file_from_config() {
  local target="$INSTALL_DIR/.env"
  if (( DRY_RUN )); then
    log "DRY-RUN: would write $target from $CONFIG_PATH with mode 600 (secrets redacted)."
    return 0
  fi
  ensure_can_write_file "$target"
  CONFIG_SOURCE="$CONFIG_PATH" INSTALL_TARGET="$INSTALL_DIR" INSTALL_SERVER_IP="$SERVER_IP" INSTALL_TLS_ACTIVE="${ENABLE_TLS:-0}" python3 - "$target" <<'PY'
import json
import os
import secrets
import shlex
import sys

target = sys.argv[1]
with open(os.environ["CONFIG_SOURCE"], encoding="utf-8") as handle:
    config = json.load(handle)

app = config.get("app") or {}
store = config.get("store") or {}
telegram = config.get("telegram") or {}
xui = config.get("xui") or {}
database = config.get("database") or {}
domain = store.get("domain") or app.get("domain") or ""
enable_tls = os.environ.get("INSTALL_TLS_ACTIVE") == "1"
install_dir = os.environ["INSTALL_TARGET"]
sqlite_path = database.get("sqlite_path") or os.path.join(install_dir, "data", "db.sqlite3")
server_ip = os.environ.get("INSTALL_SERVER_IP", "")
allowed_parts = []
if domain:
    allowed_parts.append(domain)
if server_ip:
    allowed_parts.append(server_ip)
allowed_parts.extend(["127.0.0.1", "localhost"])
allowed_hosts = ",".join(allowed_parts)
csrf_origins = ""
if domain:
    csrf_origins = f"{'https' if enable_tls else 'http'}://{domain}"
elif server_ip:
    csrf_origins = f"http://{server_ip}"

values = {
    "DJANGO_SETTINGS_MODULE": "core.settings.production",
    "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
    "DJANGO_DEBUG": "False",
    "DJANGO_ALLOWED_HOSTS": allowed_hosts,
    "DJANGO_CSRF_TRUSTED_ORIGINS": csrf_origins,
    "DJANGO_USE_X_FORWARDED_HOST": "True",
    "SQLITE_DATABASE_PATH": sqlite_path,
    "DJANGO_LANGUAGE_CODE": app.get("language") or "fa",
    "DJANGO_TIME_ZONE": app.get("timezone") or "Asia/Tehran",
    "TELEGRAM_BOT_USERNAME": telegram.get("bot_username") or "",
    "TELEGRAM_WEBHOOK_RESPONSE_ENABLED": "False",
    "BOT_API_CONNECT_TIMEOUT_SECONDS": "3",
    "BOT_API_READ_TIMEOUT_SECONDS": "8",
    "TELEGRAM_PROXY_URL": "",
    "TELEGRAM_PROXY_PROTOCOL": (telegram.get("proxy") or {}).get("protocol") or "http",
    "TELEGRAM_PROXY_HOST": (telegram.get("proxy") or {}).get("host") or "",
    "TELEGRAM_PROXY_PORT": (telegram.get("proxy") or {}).get("port") or "",
    "TELEGRAM_PROXY_USERNAME": (telegram.get("proxy") or {}).get("username") or "",
    "TELEGRAM_PROXY_PASSWORD": (telegram.get("proxy") or {}).get("password") or "",
    "XUI_PANEL_PROXY_URL": "",
    "SMSFORWARDER_WEBHOOK_TOKEN": "",
    "PAYMENT_SMS_TIME_ZONE": app.get("timezone") or "Asia/Tehran",
    "PAYMENT_RECEIPT_MAX_UPLOAD_SIZE": "5242880",
    "INSTALL_REVENUE_ENGINE_ENABLED": "true",
    "INSTALL_REVENUE_ENGINE_DRY_RUN": "true",
    "INSTALL_RUN_LIVE_BOT_CHECK": "false",
    "INSTALL_RUN_LIVE_XUI_CHECK": "false",
    "INSTALL_SEND_TELEGRAM_TEST_MESSAGE": "false",
    "DJANGO_SECURE_SSL_REDIRECT": "True" if enable_tls else "False",
    "DJANGO_SESSION_COOKIE_SECURE": "True" if enable_tls else "False",
    "DJANGO_CSRF_COOKIE_SECURE": "True" if enable_tls else "False",
    "DJANGO_SECURE_HSTS_SECONDS": "0",
    "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS": "False",
    "DJANGO_SECURE_HSTS_PRELOAD": "False",
}

for section, field in ((config.get("admin") or {}, "password_env"), (telegram, "bot_token_env"), (xui, "password_env")):
    env_name = section.get(field)
    if env_name and os.environ.get(env_name):
        values[env_name] = os.environ[env_name]

with open(target, "w", encoding="utf-8") as handle:
    for key, value in values.items():
        handle.write(f"{key}={shlex.quote(str(value))}\n")
os.chmod(target, 0o600)
PY
  log "Wrote $target with mode 600."
}

write_generated_config() {
  local target="$INSTALL_DIR/install.config.json"
  if (( DRY_RUN )); then
    log "DRY-RUN: would write $target with mode 600 (secrets redacted)."
    return 0
  fi
  ensure_can_write_file "$target"

  export APP_INSTALL_DIR="$INSTALL_DIR"
  export APP_DOMAIN APP_ENABLE_TLS APP_TIMEZONE APP_LANGUAGE
  export ADVANCED ADMIN_PASSWORD_ENV_NAME TELEGRAM_BOT_TOKEN_ENV_NAME XUI_PASSWORD_ENV_NAME
  export ADMIN_USERNAME ADMIN_EMAIL
  export DATABASE_ENGINE SQLITE_PATH
  export STORE_NAME STORE_ENGLISH_NAME STORE_CARD_NUMBER STORE_CARD_OWNER
  export TELEGRAM_ENABLED TELEGRAM_BOT_USERNAME TELEGRAM_ADMIN_IDS TELEGRAM_PROXY_ENABLED
  export TELEGRAM_PROXY_PROTOCOL TELEGRAM_PROXY_HOST TELEGRAM_PROXY_PORT TELEGRAM_PROXY_USERNAME
  export XUI_CONFIGURE_NOW XUI_PANEL_NAME XUI_PANEL_URL XUI_USERNAME XUI_PROXY_URL
  export XUI_INBOUND_KEY XUI_INBOUND_ID XUI_INBOUND_REMARK XUI_INBOUND_PROTOCOL XUI_INBOUND_SERVER
  export XUI_INBOUND_PORT XUI_INBOUND_CONFIG_PARAMS XUI_INBOUND_NETWORK XUI_INBOUND_SECURITY
  export PLAN_KEY PLAN_NAME PLAN_TRAFFIC_GB PLAN_DURATION_DAYS PLAN_PRICE PLAN_CURRENCY PLAN_DEVICE_LIMIT

  python3 - "$target" <<'PY'
import json
import os
import re
import sys

target = sys.argv[1]

def env(name, default=""):
    return os.environ.get(name, default)

def enabled(name):
    return env(name) == "1"

def slug(value, default):
    text = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return text or default

advanced = enabled("ADVANCED")
xui_enabled = enabled("XUI_CONFIGURE_NOW")
telegram_enabled = enabled("TELEGRAM_ENABLED")
store_name = env("STORE_NAME", "Qasedak")
config = {
    "app": {
        "install_dir": env("APP_INSTALL_DIR"),
        "domain": env("APP_DOMAIN"),
        "enable_tls": enabled("APP_ENABLE_TLS"),
        "timezone": env("APP_TIMEZONE", "Asia/Tehran"),
        "language": env("APP_LANGUAGE", "fa"),
    },
    "admin": {
        "username": env("ADMIN_USERNAME", "admin"),
        "email": env("ADMIN_EMAIL"),
        "password_env": env("ADMIN_PASSWORD_ENV_NAME", "QASEDAK_ADMIN_PASSWORD"),
    },
    "database": {
        "engine": "sqlite",
        "sqlite_path": env("SQLITE_PATH"),
    },
    "store": {
        "name": store_name,
        "english_name": env("STORE_ENGLISH_NAME", store_name),
    },
    "telegram": {
        "enabled": telegram_enabled,
    },
    "xui": {
        "configure_now": xui_enabled,
    },
    "revenue_engine": {
        "enabled": True,
        "dry_run": True,
    },
}

if advanced:
    plan = {
        "key": env("PLAN_KEY", "starter-30d"),
        "name": env("PLAN_NAME", "Starter 30D"),
        "traffic_gb": env("PLAN_TRAFFIC_GB", "30"),
        "duration_days": int(env("PLAN_DURATION_DAYS", "30")),
        "price": int(env("PLAN_PRICE", "100000")),
        "currency": env("PLAN_CURRENCY", "TOMAN"),
        "device_limit": int(env("PLAN_DEVICE_LIMIT", "2")),
        "is_public": True,
        "sort_order": 10,
    }
    config["store"].update({
        "slug": slug(env("STORE_ENGLISH_NAME", store_name), "default-store"),
        "domain": env("APP_DOMAIN"),
        "payment_mode": "manual_card",
        "card_number": env("STORE_CARD_NUMBER", "0000000000000000"),
        "card_owner": env("STORE_CARD_OWNER", "Configure Payment Owner"),
        "receipt_image_only_payment": False,
    })
    config["telegram"].update({
        "bot_token_env": env("TELEGRAM_BOT_TOKEN_ENV_NAME", "QASEDAK_TELEGRAM_BOT_TOKEN") if telegram_enabled else "",
        "bot_username": env("TELEGRAM_BOT_USERNAME"),
        "admin_ids": [item.strip() for item in env("TELEGRAM_ADMIN_IDS").split(",") if item.strip()],
        "proxy_enabled": enabled("TELEGRAM_PROXY_ENABLED"),
        "proxy": {
            "protocol": env("TELEGRAM_PROXY_PROTOCOL", "http"),
            "host": env("TELEGRAM_PROXY_HOST"),
            "port": env("TELEGRAM_PROXY_PORT"),
            "username": env("TELEGRAM_PROXY_USERNAME"),
            "password_env": "TELEGRAM_PROXY_PASSWORD" if env("TELEGRAM_PROXY_USERNAME") else "",
        },
    })
    config["xui"].update({
        "name": env("XUI_PANEL_NAME", "Primary X-UI panel"),
        "panel_url": env("XUI_PANEL_URL"),
        "username": env("XUI_USERNAME"),
        "password_env": env("XUI_PASSWORD_ENV_NAME", "QASEDAK_XUI_PASSWORD") if xui_enabled else "",
        "proxy_url": env("XUI_PROXY_URL"),
        "inbounds": [],
    })
    config["plans"] = [plan]
    config["plan_routes"] = []
    config["revenue_engine"].update({
        "daily_per_user": 1,
        "weekly_per_user": 3,
        "total_daily_cap": 100,
        "cooldown_hours": 24,
        "retention_cooldown_hours": 72,
        "min_ai_confidence": "0.50",
    })

if advanced and xui_enabled:
    config["xui"]["inbounds"].append({
        "key": env("XUI_INBOUND_KEY", "primary-vless"),
        "inbound_id": int(env("XUI_INBOUND_ID", "1")),
        "remark": env("XUI_INBOUND_REMARK", "Primary VLESS"),
        "protocol": env("XUI_INBOUND_PROTOCOL", "vless"),
        "server_ip": env("XUI_INBOUND_SERVER"),
        "port": env("XUI_INBOUND_PORT", "443"),
        "config_params": env("XUI_INBOUND_CONFIG_PARAMS", "type=tcp&security=none"),
        "network_type": env("XUI_INBOUND_NETWORK", "tcp"),
        "security": env("XUI_INBOUND_SECURITY", "none"),
        "available_for_new_orders": True,
        "health_monitor_enabled": True,
    })
    config["plan_routes"].append({
        "plan": plan["key"],
        "inbound": env("XUI_INBOUND_KEY", "primary-vless"),
        "priority": 100,
        "weight": 1,
    })

with open(target, "w", encoding="utf-8") as handle:
    json.dump(config, handle, indent=2, ensure_ascii=True)
    handle.write("\n")
os.chmod(target, 0o600)
PY
  log "Wrote $target with mode 600."
}

copy_config_file() {
  local target="$INSTALL_DIR/install.config.json"
  if (( DRY_RUN )); then
    log "DRY-RUN: would copy $CONFIG_PATH to $target with mode 600."
    return 0
  fi
  ensure_can_write_file "$target"
  install -m 600 "$CONFIG_PATH" "$target"
  log "Copied $CONFIG_PATH to $target with mode 600."
}

write_runtime_files() {
  if [[ -n "$CONFIG_PATH" ]]; then
    write_env_file_from_config
    copy_config_file
  else
    write_env_file
    write_generated_config
  fi
}

source_env() {
  if (( DRY_RUN )); then
    log "DRY-RUN: would source $INSTALL_DIR/.env."
    return 0
  fi
  set -a
  # shellcheck source=/dev/null
  . "$INSTALL_DIR/.env"
  set +a
}

django_setup() {
  log "Setting up Python/Django inside $INSTALL_DIR."
  run_cmd python3 -m venv "$INSTALL_DIR/venv"
  run_cmd "$INSTALL_DIR/venv/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt"
  source_env
  run_cmd "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/manage.py" check
  run_cmd "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/manage.py" migrate
  run_cmd "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/manage.py" collectstatic --noinput
  if [[ "${RUN_LIVE_CHECKS:-0}" == "1" ]]; then
    run_cmd "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/manage.py" bootstrap_install --config "$INSTALL_DIR/install.config.json" --yes --live-check
  else
    run_cmd "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/manage.py" bootstrap_install --config "$INSTALL_DIR/install.config.json" --yes
  fi
}

sed_escape() {
  printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

template_source() {
  local relative="$1"
  if [[ -f "$INSTALL_DIR/$relative" ]]; then
    printf '%s' "$INSTALL_DIR/$relative"
  else
    printf '%s' "$REPO_DIR/$relative"
  fi
}

backup_existing_path() {
  local target="$1"
  if [[ -e "$target" || -L "$target" ]]; then
    confirm "Overwrite existing $target? A timestamped backup will be created." "no" || die "Refusing to overwrite $target."
    local backup="$target.backup.$(date +%Y%m%d%H%M%S)"
    run_cmd "${SUDO_CMD[@]}" cp -a "$target" "$backup"
    run_cmd "${SUDO_CMD[@]}" rm -f "$target"
  fi
}

render_template() {
  local template="$1"
  local target="$2"
  local mode="${3:-0644}"
  local server_name="_"
  if [[ -n "${APP_DOMAIN:-}" ]]; then
    server_name="$APP_DOMAIN"
  fi

  if (( DRY_RUN )); then
    log "DRY-RUN: would render $template to $target with mode $mode."
    return 0
  fi

  [[ -f "$template" ]] || die "Template not found: $template"
  local tmp
  tmp="$(mktemp)"
  sed \
    -e "s|{{INSTALL_DIR}}|$(sed_escape "$INSTALL_DIR")|g" \
    -e "s|{{GUNICORN_PORT}}|$(sed_escape "$GUNICORN_PORT")|g" \
    -e "s|{{SERVICE_USER}}|$(sed_escape "$SERVICE_USER")|g" \
    -e "s|{{SERVICE_GROUP}}|$(sed_escape "$SERVICE_GROUP")|g" \
    -e "s|{{WEB_SERVICE_NAME}}|$(sed_escape "$WEB_SERVICE_NAME")|g" \
    -e "s|{{TELEGRAM_SERVICE_NAME}}|$(sed_escape "$TELEGRAM_SERVICE_NAME")|g" \
    -e "s|{{DOMAIN}}|$(sed_escape "${APP_DOMAIN:-}")|g" \
    -e "s|{{EXTRA_DOMAINS}}||g" \
    -e "s|{{NGINX_SERVER_NAME}}|$(sed_escape "$server_name")|g" \
    "$template" > "$tmp"
  backup_existing_path "$target"
  run_cmd "${SUDO_CMD[@]}" install -m "$mode" "$tmp" "$target"
  rm -f "$tmp"
}

configure_systemd() {
  if (( ! ENABLE_SYSTEMD )); then
    log "Systemd layer skipped."
    return 0
  fi
  log "Configuring systemd services: $WEB_SERVICE_NAME, $TELEGRAM_SERVICE_NAME"
  local web_template telegram_template
  web_template="$(template_source "scripts/templates/systemd/vpn-store-web.service.template")"
  telegram_template="$(template_source "scripts/templates/systemd/vpn-store-telegram.service.template")"
  render_template "$web_template" "/etc/systemd/system/$WEB_SERVICE_NAME.service" "0644"
  render_template "$telegram_template" "/etc/systemd/system/$TELEGRAM_SERVICE_NAME.service" "0644"
  run_cmd "${SUDO_CMD[@]}" systemctl daemon-reload
  run_cmd "${SUDO_CMD[@]}" systemctl enable --now "$WEB_SERVICE_NAME.service"
  run_cmd "${SUDO_CMD[@]}" systemctl enable --now "$TELEGRAM_SERVICE_NAME.service"
  run_cmd "${SUDO_CMD[@]}" systemctl status --no-pager "$WEB_SERVICE_NAME.service"
  run_cmd "${SUDO_CMD[@]}" systemctl status --no-pager "$TELEGRAM_SERVICE_NAME.service"
}

configure_nginx() {
  if (( ! ENABLE_NGINX )); then
    log "Nginx layer skipped."
    return 0
  fi
  log "Configuring nginx site: $NGINX_SITE_NAME"
  local nginx_template available_path enabled_path
  nginx_template="$(template_source "scripts/templates/nginx/vpn-store.conf.template")"
  available_path="/etc/nginx/sites-available/$NGINX_SITE_NAME.conf"
  enabled_path="/etc/nginx/sites-enabled/$NGINX_SITE_NAME.conf"
  render_template "$nginx_template" "$available_path" "0644"
  if (( DRY_RUN )); then
    log "DRY-RUN: would symlink $available_path to $enabled_path."
  else
    backup_existing_path "$enabled_path"
    run_cmd "${SUDO_CMD[@]}" ln -s "$available_path" "$enabled_path"
  fi
  run_cmd "${SUDO_CMD[@]}" nginx -t
  run_cmd "${SUDO_CMD[@]}" systemctl reload nginx
}

update_env_for_tls_success() {
  if (( DRY_RUN )); then
    log "DRY-RUN: would update $INSTALL_DIR/.env for successful TLS."
    return 0
  fi
  ENV_PATH="$INSTALL_DIR/.env" APP_DOMAIN="$APP_DOMAIN" python3 - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["ENV_PATH"])
domain = os.environ["APP_DOMAIN"]
updates = {
    "DJANGO_CSRF_TRUSTED_ORIGINS": f"https://{domain}",
    "DJANGO_SECURE_SSL_REDIRECT": "True",
    "DJANGO_SESSION_COOKIE_SECURE": "True",
    "DJANGO_CSRF_COOKIE_SECURE": "True",
}
lines = path.read_text(encoding="utf-8").splitlines()
seen = set()
output = []
for line in lines:
    key = line.split("=", 1)[0] if "=" in line else ""
    if key in updates:
        output.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        output.append(line)
for key, value in updates.items():
    if key not in seen:
        output.append(f"{key}={value}")
path.write_text("\n".join(output) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
}

configure_tls() {
  if (( ! ENABLE_TLS )); then
    log "TLS layer skipped."
    return 0
  fi
  if [[ -z "${APP_DOMAIN:-}" ]]; then
    warn "TLS requested without a domain; skipping TLS."
    return 0
  fi
  if (( ! ENABLE_NGINX )); then
    warn "TLS requires nginx; skipping TLS."
    return 0
  fi

  run_cmd getent hosts "$APP_DOMAIN"
  local certbot_args=(certbot --nginx -d "$APP_DOMAIN" --non-interactive --agree-tos --redirect --no-eff-email)
  if [[ -n "${ADMIN_EMAIL:-}" ]]; then
    certbot_args+=(--email "$ADMIN_EMAIL")
  else
    certbot_args+=(--register-unsafely-without-email)
  fi
  run_cmd "${SUDO_CMD[@]}" "${certbot_args[@]}"
  update_env_for_tls_success
  run_cmd "${SUDO_CMD[@]}" systemctl reload nginx
}

run_doctor_if_requested() {
  if [[ "${RUN_DOCTOR:-1}" != "1" ]]; then
    log "Doctor skipped by operator choice."
    return 0
  fi
  local args=("$INSTALL_DIR/scripts/doctor.sh" --install-dir "$INSTALL_DIR")
  if (( ENABLE_SYSTEMD )); then
    args+=(--systemd --web-service-name "$WEB_SERVICE_NAME" --telegram-service-name "$TELEGRAM_SERVICE_NAME")
  fi
  if (( ENABLE_NGINX )); then
    args+=(--nginx --nginx-site-name "$NGINX_SITE_NAME")
  fi
  if [[ "${RUN_LIVE_CHECKS:-0}" == "1" ]]; then
    args+=(--live-bot --live-xui)
  fi
  run_cmd "${args[@]}"
}

print_post_install_next_steps() {
  cat <<'EOF'
Install complete.
Business setup may still be incomplete.

Open Django Admin and complete docs/POST_INSTALL_SETUP.md:
- Store identity and payment/card settings
- Telegram BotConfiguration and optional proxy/force-join settings
- X-UI/Sanaei Panel, Inbound, Plan, and PlanInboundRoute
- Non-live doctor/check_integrations
- Test purchase
- Revenue Engine dry-run review before any real sends
EOF
}

main() {
  ORIGINAL_ARGS=("$@")
  parse_args "$@"
  resolve_install_dir
  preflight_root_sudo
  preflight_os
  collect_config
  detect_server_ip
  precheck_tls_dns
  install_base_packages
  check_python_version
  prepare_install_tree
  rsync_repo
  write_runtime_files
  django_setup
  configure_systemd
  configure_nginx
  configure_tls
  run_doctor_if_requested
  print_post_install_next_steps
}

main "$@"
