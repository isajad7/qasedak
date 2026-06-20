# Productization P0 - Installer Readiness Audit

Date: 2026-06-20

Scope: audit and installer design only. No installer was executed, no production runtime file was edited, and no migration was created.

## 1. Current Runtime Architecture

### Django app

- Project: `core`
- Main apps: `store`, `payments`
- Settings modules:
  - `core.settings.base`: shared settings
  - `core.settings.development`: local defaults, currently includes real deployment hosts
  - `core.settings.production`: production defaults, currently includes a real deployment domain
- Entrypoints:
  - `manage.py` defaults to `core.settings.development`
  - `core.wsgi` and `core.asgi` default to `core.settings.production`
- Admin UI uses `django-jazzmin`.
- UI assets use Tailwind through `package.json` scripts.

### Database

- Current configured backend is SQLite only: `django.db.backends.sqlite3`.
- Runtime database path is `SQLITE_DATABASE_PATH`, defaulting to `BASE_DIR / "db.sqlite3"`.
- Existing deploy script uses `data/db.sqlite3` under the install directory.
- PostgreSQL is not implemented in settings yet. A Postgres installer mode would require a settings/schema change, dependency additions, and `.env.example` updates.

### Telegram polling

- Telegram bots are stored in DB records: `BotConfiguration`.
- Long polling command: `python manage.py run_telegram_polling`.
- Polling discovers active Telegram `BotConfiguration` rows and starts worker threads per config.
- Telegram webhook response mode exists as an optional setting, but the current production path expects long polling.
- `set_bot_webhooks` deletes Telegram webhooks for long polling and can register non-Telegram webhooks such as Bale.

### Systemd services

- No reusable systemd templates are committed.
- `legacy deploy script` currently generates:
  - Gunicorn service, default name `vpn-store-web.service`
  - Telegram polling service, default name `vpn-store-telegram.service`
- `legacy alternate deploy script` contains another older service path with `vpn-store-web.service` and `vpn-store-telegram.service`.
- The installer should generate neutral service names, for example `vpn-store-web.service` and `vpn-store-telegram.service`.

### Static/media

- Static:
  - `STATIC_URL=/static/`
  - `STATIC_ROOT=BASE_DIR / "static_root"`
  - `STATICFILES_DIRS=[BASE_DIR / "static"]`
- Media:
  - `MEDIA_URL=/media/`
  - `MEDIA_ROOT=BASE_DIR / "media"`
  - Payment receipts are stored under `media/payment_receipts/%Y/%m/`.
- Nginx config in `legacy deploy script` serves `/static/` from `static_root` and `/media/` from `media`.

### Logs/backups

- No explicit Django `LOGGING` config was found. Logs go through Python logging and, under systemd, should land in journald.
- No committed backup script was found.
- A local `db.sqlite3.backup` exists in the working tree and should not be shipped as a product artifact.
- `legacy deploy script` creates `data`, `media`, and `static_root`, but not a dedicated `logs` or `backups` directory.

### Integrations

- Telegram Bot API through `BotConfiguration` plus optional proxy env.
- Bale Bot API through `BotConfiguration`.
- X-UI/Sanaei panel through `Panel`, `Inbound`, `PlanInboundRoute`, and `store.xui_api`.
- SMSForwarder webhook at `/webhooks/smsforwarder/`.
- Manual card-to-card payment through `Store.card_number` and `Store.card_owner`.
- Revenue Engine, renewal reminders, panel health checks, panel usage snapshots, and daily admin reports run via management commands.
- Legacy WizWiz import support exists through admin/services/management commands.

## 2. Required Environment Variables

### Application runtime env

