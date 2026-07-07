# Troubleshooting

Start with the non-live doctor:

```bash
/opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --no-fail
```

Use live checks only when network calls are intended:

```bash
/opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --live-bot --live-xui --no-fail
```

## Telegram Bot and Proxy

- Check `.env` exists and is mode `600`.
- Run `doctor.sh --live-bot --no-fail` to pass `--live-bot` into `check_integrations`.
- Review `TELEGRAM_PROXY_*` values in `.env` locally; do not paste token/proxy passwords into logs.
- Check polling logs when systemd is enabled:

```bash
journalctl -u vpn-store-telegram.service -n 100 --no-pager
```

## X-UI Panel

- Run `doctor.sh --live-xui --no-fail` to test live integration paths.
- Confirm panel URL, username, password env name, inbounds, and plan routes in Django Admin or `install.config.json`.
- For 3X-UI v3.x or multi-node panels, run `manage.py audit_xui_compatibility --panel-id <id> --live --no-write --verbose` before enabling routes. See [3X-UI Multi-Node Operations](integrations/3x-ui-multinode.md).
- If the same inbound ID appears on multiple nodes, preview local node metadata with `manage.py sync_xui_topology --panel-id <id> --dry-run --verbose`; apply only with `--apply --confirm APPLY_XUI_TOPOLOGY_SYNC`.
- Modern paid orders are deferred: checkout should not create a remote client. If approval fails, run `manage.py reconcile_order_provisioning --order-id <id> --dry-run --verbose`; use `--apply` only after confirming the frozen panel/node/inbound scope.
- `check_integrations --no-fail` warnings usually mean setup is incomplete, not necessarily broken code.
- For a single failed order delivery, open `/admin/store/orders/workbench/`, then the order review page. Use approve/retry only with explicit confirmation; GET review pages do not call X-UI or Telegram.
- For an existing customer service, open `/admin/store/services/workbench/`, then the service review page. Check panel/inbound status, expiry, local usage, Telegram target, and saved delivery errors before using explicit POST actions like refresh config link, update usage, disable, or enable.

## Order and Payment Review

- Pending card receipts appear in `/admin/store/orders/workbench/` under "نیازمند بررسی".
- Open the order review page to compare expected amount, receipt/SMS status, route status, and delivery status.
- Reject requires a reason. Approve/retry may call X-UI/Sanaei and Telegram. On modern panels, approve creates an enabled remote client only after remote lookup and verification; failed provisioning leaves the order confirmed but not completed.
- The review UI hides full config links, UUIDs, full phone numbers, card numbers, tokens, passwords, and proxy values. Use server logs for deeper debugging when needed.

## Customer and Service Review

- Active, expiring, expired, no-Telegram-target, and route/panel/inbound problem queues are in `/admin/store/services/workbench/`.
- Service review pages are under `/admin/store/services/<vpn_client_id>/review/`.
- Customer review pages are under `/admin/store/customers/<customer_id>/review/`.
- GET pages are read-only and do not call Telegram or X-UI. Use the explicit buttons to resend config, refresh links, update usage, disable, or enable.
- If resend fails, verify the customer has an active Telegram `BotUser` target and saved config links. The admin UI will not print the full link or token.

## Reports and Analytics

- Business reports are under `/admin/store/reports/`.
- Use the date filters for today, 7 days, 30 days, this month, previous month, or a custom `YYYY-MM-DD` range.
- Current-state service metrics such as active, expired, expiring in 3 days, remaining traffic, and Telegram target coverage are snapshots of the current DB state. They are not period counts.
- Period metrics such as revenue, successful orders, new customers, reminders, support messages, campaigns, and Revenue Engine logs are counted inside the selected date range and compared with the previous equal-length range.
- If panel usage says unknown or insufficient, saved `PanelDailyUsage` rows are missing or incomplete. Do not treat that as zero traffic; first confirm the panel usage snapshot/daily usage timers are running.
- CSV exports live at `/admin/store/reports/export/` with `report=sales`, `customers`, `services`, `operations`, `revenue`, or `panel_usage`. Exports are aggregate/redacted and should not contain secrets, full phone/email values, config links, UUIDs, card numbers, Telegram IDs, or proxy credentials.
- The reports page and CSV export are read-only GET paths. They do not call Telegram, X-UI/Sanaei, broadcasts, reminders, or Revenue Engine scans.

## Support and Customer Messages

