# Configuration

VPN Store uses two configuration layers:

- Environment settings: process-level values needed before Django can start, such as the secret key, host allow-list, database path, proxy settings, and security flags.
- DB-backed settings: product and business objects that are created during bootstrap and edited in Django Admin, such as Store, bot, panel, plan, payment, and Revenue Engine settings.

The future installer should generate `.env` from `.env.example`, then pass an install config file to the DB bootstrap command described in `docs/productization/02-bootstrap-install.md`. Do not put real secrets in `.env.example`, docs, GitHub issues, logs, or installer summaries.

## Environment Settings

Put these in `.env`:

- `DJANGO_SECRET_KEY`
- `DJANGO_SETTINGS_MODULE`
- `DJANGO_DEBUG`
- `DJANGO_ALLOWED_HOSTS`
- `DJANGO_CSRF_TRUSTED_ORIGINS`
- `SQLITE_DATABASE_PATH`
- `DJANGO_LANGUAGE_CODE`
- `DJANGO_TIME_ZONE`
- Telegram proxy settings
- global X-UI panel proxy setting
- upload limits
- legacy SMS/payment fallbacks
- production security flags

Required runtime env for production:

- `DJANGO_SETTINGS_MODULE=core.settings.production`
- `DJANGO_SECRET_KEY`
- `DJANGO_ALLOWED_HOSTS`
- `SQLITE_DATABASE_PATH` when using SQLite

Required when serving a public HTTPS domain:

- `DJANGO_CSRF_TRUSTED_ORIGINS`
- secure cookie flags should remain enabled

Optional env:

- Telegram proxy settings
- `XUI_PANEL_PROXY_URL`
- upload limits
- HSTS and SSL redirect flags
- optional live-check controls used by future installer/doctor wrappers

Legacy fallback env:

- `SMSFORWARDER_WEBHOOK_TOKEN`
- `PAYMENT_SMS_TIME_ZONE`
- `TELEGRAM_BOT_USERNAME`

Prefer DB-backed fields for new installs. Keep legacy env only for old deployments and migrations.

## DB-Backed Settings

These are install-time settings, but they belong in the database:

- `Store`: product name, English name, domain, payment/card settings, SMSForwarder token hash, payment SMS timezone, sales mode, reminders, reports, and Revenue Engine flags.
- `BotConfiguration`: Telegram/Bale token, bot username, admin IDs, provider, active state, and force-join settings.
- `Panel`: X-UI/Sanaei panel URL, username, password, per-panel proxy URL, and health behavior.
- `Inbound`: sellable panel inbound details.
- `Plan`: public plan pricing, duration, traffic, devices, and visibility.
- `PlanInboundRoute`: explicit routing between plans/operators and inbounds.
- Payment/card settings: card number, card owner, optional bank/receipt behavior.
- Revenue Engine settings: enabled state, dry-run state, quiet hours, offer limits, and engine-specific switches.

New installs must set Revenue Engine to `enabled=true` and `dry_run=true`. Dry-run prevents a fresh instance from sending revenue, renewal, upsell, or retention offers before an operator has reviewed the configuration and generated dry-run logs.

## Domains, TLS, and Database

A domain is recommended but not mandatory. A domain-less install can run by IP or localhost if `DJANGO_ALLOWED_HOSTS` and CSRF origins are generated accordingly.

TLS is optional during bootstrap. Enable TLS when a domain exists and DNS points to the server. HSTS should stay `0` until HTTPS is confirmed stable.

SQLite is the current default and supported database mode. PostgreSQL is a future mode and will require settings, dependency, backup, and installer updates before it is advertised as supported.

## Secret Redaction

Redact any setting or DB field whose name contains:

- `SECRET`
- `TOKEN`
- `PASSWORD`
- `KEY`
- `PROXY_URL`
- `CARD`

Installer and doctor summaries should show only whether a secret is configured, not the value. Panel URLs with embedded credentials and full Telegram admin IDs should also be masked.

## Environment Table

