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

After install, open Django Admin and start from the owner dashboard:

```text
/admin/store/dashboard/
```

The dashboard shows the overall state and action items from DB/log data only. It does not replace `doctor.sh`.

Use the Setup Center to complete the installation:

```text
/admin/store/setup/
```

For the shortest owner-facing setup path, use the guided wizard:

```text
/admin/store/setup/wizard/
```

Telegram, X-UI/Sanaei, plans, routes, payment details, and Revenue Engine rollout are completed from Django Admin. The wizard and Setup Center do not run live Telegram/X-UI checks automatically. The installer is intentionally minimal; missing integration records after install are setup warnings, not installer failures. Keep Revenue Engine dry-run at first and review logs before real sends.

Full guide: [Post-Install Setup](POST_INSTALL_SETUP.md).

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
