# Productization P2 - Bootstrap Install Command

P2 adds a Django management command that creates the initial database-backed install objects from a private JSON config:

```bash
python manage.py bootstrap_install --config install.config.json
```

This is not a shell installer. It does not configure systemd, nginx, TLS, DNS, cron, backups, virtualenvs, `.env`, `data/`, `media/`, logs, or the SQLite file path. P3 can wrap this command from an interactive bare-metal installer.

## Example Config

Start from:

```bash
docs/productization/install.config.example.json
```

Copy it outside version control before adding real secrets. The example contains placeholders only. Prefer env-backed admin and X-UI passwords:

```json
{
  "admin": {
    "username": "admin",
    "password_env": "VPN_STORE_ADMIN_PASSWORD"
  },
  "xui": {
    "password_env": "VPN_STORE_XUI_PASSWORD"
  }
}
```

`admin.password_mode=generate` is intentionally rejected in P2. P3 should own one-time password generation/output.

## Usage

Dry-run validation and planning:

```bash
python manage.py bootstrap_install --config install.config.json --dry-run
```

Real run in a script or non-interactive shell:

```bash
python manage.py bootstrap_install --config install.config.json --yes
```

Leave existing matching objects unchanged:

```bash
python manage.py bootstrap_install --config install.config.json --yes --no-update-existing
```

Explicit live checks:

```bash
python manage.py bootstrap_install --config install.config.json --yes --live-check
```

Without `--yes`, a real run asks for confirmation on an interactive terminal. In non-interactive mode it fails safely.

## What It Creates

- `auth.User`: the initial active staff superuser.
- `Store`: product identity, payment placeholders/settings, routing defaults, and Revenue Engine safety settings.
- `BotConfiguration`: Telegram bot token, username, admin IDs, and optional force-join settings when `telegram.enabled=true`.
- `Panel`: X-UI/Sanaei panel settings when `xui.configure_now=true`.
- `Inbound`: configured sellable inbound records.
- `Plan`: public sellable plans.
- `PlanInboundRoute`: explicit plan-to-inbound routes.

When `xui.configure_now=false`, the command skips Panel, Inbound, Plan, and Route creation and reports that admin setup is incomplete.

## Idempotency

The command uses stable natural keys:

- `Store.slug`, falling back to domain/name.
- `BotConfiguration` by store, Telegram provider, username/name.
- `Panel` by store and name/url.
- `Inbound` by panel and X-UI inbound ID.
- `Plan` by store and slug.
- `PlanInboundRoute` by plan, inbound, and empty operator.

Running the same config again updates matching objects by default and does not create duplicates. `--no-update-existing` changes existing matches to skipped while still allowing missing objects to be created.

## Dry-Run

`--dry-run` performs validation and DB lookups only. It does not save models, hash or store passwords, call Telegram, call X-UI, or resolve env-backed secrets. The summary reports `would_create`, `would_update`, and `would_skip`.

## Secret Redaction

Command output is passed through redaction for fields containing:

- `secret`
- `token`
- `password`
- `key`
- `proxy_url`
- `card`

Telegram admin IDs are masked. Live-check errors are sanitized before display. Raw admin passwords, bot tokens, card numbers, and panel credentials must not appear in stdout/stderr.

## Revenue Engine Safety

Fresh installs must start with:

```json
{
  "revenue_engine": {
    "enabled": true,
    "dry_run": true
  }
}
```

The command rejects `revenue_engine.dry_run=false` and writes `Store.revenue_engine_dry_run=True`. It also uses conservative P2 caps: daily per user `1`, weekly per user `3`, total daily cap `100`, offer cooldown `24h`, retention cooldown `72h`, and minimum AI confidence `0.50`.

## Live Checks

No live network call is made by default.

With `--live-check`, the command calls Telegram `getMe` through the existing bot client and attempts X-UI login through the existing panel client. Failures are warnings by default. Add `--fail-on-live-check-error` if a wrapper wants live-check failure to abort the command.

Live checks are skipped in `--dry-run`.

## Validation Failures

The command fails before writing when required install data is missing or invalid, including:

- Missing admin username or password/password env.
- Invalid timezone, language, domain, or X-UI URL.
- Telegram enabled without token/admin IDs.
- Non-numeric Telegram admin IDs.
- X-UI configure-now without panel credentials, inbounds, plans, or routes.
- Routes referencing unknown plans or inbounds.
- `revenue_engine.dry_run=false`.

## Next Phase

Productization P3 should add `scripts/install.sh`: an interactive bare-metal installer that prepares `.env`, creates runtime directories, collects secrets safely, then calls `bootstrap_install`.
