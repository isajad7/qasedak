#!/usr/bin/env bash
set -Eeuo pipefail

export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-core.settings.production}"
export REVENUE_ENGINE_DRY_RUN="${REVENUE_ENGINE_DRY_RUN:-true}"

is_true() {
    case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|on) return 0 ;;
        *) return 1 ;;
    esac
}

if [ -n "${QASEDAK_WORKER_COMMAND:-}" ]; then
    exec bash -lc "$QASEDAK_WORKER_COMMAND"
fi

interval="${QASEDAK_WORKER_INTERVAL_SECONDS:-3600}"
case "$interval" in
    ''|*[!0-9]*)
        printf '%s\n' "QASEDAK_WORKER_INTERVAL_SECONDS must be a positive integer." >&2
        exit 64
        ;;
esac

if [ "$interval" -lt 1 ]; then
    printf '%s\n' "QASEDAK_WORKER_INTERVAL_SECONDS must be greater than zero." >&2
    exit 64
fi

while true; do
    if is_true "$REVENUE_ENGINE_DRY_RUN"; then
        python manage.py run_revenue_scan --dry-run
    else
        python manage.py run_revenue_scan
    fi
    sleep "$interval"
done
