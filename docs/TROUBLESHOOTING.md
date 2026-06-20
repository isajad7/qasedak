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

New productized installs keep Revenue Engine dry-run focused. For manual checks:

```bash
/opt/qasedak/venv/bin/python /opt/qasedak/manage.py run_revenue_scan --dry-run
```

Do not create a real-send timer as part of P5.

## `check_integrations` Warnings

`check_integrations --no-fail` is designed to surface configuration gaps without stopping the whole doctor flow. Treat warnings as setup tasks:

- missing Telegram token/username
- proxy unavailable
- missing X-UI panel credentials
- no public plans/inbounds/routes
- Revenue Engine disabled or still dry-run
