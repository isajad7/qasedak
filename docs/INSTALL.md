# Install

This document covers the Productization installer for a fresh Ubuntu/Debian server.

The default installer is intentionally minimal: it brings up infrastructure, creates the admin user, creates a default Store, and leaves business setup for Django Admin. Telegram, X-UI/Sanaei, plans, routes, and payment/card settings are configured after install from the admin panel.

## Safety Model

- Run the installer from inside a local repo checkout.
- Use `--dry-run` first.
- No Telegram or X-UI live check runs unless explicitly requested.
- `.env` and `install.config.json` are written with mode `600`.
- Existing `.env` or `install.config.json` in the target install directory are not overwritten without confirmation, unless `--yes` is used.
- Revenue Engine is always bootstrapped as `enabled=true` and `dry_run=true`.
- Revenue Engine timers are not enabled for real sends.
- Secrets are not printed to stdout. Generated admin passwords are stored in the target `.env`.

## Dry-Run

```bash
TMPDIR=$(mktemp -d)
scripts/install.sh --dry-run --yes --install-dir "$TMPDIR/qasedak"
```

Dry-run prints planned commands only. It does not install packages, create directories, write `.env`, write `install.config.json`, create a virtualenv, run migrations, collect static files, or bootstrap the database.

## Real Local-Repo Install

From the project checkout:

```bash
sudo bash scripts/install.sh
```

Or non-interactive with safe defaults:

```bash
sudo bash scripts/install.sh --yes --install-dir /opt/qasedak
```

Install without systemd/nginx:

```bash
sudo bash scripts/install.sh --without-systemd --without-nginx
```

Install with systemd only:

```bash
sudo bash scripts/install.sh --with-systemd --without-nginx
```

Install with systemd and nginx HTTP:

```bash
sudo bash scripts/install.sh --with-systemd --with-nginx --without-tls
```

Install with systemd, nginx, and TLS:

```bash
sudo bash scripts/install.sh --with-systemd --with-nginx --with-tls
```

TLS requires a domain and nginx. If DNS lookup fails, TLS is skipped and the install continues in HTTP mode.

The installer copies the repo to the install directory with `rsync` and excludes runtime or secret-heavy paths:

- `.git`
- `.env`
- `install.config.json`
- `data/`
- `db.sqlite3`
- `media/`
- `venv/`
- `.venv/`
- `backups/`
- `logs/`
- `node_modules/`
- `__pycache__/`
- `*.pyc`

## Minimal Interactive Prompts

By default, when `--config` and `--advanced` are not provided, the installer asks only for:

- install directory
- optional domain
- admin username, optional email, and hidden/admin generated password
- SQLite database path
- systemd services, default yes on real installs
- nginx HTTP config, default yes on real installs
- TLS/certbot, only when a domain is present and nginx is enabled
- non-live doctor run choice

Minimal install deliberately does not ask for Store name, payment/card settings, Telegram bot token, Telegram admin IDs, Telegram proxy, X-UI/Sanaei panel, inbound, plan, route, force-join channel, or Revenue Engine real-send settings. Configure those from Django Admin after install.

## Post-Install Setup

After the installer completes, run or review the non-live doctor:

```bash
sudo /opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak
```

Then open Django Admin and complete [Post-Install Setup](POST_INSTALL_SETUP.md). This is where Store identity, payment/card settings, Telegram, Panel, Inbound, Plan, PlanInboundRoute, test purchase, and Revenue Engine dry-run review happen.

## Advanced Install

Use advanced interactive mode only when you intentionally want to answer product setup prompts during install:

```bash
sudo bash scripts/install.sh --advanced
```

Use a config file when you already have a complete private bootstrap config:

```bash
sudo bash scripts/install.sh --config install.config.json
```

## Service Names

Defaults:

- web service: `vpn-store-web.service`
- Telegram polling service: `vpn-store-telegram.service`
- nginx site: `vpn-store.conf`

Override them with:

```bash
sudo bash scripts/install.sh \
  --service-prefix vpn-store \
  --web-service-name vpn-store-web \
  --telegram-service-name vpn-store-telegram \
  --nginx-site-name vpn-store
```

## Existing Config

You can provide a private config generated from `docs/productization/install.config.example.json`:

```bash
sudo bash scripts/install.sh --config /root/install.config.json --install-dir /opt/qasedak
```

If the config uses `password_env`, `bot_token_env`, or `xui.password_env`, export those variables before running the installer or include them in the environment that launches it. The installer copies any present env-backed secret into the target `.env` with mode `600`.

## Generated `.env`

The installer writes process-level settings to:

```bash
<install-dir>/.env
```

It includes Django production settings, generated `DJANGO_SECRET_KEY`, SQLite path, locale, optional Telegram proxy values, upload limits, and install safety flags. For generated interactive installs it stores generated/provided secrets as env variables such as:

- `QASEDAK_ADMIN_PASSWORD`
- `QASEDAK_TELEGRAM_BOT_TOKEN`
- `QASEDAK_XUI_PASSWORD`

Config-file installs keep using the env names referenced by that private config.

The file is sourceable by shell scripts and is not committed.

## Generated `install.config.json`

