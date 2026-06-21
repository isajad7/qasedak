# Qasedak

Qasedak is a Django VPN store with admin panel, Telegram bot, manual payment review, X-UI/Sanaei integration, backups, updates, and dry-run Revenue Engine.

## Install

Run this on a fresh Ubuntu/Debian server:

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/install_from_github.sh | sudo bash
```

The installer asks what it needs: install path, domain/TLS, admin user, database path, systemd/nginx, and doctor check.
It also installs Python 3.12/venv if the server Python is older.
If an old/partial install exists, it warns before doing anything destructive.

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

## Delete

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/uninstall_from_github.sh | sudo bash
```

## After Install

Open Django Admin and start from the owner dashboard:

```text
/admin/store/dashboard/
```

Use it to see today’s orders, pending receipts, revenue, active/expiring services, saved panel health, Telegram status, and action items. The dashboard is an admin UI overview, not a replacement for `doctor.sh`.

For daily order/payment work, open:

```text
/admin/store/orders/workbench/
```

Owner flow: pending receipt -> review -> approve/reject -> delivery status. Actions are explicit POST confirmations and reuse the existing order services.

For customer and VPN service work, open:

```text
/admin/store/services/workbench/
```

Owner flow: customer -> service -> usage/expiry -> resend/update/disable. GET pages are read-only; live Telegram/X-UI work happens only through explicit POST actions.

For customer support and one-to-one messages, open:

```text
/admin/store/support/workbench/
/admin/store/customers/<id>/message/
```

Support replies and direct customer messages are POST-only, require CSRF plus explicit confirmation, and target only the selected customer. These pages do not provide group audience selection; use the existing campaign/broadcast tools separately and with care.

For Revenue Engine rollout and daily guardrails, open:

```text
/admin/store/revenue/control/
```

Dry-run means offers are logged and reported without sending real Telegram messages. Enable real-send only after reviewing dry-run reports and local safety warnings; use reset safe defaults to quickly return to enabled + dry-run with conservative caps.

Use the Setup Center to complete installation:

```text
/admin/store/setup/
```

Use the guided wizard for the short post-install flow:

```text
/admin/store/setup/wizard/
```

The installer is intentionally minimal. Finish store setup from Django Admin:

- Store name/support/payment card
- Telegram bot settings
- X-UI/Sanaei panel
- Inbounds
- Plans
- Plan routes
- Payment details
- Service workbench review for active/expiring/expired clients
- Revenue Control Center dry-run review before any real-send rollout
- Test purchase

The wizard and Setup Center read local state and do not run Telegram/X-UI live checks automatically.

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

- Telegram and X-UI live checks are opt-in.
- Revenue Engine starts enabled but dry-run by default.
- Real-send is a POST-only admin action with explicit confirmation and local safety checks.
