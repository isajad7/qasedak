#!/usr/bin/env bash
set -u

# Runs after migrations in the app entrypoint. DB claims prevent duplicate approvals across replicas.
while true; do
    python manage.py process_order_automation --limit 100 || printf '%s\n' 'Order automation tick failed; inspect application logs.' >&2
    sleep 30
done