- Support queues are in `/admin/store/support/workbench/`.
- A single conversation review page is under `/admin/store/support/<support_id>/review/`.
- A one-off customer message page is under `/admin/store/customers/<customer_id>/message/`.
- GET pages are read-only and do not send Telegram messages.
- Support replies and direct customer messages require POST, CSRF, and explicit confirmation.
- If sending fails with "no Telegram target", link or reactivate the customer `BotUser` first. The admin page targets only that customer and has no group audience selector; use existing campaign/broadcast tools separately and cautiously for group messaging.
- Review pages redact full config links, UUIDs, tokens, proxy credentials, full phone numbers, and full emails.

## Nginx and TLS

```bash
sudo nginx -t
/opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --nginx --no-fail
```

Check:

- `/etc/nginx/sites-available/vpn-store.conf`
- `/etc/nginx/sites-enabled/vpn-store.conf`
- DNS points to the server before certbot/TLS
- `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` match the domain and scheme

## Systemd Services

```bash
/opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --systemd --no-fail
systemctl status vpn-store-web.service vpn-store-telegram.service
journalctl -u vpn-store-web.service -n 100 --no-pager
```

If unit files changed, run:

```bash
sudo systemctl daemon-reload
```

Restart only after reviewing the reason for failure.

## Timers

Timers are optional:

```bash
/opt/qasedak/scripts/timers.sh --dry-run --install-dir /opt/qasedak --status --all
/opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --timers --no-fail
```

Revenue Engine timer support is dry-run only through `vpn-store-revenue-scan-dry-run.timer`.

## Static and Media

Rebuild static files:

```bash
/opt/qasedak/venv/bin/python /opt/qasedak/manage.py collectstatic --noinput
```

Check nginx aliases for:

- `/opt/qasedak/static_root/`
- `/opt/qasedak/media/`

Media is runtime data; include it explicitly in backups with `--include-media`.

## Database Warnings