| Env name | Source | Required status | Installer handling |
| --- | --- | --- | --- |
| `DJANGO_SETTINGS_MODULE` | `.env.example`, deploy scripts, Django entrypoints | Required for services | Set to `core.settings.production` in generated `.env` and systemd units. |
| `DJANGO_SECRET_KEY` | `core/settings/base.py`, `core/settings/production.py` | Required in production | Auto-generate. Never print. Never overwrite without confirmation. |
| `DJANGO_DEBUG` | `core/settings/base.py`, `development.py`, `production.py` | Optional, security-critical | Default `False` for installs. |
| `DJANGO_ALLOWED_HOSTS` | `core/settings/base.py`, `development.py`, `production.py` | Required for production | Generate from domain, extra domains, IP, `127.0.0.1`, `localhost`. |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `core/settings/production.py`, `.env.example` | Required when public domain/HTTPS is used | Generate from domain with `https://` and optional `http://` for non-TLS mode. |
| `DJANGO_LANGUAGE_CODE` | `core/settings/base.py` | Optional | Ask or default to `fa`; support `en`. |
| `DJANGO_TIME_ZONE` | `core/settings/base.py` | Optional | Ask or default to `Asia/Tehran`. |
| `SQLITE_DATABASE_PATH` | `core/settings/base.py`, deploy script | Required only in SQLite mode | Set to `<install_dir>/data/db.sqlite3` for SQLite installs. |
| `PAYMENT_RECEIPT_MAX_UPLOAD_SIZE` | `core/settings/base.py` | Optional | Default 5 MB. Expose as advanced setting. |
| `SMSFORWARDER_WEBHOOK_TOKEN` | `core/settings/base.py`, `configuration_services.py` | Legacy optional | Prefer DB hash on `Store`; keep env fallback for migrations/backward compatibility. |
| `PAYMENT_SMS_TIME_ZONE` | `core/settings/base.py`, `configuration_services.py` | Legacy optional | Prefer `Store.payment_sms_time_zone`; env fallback optional. |
| `TELEGRAM_BOT_USERNAME` | `core/settings/base.py`, `configuration_services.py` | Legacy optional | Prefer `BotConfiguration.telegram_bot_username`. |
| `TELEGRAM_PROXY_URL` | `core/settings/base.py`, `bot_proxy.py` | Optional | Ask only if Telegram needs a proxy. Redact in logs. |
| `TELEGRAM_PROXY_PROTOCOL` | `core/settings/base.py`, `bot_proxy.py` | Optional | Structured proxy mode; default `http` if host/port are set. |
| `TELEGRAM_PROXY_HOST` | `core/settings/base.py`, `bot_proxy.py` | Optional | Ask only if proxy is enabled. |
| `TELEGRAM_PROXY_PORT` | `core/settings/base.py`, `bot_proxy.py` | Optional | Ask only if proxy is enabled. |
| `TELEGRAM_PROXY_USERNAME` | `core/settings/base.py`, `bot_proxy.py` | Optional secret-adjacent | Ask only if proxy auth is enabled. |
| `TELEGRAM_PROXY_PASSWORD` | `core/settings/base.py`, `bot_proxy.py` | Optional secret | Ask hidden; never print. |
| `TELEGRAM_WEBHOOK_RESPONSE_ENABLED` | `core/settings/base.py`, `bot_proxy.py` | Optional | Default `False` for long polling installs. |
| `BOT_API_CONNECT_TIMEOUT_SECONDS` | `core/settings/base.py`, `telegram_bot/client.py` | Optional | Default `3`. |
| `BOT_API_READ_TIMEOUT_SECONDS` | `core/settings/base.py`, `telegram_bot/client.py` | Optional | Default `8`. |
| `XUI_PANEL_PROXY_URL` | `core/settings/base.py`, `xui_api.py` | Optional secret-adjacent | Ask only if panel access needs a global HTTP proxy. Prefer per-panel proxy URL in DB when possible. |
| `DJANGO_USE_X_FORWARDED_HOST` | `core/settings/production.py` | Optional | Default `True` behind Nginx/CDN. |
| `DJANGO_SECURE_SSL_REDIRECT` | `core/settings/production.py` | Optional | Enable only when origin TLS is configured and tested. |
| `DJANGO_SESSION_COOKIE_SECURE` | `core/settings/production.py` | Optional security setting | Default `True` when domain/TLS or HTTPS CDN is used. |
| `DJANGO_CSRF_COOKIE_SECURE` | `core/settings/production.py` | Optional security setting | Default `True` when domain/TLS or HTTPS CDN is used. |
| `DJANGO_SECURE_HSTS_SECONDS` | `core/settings/production.py` | Optional | Default `0`; ask only in advanced TLS mode. |
| `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS` | `core/settings/production.py` | Optional | Default `False`. |
| `DJANGO_SECURE_HSTS_PRELOAD` | `core/settings/production.py` | Optional | Default `False`. |