The installer writes DB-backed install settings to:

```bash
<install-dir>/install.config.json
```

Minimal mode writes a small config that points the admin password at `QASEDAK_ADMIN_PASSWORD`, creates a default Qasedak store, disables Telegram setup, skips X-UI setup, and keeps Revenue Engine dry-run:

```json
{
  "app": {
    "install_dir": "/opt/qasedak",
    "domain": "",
    "enable_tls": false,
    "timezone": "Asia/Tehran",
    "language": "fa"
  },
  "admin": {
    "username": "admin",
    "email": "",
    "password_env": "QASEDAK_ADMIN_PASSWORD"
  },
  "database": {
    "engine": "sqlite",
    "sqlite_path": "/opt/qasedak/data/db.sqlite3"
  },
  "store": {
    "name": "Qasedak",
    "english_name": "Qasedak"
  },
  "telegram": {
    "enabled": false
  },
  "xui": {
    "configure_now": false
  },
  "revenue_engine": {
    "enabled": true,
    "dry_run": true
  }
}
```

## Install Without Domain

A domain is optional. If you continue without one, allowed hosts include the detected server IP when available plus `127.0.0.1,localhost`. nginx can run in HTTP/IP mode with `server_name _;`. TLS is not offered and secure cookie/SSL redirect flags remain disabled.

## Domain Without TLS

With a domain and `--without-tls`, nginx serves HTTP only. `.env` includes the domain in `DJANGO_ALLOWED_HOSTS`, sets `DJANGO_CSRF_TRUSTED_ORIGINS=http://domain`, and keeps secure cookie/SSL redirect flags disabled.

## Domain With TLS

With `--with-tls`, the installer:

- requires a domain
- requires nginx
- checks `getent hosts <domain>`
- plans/runs `certbot --nginx -d <domain>`
- does not enable HSTS automatically
- updates `.env` security flags only after TLS succeeds

The TLS `.env` flags are:

- `DJANGO_SECURE_SSL_REDIRECT=True`
- `DJANGO_SESSION_COOKIE_SECURE=True`
- `DJANGO_CSRF_COOKIE_SECURE=True`
- `DJANGO_CSRF_TRUSTED_ORIGINS=https://domain`

## Systemd

When enabled, the installer renders:

- `/etc/systemd/system/vpn-store-web.service`
- `/etc/systemd/system/vpn-store-telegram.service`

It then runs:

```bash
systemctl daemon-reload
systemctl enable --now vpn-store-web.service
systemctl enable --now vpn-store-telegram.service
```

Check status:

```bash
systemctl status vpn-store-web.service
systemctl status vpn-store-telegram.service
```

Disable services:

```bash
sudo systemctl disable --now vpn-store-web.service vpn-store-telegram.service
```

Logs:

```bash
journalctl -u vpn-store-web.service -f
journalctl -u vpn-store-telegram.service -f
```

## Nginx

When enabled, the installer renders:

- `/etc/nginx/sites-available/vpn-store.conf`
- `/etc/nginx/sites-enabled/vpn-store.conf`

The config proxies to `127.0.0.1:8000`, serves `static_root/` and `media/`, and sets standard proxy headers. Validate manually:

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## Doctor

After install, run non-live checks:

```bash
/opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak
```

Optional live checks:

```bash
/opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --live-bot --live-xui
```

Optional service/proxy checks:

```bash
/opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --systemd --nginx
```

Optional timer checks:

```bash
/opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --timers --no-fail
```

Live checks can call Telegram `getMe` and X-UI login, so keep them opt-in.

## Backup, Update, and Timers

Maintenance docs:

- [Backup](BACKUP.md)
- [Upgrade](UPGRADE.md)
- [Troubleshooting](TROUBLESHOOTING.md)

Always dry-run first:

```bash
/opt/qasedak/scripts/backup.sh --dry-run --install-dir /opt/qasedak --output-dir /opt/qasedak/backups --yes
/opt/qasedak/scripts/update.sh --dry-run --install-dir /opt/qasedak --source-dir /path/to/source --yes
/opt/qasedak/scripts/timers.sh --dry-run --install-dir /opt/qasedak --status --all
```

Timers are optional. Enabling timers is explicit and confirmed. The Revenue Engine timer is `vpn-store-revenue-scan-dry-run.timer` and uses `run_revenue_scan --dry-run`.

## Troubleshooting

- `python3 >= 3.12 is required`: install a Python 3.12 package source appropriate for your OS before rerunning.
- `Refusing to overwrite`: move or back up the existing target `.env` or `install.config.json`, then rerun and confirm.
- `bootstrap_install` validation errors: inspect `<install-dir>/install.config.json`; secrets are usually referenced by env names in `<install-dir>/.env`.
- `check_integrations` warnings about missing panel/routes: rerun with X-UI configured or finish setup in Django Admin.
- `TLS requested without a domain`: rerun with a domain or use `--without-tls`.
- `DNS lookup failed`: point DNS at the server first, then rerun certbot manually or rerun the installer after reviewing generated files.
- `nginx -t` fails: inspect `/etc/nginx/sites-available/vpn-store.conf` and the service port.

## Future Work

Future phases should add:

- GitHub release install mode
