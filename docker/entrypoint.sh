#!/usr/bin/env bash
set -Eeuo pipefail

log() {
    printf '%s\n' "$*" >&2
}

is_true() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

require_env() {
    local name="$1"
    if [ -z "${!name:-}" ]; then
        log "Missing required environment variable: ${name}"
        exit 64
    fi
}

load_env_file() {
    local env_file="${ENV_FILE:-/app/.env}"
    if [ -f "$env_file" ]; then
        set -a
        # shellcheck disable=SC1090
        . "$env_file"
        set +a
        log "Loaded runtime environment file."
    fi
}

configure_defaults() {
    export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-core.settings.production}"
    export PORT="${PORT:-8000}"
    export REVENUE_ENGINE_DRY_RUN="${REVENUE_ENGINE_DRY_RUN:-true}"
    export QASEDAK_BOT_ENABLED="${QASEDAK_BOT_ENABLED:-true}"
    export QASEDAK_WORKER_ENABLED="${QASEDAK_WORKER_ENABLED:-true}"
    export QASEDAK_RUN_MIGRATIONS="${QASEDAK_RUN_MIGRATIONS:-true}"
    export QASEDAK_COLLECTSTATIC="${QASEDAK_COLLECTSTATIC:-true}"
    export QASEDAK_BOOTSTRAP_TENANT="${QASEDAK_BOOTSTRAP_TENANT:-false}"

    if [ -n "${DOMAIN:-}" ] && [ -z "${DJANGO_ALLOWED_HOSTS:-}" ]; then
        export DJANGO_ALLOWED_HOSTS="${DOMAIN},127.0.0.1,localhost"
    fi

    if [ -n "${DOMAIN:-}" ] && [ -z "${DJANGO_CSRF_TRUSTED_ORIGINS:-}" ]; then
        case "$DOMAIN" in
            http://*|https://*) export DJANGO_CSRF_TRUSTED_ORIGINS="$DOMAIN" ;;
            *) export DJANGO_CSRF_TRUSTED_ORIGINS="https://${DOMAIN},http://${DOMAIN}" ;;
        esac
    fi
}

validate_database_config() {
    if [ -n "${DATABASE_URL:-}" ]; then
        return
    fi

    case "${DATABASE_ENGINE:-}" in
        postgres|postgresql)
            require_env POSTGRES_HOST
            require_env POSTGRES_PASSWORD
            ;;
        sqlite|sqlite3)
            if ! is_true "${QASEDAK_ALLOW_SQLITE:-false}"; then
                log "SQLite runtime requires QASEDAK_ALLOW_SQLITE=true. Use DATABASE_URL or PostgreSQL config for production."
                exit 64
            fi
            ;;
        "")
            if [ -n "${POSTGRES_HOST:-}" ] || [ -n "${POSTGRES_PASSWORD:-}" ]; then
                export DATABASE_ENGINE=postgres
                require_env POSTGRES_HOST
                require_env POSTGRES_PASSWORD
            else
                log "Missing database configuration. Set DATABASE_URL or PostgreSQL environment variables."
                exit 64
            fi
            ;;
        *)
            log "Unsupported DATABASE_ENGINE: ${DATABASE_ENGINE}"
            exit 64
            ;;
    esac
}

validate_runtime_config() {
    require_env TENANT_ID
    require_env DJANGO_SECRET_KEY
    validate_database_config

    if is_true "$QASEDAK_BOT_ENABLED"; then
        require_env TELEGRAM_BOT_TOKEN
    fi

    if is_true "$QASEDAK_BOOTSTRAP_TENANT"; then
        require_env QASEDAK_ADMIN_USERNAME
        require_env QASEDAK_ADMIN_PASSWORD
    fi
}

run_tenant_bootstrap() {
    if ! is_true "$QASEDAK_BOOTSTRAP_TENANT"; then
        return
    fi

    local config_path
    config_path="$(mktemp)"
    python - "$config_path" <<'PY'
import json
import os
import sys

path = sys.argv[1]
tenant_id = os.environ["TENANT_ID"].strip()
domain = os.environ.get("DOMAIN", "").strip()
display_name = os.environ.get("TENANT_DISPLAY_NAME", "").strip() or f"Qasedak {tenant_id}"
timezone = os.environ.get("QASEDAK_TIMEZONE", "Asia/Tehran").strip() or "Asia/Tehran"

config = {
    "app": {
        "domain": domain,
        "timezone": timezone,
        "language": os.environ.get("QASEDAK_LANGUAGE", "fa").strip() or "fa",
    },
    "admin": {
        "username": os.environ["QASEDAK_ADMIN_USERNAME"].strip(),
        "password": os.environ["QASEDAK_ADMIN_PASSWORD"],
        "email": os.environ.get("QASEDAK_ADMIN_EMAIL", "").strip(),
    },
    "store": {
        "name": display_name,
        "english_name": os.environ.get("TENANT_ENGLISH_NAME", "").strip() or display_name,
        "slug": tenant_id,
        "domain": domain,
        "payment": {
            "card_number": "0000000000000000",
            "card_owner": "Configure Payment Owner",
        },
    },
    "telegram": {
        "enabled": False,
        "create_inactive_placeholder": True,
        "name": "Telegram setup placeholder",
    },
    "xui": {
        "configure_now": False,
    },
    "revenue_engine": {
        "enabled": True,
        "dry_run": True,
    },
}

with open(path, "w", encoding="utf-8") as handle:
    json.dump(config, handle, ensure_ascii=False)
PY

    log "Bootstrapping tenant database objects."
    python manage.py bootstrap_install --config "$config_path" --yes --no-update-existing
    rm -f "$config_path"
}

run_startup_tasks() {
    if is_true "$QASEDAK_RUN_MIGRATIONS"; then
        log "Running Django migrations."
        python manage.py migrate --noinput
    fi

    run_tenant_bootstrap

    if is_true "$QASEDAK_COLLECTSTATIC"; then
        log "Collecting static files."
        python manage.py collectstatic --noinput --verbosity 0
    fi
}

pids=()

start_process() {
    local name="$1"
    shift
    "$@" &
    pids+=("$!")
    log "Started ${name} process."
}

shutdown() {
    trap - SIGINT SIGTERM
    log "Stopping Qasedak runtime."
    for pid in "${pids[@]:-}"; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    wait "${pids[@]:-}" 2>/dev/null || true
}

load_env_file
configure_defaults

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

validate_runtime_config
run_startup_tasks

trap 'shutdown; exit 143' SIGTERM
trap 'shutdown; exit 130' SIGINT

if is_true "$QASEDAK_BOT_ENABLED"; then
    start_process "bot" /app/docker/start-bot.sh
fi

if is_true "$QASEDAK_WORKER_ENABLED"; then
    start_process "worker" /app/docker/start-worker.sh
fi

start_process "web" /app/docker/start-web.sh

set +e
wait -n "${pids[@]}"
status=$?
set -e

log "A Qasedak runtime process exited; shutting down container."
shutdown
exit "$status"
