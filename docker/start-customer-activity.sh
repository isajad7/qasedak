#!/usr/bin/env bash
set -u

# Journeys have independent per-store modes; legacy Revenue worker flags are not touched.
while true; do
    if python manage.py collect_customer_activity; then
        python manage.py run_customer_outreach || printf '%s\n' 'Purchase outreach tick failed; inspect application logs.' >&2
    else
        printf '%s\n' 'Customer activity tick failed; inspect application logs.' >&2
    fi
    sleep 900
done