- Check `.env` for `DATABASE_ENGINE`.
- SQLite mode: stop long-running writes before maintenance when possible. `backup.sh` uses SQLite backup mode when `sqlite3` is installed. If migrations fail due to locks, stop app services, rerun the update step, then run doctor before restarting.
- PostgreSQL mode: confirm `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, and `POSTGRES_PORT` are set in `.env`.
- Run `doctor.sh --no-fail`; it checks `pg_isready`, Django DB connectivity, migration status, backup tools (`pg_dump`, `pg_restore`), and the Docker bridge path required by SaaS tenant containers.
- PostgreSQL backups use `pg_dump -Fc`. Restore requires `pg_restore`, a fresh pre-restore backup, and explicit operator confirmation before any destructive clean.

## Tenant 502 / Restart Loop With Host PostgreSQL

Tenant containers connect to host PostgreSQL through Docker bridge networking at `172.17.0.1:5432`. If PostgreSQL only listens on `127.0.0.1:5432`, the tenant web container can restart repeatedly and Nginx will return `502`. The common log symptom is:

```text
connection to server at "172.17.0.1", port 5432 failed: Connection refused
```

Triage:

```bash
docker logs qasedak_<tenant>
curl http://127.0.0.1:<port>/health/
ss -ltnp | grep 5432
pg_lsclusters
sudo /opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --no-fail --verbose
```

Required host PostgreSQL shape:

```text
listen_addresses = '127.0.0.1,172.17.0.1'
host all all 172.17.0.0/16 scram-sha-256
```

Recovery:

```bash
sudo -u postgres psql -Atqc "SHOW config_file; SHOW hba_file;" postgres
sudo cp <postgresql.conf> <postgresql.conf>.bak.$(date +%Y%m%d%H%M%S)
sudo cp <pg_hba.conf> <pg_hba.conf>.bak.$(date +%Y%m%d%H%M%S)
sudoedit <postgresql.conf>
sudoedit <pg_hba.conf>
sudo systemctl restart postgresql
sudo /opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --no-fail --verbose
docker restart qasedak_<tenant>
curl http://127.0.0.1:<port>/health/
```

Only bind PostgreSQL to `127.0.0.1` and `172.17.0.1` for this setup. Do not use `listen_addresses='*'` and do not add public CIDRs to `pg_hba.conf`.

## Backup and Restore Center

Open:

```text
/admin/store/backups/
```

Common issues:

- Upload rejected: use a `.tar.gz` Qasedak package, not a raw DB dump.
- Validation rejected: check for missing `manifest.json`, missing `checksums.sha256`, checksum mismatch, unsupported backup version, unsafe archive paths, symlinks, or missing DB file.
- PostgreSQL validation rejected: install `pg_restore` and confirm the package contains `database/db.postgres.dump`.
- SQLite package on PostgreSQL target: validation may warn that a migration path is required.
- Download forbidden: backups that include env require Django superuser or the explicit env-download capability.
- Restore command missing: Validate the restore job first, then enter the exact `RESTORE_QASEDAK_BACKUP_<id>` confirmation phrase.

Backup and restore files must stay under private backup roots. Do not move them into `media/`, `static/`, tickets, chat, or public repos.

## PostgreSQL select_for_update compatibility

SQLite ignores row-lock SQL, so nullable `select_related()` joins can pass every SQLite test and still fail on PostgreSQL with `FOR UPDATE cannot be applied to the nullable side of an outer join`. When a lock query needs nullable related objects, lock only the base row with `select_for_update_self()` or split the base-row lock from later related loading. Before a production switch, run:

```bash
/opt/qasedak/scripts/postgres_test_gate.sh --env-file /path/to/postgres.env --full
```

The gate must use a PostgreSQL connection and a disposable Django test database. Do not switch production `.env` to PostgreSQL unless this gate passes.

## Revenue Engine Dry-Run

New productized installs keep Revenue Engine dry-run focused. Start from the admin control page:

```text
/admin/store/revenue/control/
```

Dry-run logs offers without sending real Telegram messages. Use real-send only after reviewing dry-run counts, failed logs, Telegram target coverage, and safety caps. If the rollout looks risky, use reset safe defaults to return to enabled + dry-run with conservative limits.

For manual checks:

```bash
/opt/qasedak/venv/bin/python /opt/qasedak/manage.py run_revenue_scan --dry-run --engine all --limit 100 --verbose
/opt/qasedak/venv/bin/python /opt/qasedak/manage.py revenue_report --days 7 --verbose
/opt/qasedak/scripts/doctor.sh --live-bot --live-xui
```

Do not create a real-send timer as part of the dry-run rollout. Real-send from the control page is POST-only and requires explicit confirmation.

## Broadcast Campaigns

Open the owner workflow at:

```text
/admin/store/campaigns/
```

If a campaign stays queued, run a bounded processor batch:

```bash
/opt/qasedak/venv/bin/python /opt/qasedak/manage.py process_broadcast_queue --campaign-id <id> --batch-size 50 --verbose
```

Use `--dry-run` first when checking production state. The web confirm page never sends to all recipients; it only materializes recipients and sets the campaign to queued.

Common campaign states:

- `draft`: message/audience can still be edited.
- `queued`: ready for `process_broadcast_queue`.
- `sending`: processor is currently working or stopped mid-batch; rerun the command to continue pending recipients.
- `sent`: no pending recipients remain; failed/skipped counts may still need review.
- `failed`: no successful sends were recorded or the campaign failed validation.
- `cancelled`: queued/draft campaign was cancelled; already sent recipients are not rolled back.

Recipient failure categories are safe summaries. `blocked` and `chat not found` are not retried. Timeout/network and rate-limit failures can be retried from Campaign Review or with `--resume-failed`.

CSV export from Campaign Review intentionally contains only internal ids, status, safe error category, and timestamps. It does not include chat IDs, phone/email, message text, config links, UUIDs, tokens, or raw metadata.

## `check_integrations` Warnings

`check_integrations --no-fail` is designed to surface configuration gaps without stopping the whole doctor flow. Treat warnings as setup tasks:

- missing Telegram token/username
- proxy unavailable
- missing X-UI panel credentials
- no public plans/inbounds/routes
- Revenue Engine disabled or still dry-run

## Staff Access Problems

If a staff user cannot see a workflow, check their product role in:

```text
/admin/store/staff/
```

Then resync role groups:

```bash
/opt/qasedak/venv/bin/python /opt/qasedak/manage.py sync_staff_roles --apply
```

Use a Django superuser for emergency recovery. Do not grant raw `is_superuser` or arbitrary `user_permissions` to ordinary staff. The Staff Access Center protects the last active superuser and prevents non-superuser staff from editing superuser accounts.

## Service Reconciliation

Open the owner workflow at:

```text
/admin/store/services/workbench/
```

Use **بررسی سرویس‌ها با پنل** for a POST-only X-UI read. GET Workbench and Review pages use saved DB state only.

Important safety notes:

- `panel_unreachable`, timeout, proxy/login failure, and node offline are not deletion proof.
- Only a fresh `remote_missing` result in the same panel/node/inbound scope can be soft-deleted locally.
- Cleanup revalidates the remote scope before changing local state.
- Cleanup never calls any X-UI delete API.

For CLI preview:

```bash
/opt/qasedak/venv/bin/python /opt/qasedak/manage.py reconcile_vpn_clients --all --dry-run --verbose
```

See [Service Reconciliation](SERVICE_RECONCILIATION.md).