### Configuration stored in DB, not env

These are install-time required values, but current code expects them in database rows rather than environment variables.

| Item | Model/source | Required status |
| --- | --- | --- |
| Store name and English name | `Store.name`, `Store.english_name` | Required for usable store bootstrap. |
| Store domain | `Store.domain` | Optional. |
| Payment card number and owner | `Store.card_number`, `Store.card_owner` | Required for manual payment mode. |
| SMSForwarder token hash | `Store.smsforwarder_webhook_token_hash` | Optional unless SMS matching is enabled. |
| Telegram bot token | `BotConfiguration.bot_token` | Required for Telegram bot mode. |
| Telegram admin IDs | `BotConfiguration.admin_user_id`, `additional_admin_user_ids` | Required for admin notifications. |
| Telegram bot username | `BotConfiguration.telegram_bot_username` | Strongly recommended for referral/link flows. |
| X-UI panel URL/username/password | `Panel.url`, `Panel.username`, `Panel.password` | Required if selling or creating VPN clients. |
| X-UI inbound details | `Inbound` fields | Required for sales/free trial routes. |
| Plans | `Plan` | Required for public purchasing unless custom-volume only. |
| Plan to inbound mapping | `PlanInboundRoute` | Required when explicit plan routing is enabled. |
| Revenue Engine mode | `Store.revenue_engine_enabled`, `Store.revenue_engine_dry_run` | Product install should set enabled true and dry-run true. |

### Deploy-script-only variables observed

These are not Django runtime settings, but current deployment scripts use them and the future installer should replace or normalize them:

- `SERVER_IP`
- `SERVER_PATH`
- `APP_DOMAIN`
- `APP_EXTRA_DOMAINS`
- `APP_URL`
- `SERVICE_NAME`
- `TELEGRAM_POLLING_SERVICE_NAME`
- `SSH_USER`
- `SSH_PORT`
- `PYTHON_BIN`
- `ORIGIN_SSL_CERT`
- `ORIGIN_SSL_KEY`
- `PIP_INDEX_URL`
- `USE_SSH_PROXY`
- `SSH_PROXY_HOST`
- `SSH_PROXY_PORT`
- `SSH_PROXY_TYPE`
- `SSH_PROXY_COMMAND`
- `SSH_PROXY_FDPASS`

## 3. Hardcoded Values Audit

### Important hardcoded values found

