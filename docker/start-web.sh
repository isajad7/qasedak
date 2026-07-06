#!/usr/bin/env bash
set -Eeuo pipefail

export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-core.settings.production}"
export PORT="${PORT:-8000}"

bind="${GUNICORN_BIND:-0.0.0.0:${PORT}}"
workers="${GUNICORN_WORKERS:-3}"
timeout="${GUNICORN_TIMEOUT:-120}"
graceful_timeout="${GUNICORN_GRACEFUL_TIMEOUT:-30}"

exec gunicorn core.wsgi:application \
    --bind "$bind" \
    --workers "$workers" \
    --timeout "$timeout" \
    --graceful-timeout "$graceful_timeout" \
    --access-logfile - \
    --error-logfile -
