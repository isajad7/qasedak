# Install

Use one command on a fresh Ubuntu/Debian server:

```bash
sudo bash -c 'set -e; apt-get update; apt-get install -y git; tmp=$(mktemp -d); git clone https://github.com/isajad7/qasedak.git "$tmp/qasedak"; bash "$tmp/qasedak/scripts/install.sh"'
```

The installer asks for the needed basics:

- install directory
- optional domain
- TLS when a domain is available
- admin username/email/password
- SQLite database path
- systemd
- nginx
- non-live doctor check

Default install directory:

```text
/opt/qasedak
```

After install, open Django Admin and complete [Post-Install Setup](POST_INSTALL_SETUP.md).

## Advanced Install

Only use this if you want Telegram/X-UI/Plan/Payment questions during install:

```bash
sudo bash -c 'set -e; apt-get update; apt-get install -y git; tmp=$(mktemp -d); git clone https://github.com/isajad7/qasedak.git "$tmp/qasedak"; bash "$tmp/qasedak/scripts/install.sh" --advanced'
```

## Existing Config

```bash
sudo bash -c 'set -e; apt-get update; apt-get install -y git; tmp=$(mktemp -d); git clone https://github.com/isajad7/qasedak.git "$tmp/qasedak"; bash "$tmp/qasedak/scripts/install.sh" --config /root/install.config.json'
```

## Doctor

```bash
sudo /opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --no-fail
```

Live Telegram/X-UI checks are optional:

```bash
sudo /opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --live-bot --live-xui --no-fail
```