| Category | Value or pattern | Source | Productization action |
| --- | --- | --- | --- |
| Production domain | `example.com` | `core/settings/production.py`, `legacy deploy script` | Move to installer input and generated `.env`; remove as default before GitHub release. |
| Development hosts | `example.com`, `example.com`, `203.0.113.10` | `core/settings/development.py` | Replace with local-only defaults in a later phase. |
| Production server IP | `203.0.113.10` | `legacy deploy script` | Remove from product scripts; ask installer or detect public IP only after confirmation. |
| Install path | `/opt/vpn-store` | `legacy deploy script`, `.env.example` | Ask installer; default can be `/opt/vpn-store` or `/opt/vpn-store`. |
| Alternate install path | `/opt/vpn-store` | `legacy alternate deploy script` | Remove from product scripts or archive as local deploy history. |
| Alternate app URL | `http://example.com/` | `legacy alternate deploy script` | Remove from product scripts. |
| Service names | `vpn-store-web.service`, `vpn-store-telegram.service` | `legacy deploy script` | Use product-neutral service names. |
| Service names | `vpn-store-web.service`, `vpn-store-telegram.service` | `legacy alternate deploy script` | Remove or replace with generated names. |
| Nginx site name | `vpn-store` | `legacy deploy script` | Generate from project slug or use `vpn-store`. |
| SSH alias/temp path | `vpn-store-target`, `/tmp/vpn-store-ssh-*` | `legacy deploy script` | Remove from installer; not needed for local bare-metal install. |
| Brand/store default | `VPN Store` | `Store.name`, `Store.english_name`, templates, migrations | Ask installer; avoid product default leaking previous brand. |
| Admin brand | `VPN Store`, `VPN Store Admin` | `JAZZMIN_SETTINGS` | Acceptable product default, but should become configurable in P1/P2. |
| Language | `fa` | `core/settings/base.py` | Ask/default. |
| Timezone | `Asia/Tehran` | settings, `Store` defaults, SMS parser config | Ask/default. |
| Payment locale/currency | `TOMAN`, `IRR`, Persian SMS parsing | models/payment parser | Installer should expose currency/payment mode and document Iran-specific defaults. |
| Django secret default | hardcoded insecure development fallback | `core/settings/base.py` | Keep only for local dev or remove before GitHub release. Do not print value. |
| Pip mirror | `https://mirror2.chabokan.net/pypi/simple/` | `legacy deploy script` | Make optional; default to PyPI. |
| ACME path | `/var/www/letsencrypt` | `legacy deploy script` | Generate only when Nginx/TLS mode is selected. |
| X-UI API endpoint paths | `/login`, `/panel/api/inbounds/...` | `store/xui_api.py` | Not install-specific; keep as integration logic. |
| Telegram/Bale API base URLs | `api.telegram.org`, `tapi.bale.ai` | bot client and webhook command | Not install-specific; keep as integration constants. |

### Values searched but not found as concrete production config

- No concrete X-UI panel URL was found in source code; panel URL is stored in DB.
- No concrete Telegram bot token was found in source code.
- No concrete Telegram admin ID was found in non-test source.
- No concrete payment card number was found in non-test source.
- `LegacyBrand` was not found; `VPN Store` was found.

### Secret redaction notes

- Secret-bearing fields exist in DB models and env names: `DJANGO_SECRET_KEY`, bot token, panel password, Telegram proxy password, SMSForwarder token, X-UI proxy URL with possible credentials.
- The report intentionally lists names and locations only, not secret values.

## 4. Installation Requirements

### OS assumptions

- Current deploy scripts assume Debian/Ubuntu with `apt-get`.
- Root or sudo access is needed for package installation, systemd units, Nginx config, and optional TLS.
- Installer should check OS and fail with a clear message on unsupported distributions.

### Python version

- `requirements.txt` pins `Django==6.0.4`.
- Current deploy script enforces Python 3.12+.
- Installer should require Python 3.12 or newer and create a virtualenv.

### System packages

Current script installs:

- `python3`
- `python3-venv`
- `python3-pip`
- `nginx`
- `rsync`
- `curl`

Recommended product installer additions:

- `sqlite3` for SQLite maintenance and backup checks.
- `ca-certificates`.
- `openssl` for secret generation and TLS checks.
- `certbot` and `python3-certbot-nginx` only when TLS automation is selected.
- `postgresql-client` only when Postgres mode exists.
- `nodejs` and `npm` only if building Tailwind assets on the server.

### Virtualenv

- Create `<install_dir>/venv`.
- Install from `requirements.txt`.
- Avoid deleting an existing venv without explicit confirmation.

### Database choice

- Current supported mode: SQLite.
- Recommended P1/P2 design: installer offers SQLite first, Postgres later.
- Postgres mode needs new settings support, `psycopg` dependency, env vars, and backup strategy.

### Redis/RabbitMQ

