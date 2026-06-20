# Qasedak

Qasedak is a Django VPN store with admin panel, Telegram bot, manual payment review, X-UI/Sanaei integration, backups, updates, and dry-run Revenue Engine.

## Install

Run this on a fresh Ubuntu/Debian server:

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/install_from_github.sh | sudo bash
```

The installer asks what it needs: install path, domain/TLS, admin user, database path, systemd/nginx, and doctor check.
It also installs Python 3.12/venv if the server Python is older.

Default install path is:

```text
/opt/qasedak
```

## Update

For the default install path:

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/update_from_github.sh | sudo bash
```

If you installed somewhere else:

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/update_from_github.sh | sudo env QASEDAK_INSTALL_DIR=/your/path bash
```

## After Install

Open Django Admin and finish store setup:

- Store name/support/payment card
- Telegram bot settings
- X-UI/Sanaei panel
- Inbounds
- Plans
- Plan routes
- Test purchase

Guide: [Post-Install Setup](docs/POST_INSTALL_SETUP.md)

## Useful Commands

Doctor:

```bash
sudo /opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --no-fail
```

Backup:

```bash
sudo /opt/qasedak/scripts/backup.sh --install-dir /opt/qasedak --output-dir /opt/qasedak/backups --yes
```

## Notes

- Do not commit `.env`, `install.config.json`, database, media, backups, logs, or virtualenv files.
- Telegram and X-UI live checks are opt-in.
- Revenue Engine starts enabled but dry-run by default.
