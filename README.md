# VPN Store

VPN Store is a Django application for selling and operating VPN plans with manual payment review, Telegram bot workflows, X-UI/Sanaei panel integration, renewal reminders, usage/health checks, and a dry-run-first Revenue Engine.

## Features

- Django admin and storefront for stores, plans, orders, customers, panels, inbounds, and routing.
- Telegram bot flows for buying, renewals, support, referrals, payment guidance, and admin operations.
- Manual card-to-card payment matching through receipt uploads and SMS parsing.
- X-UI/Sanaei client provisioning with panel health and usage snapshot tooling.
- Minimal local installer plus doctor, backup, update, and timer scripts.
- Revenue Engine defaults to dry-run for fresh installs.

## Requirements

- Python 3.12+
- Django dependencies from `requirements.txt`
- Ubuntu/Debian for the bare-metal installer path
- SQLite by default
- Optional nginx, systemd, certbot/TLS, Telegram, and X-UI access

## Quick Start

Local repository install is the supported path today. The default installer only brings up infrastructure; Store identity, payment, Telegram, X-UI/Sanaei, plans, and routes are completed afterward in Django Admin.

```bash
git clone https://github.com/OWNER/REPO.git
cd REPO
cp .env.example .env
scripts/install.sh --dry-run --yes --install-dir /opt/qasedak
```

For a real local-repo install, review `docs/INSTALL.md` and run:

```bash
sudo bash scripts/install.sh
```

Then complete [Post-Install Setup](docs/POST_INSTALL_SETUP.md) from Django Admin. Revenue Engine is enabled but dry-run by default for fresh installs.

Future public install placeholder:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/OWNER/REPO/main/scripts/install.sh)
```

Replace `OWNER/REPO` only after the GitHub repository is created.

## Security Notes

- Never commit `.env`, `install.config.json`, databases, media uploads, backups, logs, virtualenvs, or `node_modules`.
- Keep Telegram/X-UI live checks disabled unless you intentionally request them.
- Revenue Engine is installed as enabled but dry-run by default.
- Do not paste bot tokens, proxy credentials, card numbers, panel passwords, or backup archives into public issues, docs, logs, or release assets.

## Documentation

- [Install](docs/INSTALL.md)
- [Post-Install Setup](docs/POST_INSTALL_SETUP.md)
- [Configuration](docs/CONFIGURATION.md)
- [Backup](docs/BACKUP.md)
- [Upgrade](docs/UPGRADE.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Release](docs/RELEASE.md)

## License

TODO - choose before public release. See [license decision notes](docs/productization/06-license-decision.md).