- Not required by current requirements or settings.
- No Celery, Redis, or RabbitMQ integration was found.
- Current scheduled/async behavior is implemented by management commands plus cron/systemd timers or by request-time scheduling.

### Nginx

- Required for normal public production install.
- Optional for local/private testing if running Gunicorn directly.
- If no domain is provided, installer should still be able to configure HTTP by IP or skip Nginx with a clear note.

### Certbot/TLS

- Optional.
- Should be offered only when a domain exists and DNS points to the server.
- Existing script only reuses existing Let's Encrypt cert files; it does not run certbot.
- Installer should not block domain-less installs.

### Telegram access/proxy

- Telegram access is required for Telegram bot operation.
- Proxy is optional but important for restricted networks.
- Existing proxy support can use either a full proxy URL or structured protocol/host/port/username/password.

## 5. Interactive Installer Questions

The installer should ask these questions, grouped by dependency:

1. Install path, default `/opt/vpn-store` or `/opt/vpn-store`.
2. Unix user/group for services, default `www-data` if present.
3. Public domain, optional.
4. Extra domains, optional.
5. If domain exists: enable HTTPS now? default yes only when DNS check passes.
6. If no domain: continue HTTP/IP install? default yes.
7. Admin username.
8. Admin email, optional.
9. Admin password, hidden input or auto-generate and print once.
10. Generate Django secret key? default yes.
11. Allowed hosts, generated from domain/IP with manual edit option.
12. CSRF trusted origins, generated from domain/TLS mode with manual edit option.
13. Database mode: SQLite now; Postgres later.
14. SQLite database path, default `<install_dir>/data/db.sqlite3`.
15. Store name.
16. Store English name/slug.
17. Store timezone, default `Asia/Tehran`.
18. Site language, default `fa`.
19. Payment mode: manual card-to-card enabled?
20. Card number, hidden/redacted in summary.
21. Card owner.
22. Bank name and Sheba number, optional.
23. SMSForwarder webhook enabled?
24. SMSForwarder webhook token, hidden, optional.
25. Payment SMS timezone, default store timezone.
26. Telegram bot enabled?
27. Telegram bot token, hidden.
28. Telegram bot username.
29. Telegram admin IDs, comma/newline-separated.
30. Force join channel enabled?
31. Force join channel ID or username, optional.
32. Force join invite link, optional.
33. Telegram proxy enabled?
34. If proxy enabled: proxy mode, full URL or structured fields.
35. If structured proxy: protocol, host, port, username, password.
36. X-UI/Sanaei panel configured now?
37. Panel name.
38. Panel URL.
39. Panel username.
40. Panel password, hidden.
41. Panel proxy URL, optional and hidden/redacted in summary.
42. Sync inbound from panel now? default optional.
43. Default inbound ID.
44. If not syncing inbound: protocol, server IP/host, port, config params, network type, security, SNI, fingerprint, public key, short ID, WebSocket path/host.
45. Create default public plan now?
46. Plan name, volume, duration, price, currency, device limit.
47. Default inbound/plan route setup.
48. Free trial enabled?
49. If free trial: free trial inbound, traffic, duration, cooldown.
50. SMS provider optional settings.
51. Revenue Engine enabled? default yes.
52. Revenue Engine dry-run? default yes and should not be disabled during installer bootstrap.
53. Daily admin report enabled?
54. Renewal reminders enabled?
55. Run non-live doctor after install? default yes.
56. Run live Telegram/X-UI checks? default no, ask explicitly because they call external services.

## 6. Installer Modes

- `scripts/install.sh`: bare-metal installer for a fresh server. It should be idempotent and support `--dry-run`.
- `docker-compose`: later or optional. Useful for Postgres/Nginx isolation, but not required for P0/P1.
- `scripts/update.sh`: pull/release update, backup first, install dependencies, collectstatic, migrate, restart services.
- `scripts/backup.sh`: backup SQLite/Postgres, `.env`, media, and selected generated config files.
- `scripts/doctor.sh`: wrapper around `manage.py check`, `check_integrations`, service status, Nginx status, and optional live checks.
- `scripts/uninstall.sh`: optional and explicitly destructive. Should require confirmation and leave backups by default.

