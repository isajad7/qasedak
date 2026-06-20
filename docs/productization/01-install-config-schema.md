# Productization P1 - Install Config Schema

This document defines the proposed input schema for the future P2 management command:

```bash
python manage.py bootstrap_install --config install.config.json
```

P1 is design-only. No executable installer or destructive bootstrap command is introduced in this phase.

## Goals

- Keep process/runtime settings in `.env`.
- Keep product and integration objects in the database.
- Make fresh installs safe by default.
- Never store real secrets in sample files.
- Provide a stable contract for P2 `bootstrap_install`.

## Example: `install.config.example.json`

```json
{
  "app": {
    "install_dir": "/opt/vpn-store",
    "domain": "example.com",
    "enable_tls": true,
    "timezone": "Asia/Tehran",
    "language": "fa"
  },
  "admin": {
    "username": "admin",
    "email": "admin@example.com",
    "password_mode": "generate"
  },
  "database": {
    "engine": "sqlite",
    "sqlite_path": "/opt/vpn-store/data/db.sqlite3"
  },
  "store": {
    "name": "VPN Store",
    "english_name": "VPN Store",
    "payment_mode": "manual_card"
  },
  "telegram": {
    "enabled": true,
    "bot_token": "SECRET",
    "bot_username": "your_bot",
    "admin_ids": ["123456789"],
    "proxy_enabled": false
  },
  "xui": {
    "configure_now": false,
    "panel_url": "",
    "username": "",
    "password": ""
  },
  "revenue_engine": {
    "enabled": true,
    "dry_run": true
  }
}
```

The sample uses placeholders only. A real config file must not be committed if it contains tokens, passwords, card numbers, or panel credentials.

## Top-Level Sections

| Section | Purpose | P2 DB/env target |
| --- | --- | --- |
| `app` | Install path, domain/TLS mode, language, timezone. | `.env`, service/nginx templates, Store defaults. |
| `admin` | Initial Django admin account. | `auth.User`. |
| `database` | Database mode and path. | `.env` for SQLite; future Postgres env later. |
| `store` | Store identity and payment mode. | `Store`. |
| `telegram` | Telegram bot bootstrap. | `BotConfiguration`; optional proxy env. |
| `xui` | Optional panel bootstrap. | `Panel`; later optional `Inbound`. |
| `revenue_engine` | Fresh-install safety defaults. | `Store.revenue_engine_enabled`, `Store.revenue_engine_dry_run`. |

## Field Notes

### `app`

- `install_dir`: absolute path for app files. Suggested default: `/opt/vpn-store`.
- `domain`: optional public domain. Use `example.com` only in samples.
- `enable_tls`: true only when a real domain and DNS are ready.
- `timezone`: IANA timezone. Default can be `Asia/Tehran`.
- `language`: current supported values are `fa` and `en`.

If no domain is provided, P2 should support an IP/localhost install and generate matching `DJANGO_ALLOWED_HOSTS` and CSRF origins.

### `admin`

- `password_mode=generate` means P2 generates a password and prints it once.
- Future allowed values can be `generate`, `prompt`, or `disabled`.
- Admin passwords must never be written to docs or logs.

### `database`

- `engine=sqlite` is the only supported P1/P2 target.
- `sqlite_path` should default to `<install_dir>/data/db.sqlite3`.
- PostgreSQL is a future extension and should not appear as supported until settings, dependencies, backup, and upgrade docs exist.

### `store`

P2 should create or update an active Store with product-neutral names. Payment details such as card number and owner should be prompted or supplied by a private real config file, not by the public example.

### `telegram`

- `bot_token` is a secret placeholder in the sample.
- `bot_username` should be stored without `@`.
- `admin_ids` are needed for admin notifications.
- `proxy_enabled=false` means Telegram proxy fields are ignored.

Future schema versions can add structured proxy fields:

```json
{
  "proxy_enabled": true,
  "proxy": {
    "mode": "structured",
    "protocol": "http",
    "host": "proxy.example.com",
    "port": "8080",
    "username": "",
    "password": ""
  }
}
```

### `xui`

When `configure_now=false`, P2 should skip Panel, Inbound, Plan, and PlanInboundRoute creation and report that the store is not yet sellable.

When `configure_now=true`, P2 should support Panel creation first. Inbound sync and plan route bootstrap can be added incrementally and must avoid live X-UI calls unless a separate live-check flag is supplied.

### `revenue_engine`

Fresh installs must use:

```json
{
  "enabled": true,
  "dry_run": true
}
```

P2 should refuse or warn loudly if a bootstrap config tries to set `dry_run=false`. Real sends should require a later explicit operator action after dry-run logs and targeting are reviewed.

## Redaction Rules

Any CLI summary, dry-run output, generated report, or error message must redact fields containing:

- `secret`
- `token`
- `password`
- `key`
- `proxy_url`
- `card`

Use booleans such as `configured=true`, masked IDs, or last-four hints instead of raw values.

## P2 Acceptance Contract

The future `bootstrap_install` command should:

- Accept this JSON structure from `--config`.
- Validate required fields before writing.
- Be idempotent.
- Support `--dry-run`.
- Create the initial admin user and DB-backed objects.
- Set Revenue Engine `enabled=true`, `dry_run=true` for new installs.
- Avoid Telegram/X-UI live calls unless an explicit live-check option is passed.
- Never print or log secrets.
