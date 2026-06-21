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
- `check_integrations --no-fail` warnings usually mean setup is incomplete, not necessarily broken code.
- For a single failed order delivery, open `/admin/store/orders/workbench/`, then the order review page. Use approve/retry only with explicit confirmation; GET review pages do not call X-UI or Telegram.
- For an existing customer service, open `/admin/store/services/workbench/`, then the service review page. Check panel/inbound status, expiry, local usage, Telegram target, and saved delivery errors before using explicit POST actions like refresh config link, update usage, disable, or enable.

## Order and Payment Review

- Pending card receipts appear in `/admin/store/orders/workbench/` under "نیازمند بررسی".
- Open the order review page to compare expected amount, receipt/SMS status, route status, and delivery status.
- Reject requires a reason. Approve/retry may call the existing X-UI/Sanaei and Telegram order services, so use them only after checking the receipt.
- The review UI hides full config links, UUIDs, full phone numbers, card numbers, tokens, passwords, and proxy values. Use server logs for deeper debugging when needed.

## Customer and Service Review

- Active, expiring, expired, no-Telegram-target, and route/panel/inbound problem queues are in `/admin/store/services/workbench/`.
- Service review pages are under `/admin/store/services/<vpn_client_id>/review/`.
- Customer review pages are under `/admin/store/customers/<customer_id>/review/`.
- GET pages are read-only and do not call Telegram or X-UI. Use the explicit buttons to resend config, refresh links, update usage, disable, or enable.
- If resend fails, verify the customer has an active Telegram `BotUser` target and saved config links. The admin UI will not print the full link or token.

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

## SQLite Locks and Warnings

- Stop long-running writes before maintenance when possible.
- `backup.sh` uses SQLite backup mode when `sqlite3` is installed.
- If migrations fail due to locks, stop app services, rerun the update step, then run doctor before restarting.

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

## `check_integrations` Warnings

`check_integrations --no-fail` is designed to surface configuration gaps without stopping the whole doctor flow. Treat warnings as setup tasks:

- missing Telegram token/username
- proxy unavailable
- missing X-UI panel credentials
- no public plans/inbounds/routes
- Revenue Engine disabled or still dry-run