## 7. Generated Files Design

The installer should generate only instance-local files and should never overwrite existing files without confirmation.

| File/path | Purpose | Notes |
| --- | --- | --- |
| `<install_dir>/.env` | Runtime environment | Mode 0600 or 0640. Contains secrets. Never committed. |
| `<install_dir>/data/` | SQLite DB and state | Created for SQLite mode. |
| `<install_dir>/media/` | User uploads/payment receipts | Backed up by `backup.sh`. |
| `<install_dir>/static_root/` | Collected static files | Rebuildable. |
| `<install_dir>/logs/` | Optional app logs | Current app logs to journald; file logging can be a later explicit feature. |
| `<install_dir>/backups/` | Local backups | Should be outside web-served paths. |
| `/etc/systemd/system/vpn-store-web.service` | Gunicorn service | Generated from template. |
| `/etc/systemd/system/vpn-store-telegram.service` | Telegram long polling | Runs `manage.py run_telegram_polling`. |
| `/etc/systemd/system/vpn-store-*.timer` | Optional scheduled jobs | For reminders, reports, health checks, usage snapshots, revenue scans. |
| `/etc/nginx/sites-available/vpn-store` | Nginx site | Only when Nginx mode selected. |
| `/etc/nginx/sites-enabled/vpn-store` | Nginx symlink | Only when Nginx mode selected. |
| initial admin user | Django admin access | Created by `bootstrap_install` or installer shell wrapper. |
| initial `Store` | Product identity and payment config | Created by management command. |
| initial `BotConfiguration` | Telegram bot/admin config | Created by management command. |
| initial `Panel`/`Inbound`/`Plan`/`PlanInboundRoute` | Sellable VPN setup | Created only when user opts in. |

## 8. Safety Design

- Idempotency:
  - Detect existing install directory.
  - Detect existing `.env`, database, systemd units, and Nginx site.
  - Re-run should converge without duplicating DB bootstrap rows.
- Backup before update:
  - Backup DB, `.env`, media, and generated system files before `update.sh` mutates anything.
- Dry-run install mode:
  - Print planned file paths, packages, service names, and DB objects.
  - Redact secret values.
- No overwrite without confirmation:
  - Existing `.env` should be preserved by default.
  - Existing DB should never be replaced automatically.
  - Existing systemd/nginx files should be diffed or saved with timestamped backup.
- Secret redaction:
  - Redact any value for names containing `SECRET`, `TOKEN`, `PASSWORD`, `KEY`, `PROXY_URL`, `CARD`.
  - Redact panel URL if it embeds credentials.
  - Avoid printing full Telegram admin IDs by default; show count or masked IDs in summaries.
- Rollback plan:
  - On failed install before service start, leave files but print cleanup steps.
  - On failed update, restore previous `.env`, DB backup, and service configs, then restart old service.
- External side effects:
  - Default doctor should be non-live.
  - Live bot/X-UI checks require explicit confirmation.
  - Revenue Engine must remain dry-run after installer bootstrap.

## 9. GitHub Readiness

### Missing or incomplete root files

- `README.md` was not found.
- `.gitignore` was not found.
- `LICENSE` was not found.
- `docs/INSTALL.md` was not found.
- `docs/UPGRADE.md` was not found.
- `docs/TROUBLESHOOTING.md` was not found.
- `scripts/install.sh`, `scripts/update.sh`, `scripts/backup.sh`, `scripts/doctor.sh` were not found.

### Files/data that must not be shipped

- `.env`
- `db.sqlite3`
- `db.sqlite3.backup`
- `media/`
- `venv/`
- `node_modules/`
- `static_root/`
- `data/`
- `backups/`
- `logs/`
- `*.pyc`
- `__pycache__/`
- local deploy scripts with real host/domain/IP unless sanitized or moved to private docs.

