# Install

Use one command on a fresh Ubuntu/Debian server:

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/install_from_github.sh | sudo bash
```

The installer asks for the needed basics:

- install directory
- optional domain
- server public IP
- TLS when a domain is available
- admin username/email/password
- SQLite database path
- systemd
- nginx
- non-live doctor check

It also installs Python 3.12/venv if the server Python is older.
If an old/partial install exists, it warns before doing anything destructive.
At the end, it prints the admin panel URL, username, and password.

Default install directory:

```text
/opt/qasedak
```

After install, open Django Admin and complete [Post-Install Setup](POST_INSTALL_SETUP.md).

## Advanced Install

Only use this if you want Telegram/X-UI/Plan/Payment questions during install:

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/install_from_github.sh | sudo bash -s -- --advanced
```

## Existing Config

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/install_from_github.sh | sudo bash -s -- --config /root/install.config.json
```

## Doctor

```bash
sudo /opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --no-fail
```

Live Telegram/X-UI checks are optional:

```bash
sudo /opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --live-bot --live-xui --no-fail
```

## Delete

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/uninstall_from_github.sh | sudo bash
```