| Name | Required | Default/example | Description | Secret? | Installer prompt? |
| --- | --- | --- | --- | --- | --- |
| `DJANGO_SETTINGS_MODULE` | yes | `core.settings.production` | Selects production settings. | no | no |
| `DJANGO_SECRET_KEY` | yes | `generate-with-installer` | Django cryptographic secret. | yes | generate |
| `DJANGO_DEBUG` | no | `False` | Enables Django debug mode. Keep false in production. | no | advanced |
| `DJANGO_ALLOWED_HOSTS` | yes | `example.com,127.0.0.1,localhost` | Comma-separated public host allow-list. | no | yes |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | conditional | `https://example.com` | Required for public HTTPS forms/admin. | no | yes |
| `DJANGO_USE_X_FORWARDED_HOST` | no | `True` | Trust reverse proxy host header. | no | advanced |
| `SQLITE_DATABASE_PATH` | sqlite only | `/opt/vpn-store/data/db.sqlite3` | SQLite database path. | no | yes |
| `DJANGO_LANGUAGE_CODE` | no | `fa` | UI language. | no | yes |
| `DJANGO_TIME_ZONE` | no | `Asia/Tehran` | Django timezone. | no | yes |
| `TELEGRAM_BOT_USERNAME` | no, legacy | empty | Legacy fallback; prefer `BotConfiguration.telegram_bot_username`. | no | no |
| `TELEGRAM_WEBHOOK_RESPONSE_ENABLED` | no | `False` | Optional webhook response optimization; long polling installs keep false. | no | advanced |
| `BOT_API_CONNECT_TIMEOUT_SECONDS` | no | `3` | Bot API connect timeout. | no | advanced |
| `BOT_API_READ_TIMEOUT_SECONDS` | no | `8` | Bot API read timeout. | no | advanced |
| `TELEGRAM_PROXY_URL` | no | empty | Legacy full Telegram proxy URL. Prefer structured proxy fields. | yes | conditional |
| `TELEGRAM_PROXY_PROTOCOL` | no | `http` | Telegram proxy protocol when host/port are used. | no | conditional |
| `TELEGRAM_PROXY_HOST` | no | empty | Telegram proxy host. | no | conditional |
| `TELEGRAM_PROXY_PORT` | no | empty | Telegram proxy port. | no | conditional |
| `TELEGRAM_PROXY_USERNAME` | no | empty | Telegram proxy username. | yes | conditional |
| `TELEGRAM_PROXY_PASSWORD` | no | empty | Telegram proxy password. | yes | conditional |
| `XUI_PANEL_PROXY_URL` | no | empty | Global proxy for panel API calls; prefer per-panel DB proxy when possible. | yes | conditional |
| `SMSFORWARDER_WEBHOOK_TOKEN` | no, legacy | empty | Legacy raw SMSForwarder token; prefer Store token hash. | yes | conditional |
| `PAYMENT_SMS_TIME_ZONE` | no, legacy | `Asia/Tehran` | Legacy payment SMS timezone fallback. | no | conditional |
| `PAYMENT_RECEIPT_MAX_UPLOAD_SIZE` | no | `5242880` | Receipt upload limit in bytes. | no | advanced |
| `INSTALL_REVENUE_ENGINE_ENABLED` | installer only | `true` | Future bootstrap default for Revenue Engine. | no | yes |
| `INSTALL_REVENUE_ENGINE_DRY_RUN` | installer only | `true` | Future bootstrap safety default. | no | yes |
| `INSTALL_RUN_LIVE_BOT_CHECK` | installer only | `false` | Future doctor live Telegram check opt-in. | no | yes |
| `INSTALL_RUN_LIVE_XUI_CHECK` | installer only | `false` | Future doctor live X-UI check opt-in. | no | yes |
| `INSTALL_SEND_TELEGRAM_TEST_MESSAGE` | installer only | `false` | Future test-message opt-in. | no | yes |
| `DJANGO_SECURE_SSL_REDIRECT` | no | `False` | Redirect HTTP to HTTPS. Enable only when origin TLS is stable. | no | advanced |
| `DJANGO_SESSION_COOKIE_SECURE` | no | `True` | Sends session cookies only over HTTPS. | no | advanced |
| `DJANGO_CSRF_COOKIE_SECURE` | no | `True` | Sends CSRF cookies only over HTTPS. | no | advanced |
| `DJANGO_SECURE_HSTS_SECONDS` | no | `0` | HSTS max age. Keep 0 until TLS is verified. | no | advanced |
| `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS` | no | `False` | Include subdomains in HSTS. | no | advanced |
| `DJANGO_SECURE_HSTS_PRELOAD` | no | `False` | HSTS preload flag. | no | advanced |