### Needed GitHub product files

- `README.md`: product overview, screenshots optional, install warning, supported OS.
- `.env.example`: complete runtime env list with safe placeholders.
- `.gitignore`: strict local/prod artifact exclusions.
- `scripts/install.sh`: future P3.
- `scripts/update.sh`: future P5.
- `scripts/backup.sh`: future P5.
- `scripts/doctor.sh`: future P5.
- `docs/INSTALL.md`: manual and installer install guide.
- `docs/UPGRADE.md`: update and migration process.
- `docs/TROUBLESHOOTING.md`: Telegram, X-UI, Nginx/TLS, DB, static/media.
- `LICENSE`: choose before public GitHub release.
- GitHub Actions optional:
  - `python manage.py check`
  - `python manage.py test store.tests payments.tests`
  - `python manage.py makemigrations --check --dry-run`
  - lint/shellcheck later.

## 10. Productization Risk List

1. Secret leak risk:
   - Existing DB, media, `.env`, and deploy scripts must not be committed with production secrets or customer data.
2. Hardcoded production values:
   - Real domains/IPs/service names exist in settings and deploy scripts.
3. SQLite production warning:
   - Current settings are SQLite only. Good for small installs, risky for larger stores and concurrent writes.
4. Telegram proxy instability:
   - Bot operation depends on Telegram reachability; proxy config must be testable and redacted.
5. X-UI panel/inbound setup complexity:
   - Sellable setup requires valid panel credentials, inbound IDs, route mapping, and optional sync.
6. Revenue real-send risk:
   - Model default currently has `revenue_engine_dry_run=False`; product install must override to dry-run true.
7. Domain/TLS optional complexity:
   - Installer must support domain-less installs without failing, while still guiding users toward HTTPS for production.
8. Existing deploy scripts are destructive-ish:
   - `rsync --delete`, service restarts, Nginx rewrites, and venv deletion are not appropriate as a public installer baseline without safeguards.
9. No committed backup/update scripts:
   - Product users need a safe update path before public release.
10. Logs are not explicitly designed:
   - Journald is acceptable, but docs and doctor checks should make this visible.

## 11. Source Code Search Summary

Requested search terms were reviewed across source files while excluding local runtime directories such as `venv`, `node_modules`, `media`, `.env`, and `*.pyc`.

Important source areas:

- Env and Django settings: `core/settings/base.py`, `core/settings/development.py`, `core/settings/production.py`.
- Production deploy behavior: `legacy deploy script`.
- Legacy deploy behavior: `legacy alternate deploy script`.
- Telegram polling/proxy: `store/management/commands/run_telegram_polling.py`, `store/bot_proxy.py`, `store/telegram_bot/client.py`.
- Doctor-like checks: `store/management/commands/check_integrations.py`.
- X-UI/Sanaei: `store/xui_api.py`, `Panel`, `Inbound`, `PlanInboundRoute`.
- Payments/SMS: `payments/views.py`, `payments/sms_parser.py`, `Store` payment fields.
- Product identity and revenue defaults: `Store`, templates, `JAZZMIN_SETTINGS`.

Existing doctor foundation:

- `check_integrations` already checks Django settings, stores, bots, panels, inbounds, routes, panel monitoring, daily reports, revenue engine, panel usage, SMS webhook, static/media.
- It supports `--live-bot`, `--live-xui`, `--send-telegram-test-message`, and `--no-fail`.
- A future `scripts/doctor.sh` should wrap it and default to non-live checks.

## 12. Proposed Installer UX

Public install command after GitHub release:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/scripts/install.sh)
```

Proposed flow:

```text
VPN Store installer

