#!/usr/bin/env bash
set -Eeuo pipefail

export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-core.settings.production}"

if [ -n "${QASEDAK_BOT_ARGS:-}" ]; then
    # shellcheck disable=SC2086
    exec python manage.py run_bot ${QASEDAK_BOT_ARGS}
fi

exec python manage.py run_bot
