# Productization P4 - Systemd, Nginx, TLS Layer

P4 adds optional production runtime wiring on top of the P3 local-repo installer.

## Scope

Implemented:

- optional systemd service rendering and enablement
- optional nginx HTTP reverse proxy rendering
- optional certbot nginx TLS flow
- domain-less HTTP/IP mode
- doctor checks for systemd and nginx
- timer templates for later phases

Not implemented:

- automatic timer enablement
- backup/update scripts
- full remote doctor
- GitHub release/curl install mode

## Generated Files

Systemd:

- `/etc/systemd/system/<web-service-name>.service`
- `/etc/systemd/system/<telegram-service-name>.service`

Nginx:

- `/etc/nginx/sites-available/<nginx-site-name>.conf`
- `/etc/nginx/sites-enabled/<nginx-site-name>.conf`

Runtime:

- `<install-dir>/.env`
- `<install-dir>/install.config.json`
- `<install-dir>/data/`
- `<install-dir>/media/`
- `<install-dir>/static_root/`
- `<install-dir>/backups/`
- `<install-dir>/logs/`

Existing generated system paths are backed up with a timestamp before overwrite after confirmation.

## Templates

Template roots:

- `scripts/templates/systemd/`
- `scripts/templates/systemd/timers/`
- `scripts/templates/nginx/`

The web service runs gunicorn bound to `127.0.0.1:{{GUNICORN_PORT}}`. The Telegram service runs `manage.py run_telegram_polling`. Unit files reference `EnvironmentFile={{INSTALL_DIR}}/.env`; secrets are not embedded in unit files.

The nginx template supports:

- domain HTTP mode through `server_name {{DOMAIN}}`
- no-domain/IP mode through `server_name _`
- static alias at `{{INSTALL_DIR}}/static_root/`
- media alias at `{{INSTALL_DIR}}/media/`
- standard reverse proxy headers

## TLS Safety

TLS is only enabled when all are true:

- a domain is configured
- nginx is enabled
- `--with-tls` is used or the operator confirms the prompt
- DNS lookup succeeds

The installer uses `certbot --nginx -d <domain>` and does not enable HSTS automatically. If DNS lookup fails, TLS is skipped and the install continues in HTTP mode. TLS without a domain is forbidden and is treated as a safe skip.

After successful TLS, `.env` is updated:

- `DJANGO_SECURE_SSL_REDIRECT=True`
- `DJANGO_SESSION_COOKIE_SECURE=True`
- `DJANGO_CSRF_COOKIE_SECURE=True`
- `DJANGO_CSRF_TRUSTED_ORIGINS=https://domain`

## Domain-Less Mode

Domain-less installs stay valid:

- allowed hosts include detected server IP, `127.0.0.1`, and `localhost`
- nginx uses `server_name _`
- TLS is not offered
- SSL redirect and secure cookie flags remain disabled

## Timers

P4 ships timer templates only. The installer does not auto-enable timers.

Templates include:

- renewal reminders
- panel health
- daily admin report
- panel usage snapshots
- panel daily usage
- Revenue Engine dry-run scan

The Revenue Engine timer template uses `run_revenue_scan --dry-run`. No revenue real-send timer is enabled in P4.

## P5 Next Steps

- `backup.sh`
- `update.sh`
- full doctor
- explicit timer enablement UX
- GitHub release install mode
