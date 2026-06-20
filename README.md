# Qasedak

Qasedak is a Django VPN store with admin panel, Telegram bot, manual payment review, X-UI/Sanaei integration, backups, updates, and dry-run Revenue Engine.

## Install

Run this on a fresh Ubuntu/Debian server:

```bash
sudo bash -c 'set -e; apt-get update; apt-get install -y git; tmp=$(mktemp -d); git clone https://github.com/isajad7/qasedak.git "$tmp/qasedak"; bash "$tmp/qasedak/scripts/install.sh"'
```

The installer asks what it needs: install path, domain/TLS, admin user, database path, systemd/nginx, and doctor check.

Default install path is:

```text
/opt/qasedak
```

## Update

For the default install path:

```bash
sudo bash -c 'set -e; apt-get update; apt-get install -y git; tmp=$(mktemp -d); git clone https://github.com/isajad7/qasedak.git "$tmp/qasedak"; bash "$tmp/qasedak/scripts/update.sh" --install-dir /opt/qasedak --source-dir "$tmp/qasedak" --yes --restart'
```

If you installed somewhere else, change `/opt/qasedak` in the update command.

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
