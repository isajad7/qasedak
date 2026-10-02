#!/usr/bin/env bash
set -u

# Observation only. The independent Revenue worker's sending flags are not touched.
while true; do
    python manage.py collect_customer_activity || printf '%s\n' 'Customer activity tick failed; inspect application logs.' >&2
    sleep 900
done