Install path [/opt/vpn-store]:
Domain (optional):
No domain provided. Continue with HTTP/IP-only install? [Y/n]
Enable HTTPS? [y/N]          # asked only when domain exists
Admin username [admin]:
Admin email (optional):
Admin password [auto-generate]:
Store name:
Timezone [Asia/Tehran]:
Database [sqlite/postgres]:  # postgres disabled until implemented
Telegram bot token:
Telegram bot username:
Admin Telegram IDs:
Use Telegram proxy? [y/N]:
Configure X-UI/Sanaei panel now? [Y/n]:
Create default plan now? [Y/n]:
Enable Revenue Engine? [Y/n]:
Revenue Engine dry-run [Y/n]: Y
Run non-live doctor after install? [Y/n]:
Run live Telegram/X-UI checks? [y/N]:
```

Installer summary should show:

- Install path
- Domain/TLS mode
- Database mode and path
- Service names
- Store name
- Count of Telegram admin IDs
- Whether Telegram proxy is enabled, with credentials redacted
- Whether X-UI panel is configured, with URL/password redacted
- Revenue Engine dry-run status

Installer summary should not show:

- Django secret key
- Bot token
- Panel password
- Proxy password
- SMS webhook token
- Full card number

## 13. Proposed Phase Plan

### P1 - `.env.example` and config schema

Outputs:

- Complete `.env.example` with every runtime env listed above.
- `docs/CONFIGURATION.md` explaining env vs DB-backed settings.
- Product-neutral defaults in settings proposed as a patch.
- Decision record for SQLite vs Postgres.
- Secret redaction rules documented.

### P2 - Management command `bootstrap_install`

Outputs:

- `python manage.py bootstrap_install --config install.json`.
- Creates admin user, Store, BotConfiguration, Panel, Inbound, Plan, PlanInboundRoute.
- Idempotent updates by stable slug/name.
- Sets Revenue Engine `enabled=true`, `dry_run=true`.
- Supports `--dry-run`.
- Does not call Telegram/X-UI live APIs unless `--live-check` is passed.

### P3 - `scripts/install.sh`

Outputs:

- Bare-metal installer with interactive prompts.
- OS/Python/package checks.
- `.env` generation with no overwrite by default.
- Virtualenv creation and dependency install.
- `collectstatic`, `migrate`, and `bootstrap_install`.
- Non-live doctor run.
- No destructive behavior without confirmation.

### P4 - systemd/nginx/TLS optional

Outputs:

- Product-neutral systemd unit templates.
- Optional timers for reminders, panel health, panel usage, reports, revenue scan.
- Optional Nginx config generation.
- Optional Certbot automation when domain and DNS are valid.
- Domain-less HTTP/IP mode documented.

### P5 - doctor/backup/update scripts

Outputs:

- `scripts/doctor.sh` wrapping Django checks, `check_integrations`, systemd, Nginx, disk, DB, static/media.
- `scripts/backup.sh` for SQLite DB, media, `.env`, generated service/nginx configs.
- `scripts/update.sh` with backup, dependency install, collectstatic, migrate, restart, rollback guidance.
- Optional `scripts/uninstall.sh` with explicit destructive confirmations.

### P6 - README/GitHub release

Outputs:

- Public `README.md`.
- `docs/INSTALL.md`, `docs/UPGRADE.md`, `docs/TROUBLESHOOTING.md`.
- `LICENSE`.
- `.gitignore`.
- GitHub Actions for checks/tests/migration check.
- Sanitized release checklist verifying no `.env`, DB, media, logs, backups, pyc, or local deploy artifacts are included.

## 14. P0 Conclusion

This repository is close to being installable because it already has deploy knowledge, long polling, integration checks, and DB-backed business configuration. The main P0 blockers for productization are:

- hardcoded local production values in settings and deploy scripts,
- no safe public installer/update/backup wrapper,
- incomplete GitHub hygiene files,
- SQLite-only settings,
- install-time config split between env and DB,
- Revenue Engine dry-run default mismatch for new installs.

Recommended next phase: P1, focused on `.env.example`, config schema, product-neutral defaults, `.gitignore`, and documentation before any executable installer is added.
