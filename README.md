# Qasedak

Qasedak is a Django VPN store with admin panel, Telegram bot, manual payment review, X-UI/Sanaei integration, backups, updates, and dry-run Revenue Engine.

## Install

Run this on a fresh Ubuntu/Debian server:

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/install_from_github.sh | sudo bash
```

The installer asks what it needs: install path, domain/TLS, admin user, database engine, systemd/nginx, and doctor check.
PostgreSQL is the default for new production installs; SQLite remains supported for development, tests, small installs, and fallback.
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

Open Django Admin. The admin home page is a responsive Qasedak dashboard with KPI cards, product workspaces, action items, recent activity, and the traditional Django model management collapsed at the bottom:

```text
/admin/
```

The detailed owner dashboard remains available at:

```text
/admin/store/dashboard/
```

Use these dashboards to see today’s orders, pending receipts, revenue, active/expiring services, saved panel health, Telegram status, and action items. They read DB/log state only and are not a replacement for `doctor.sh`.

For business reports and CSV exports, open:

```text
/admin/store/reports/
/admin/store/reports/export/?report=sales
```

Reports include today, 7-day, 30-day, this-month, previous-month, and custom date filters. Period metrics such as revenue, orders, customers, reminders, support, campaigns, and Revenue Engine logs are separate from current-state metrics such as active services and expiring services. Panel usage marked unknown or insufficient is missing/partial data, not zero usage. CSV exports are aggregate/redacted and do not include secrets, full phone/email values, config links, UUIDs, card numbers, or proxy credentials.

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

For group messaging campaigns, open:

```text
/admin/store/campaigns/
```

Owner flow: draft message -> choose audience -> DB-only preview -> exact `SEND_CAMPAIGN_<id>` confirmation -> queued processing. The web confirm step materializes recipients and marks the campaign queued; it does not loop over recipients or call Telegram. Process queued campaigns outside the request with:

```bash
./venv/bin/python manage.py process_broadcast_queue --batch-size 50
```

See [Campaigns & Broadcasts](docs/CAMPAIGNS.md) for preview metrics, retry/cancel behavior, safe CSV export, and the difference between direct messages, campaigns, and Revenue Engine.

For backups, restore validation, and server transfer, open:

```text
/admin/store/backups/
```

The Backup & Restore Center creates private `qasedak-backup-YYYYMMDD-HHMMSS.tar.gz` packages, validates uploaded packages, and generates an SSH restore command. Real restore apply is command-based through `scripts/restore.sh`; Admin does not replace the database inside a web request. See [Backup](docs/BACKUP.md) and [Restore](docs/RESTORE.md).

For Revenue Engine rollout and daily guardrails, open:

```text
/admin/store/revenue/control/
```

Dry-run means offers are logged and reported without sending real Telegram messages. Enable real-send only after reviewing dry-run reports and local safety warnings; use reset safe defaults to quickly return to enabled + dry-run with conservative caps.

For staff roles and access control, sync role presets and open:

```bash
./venv/bin/python manage.py sync_staff_roles --apply
```

```text
/admin/store/staff/
```

Use Staff Access Center to create non-superuser staff and assign product roles such as Store Owner, Support Agent, Finance, Catalog Manager, Technical Operator, Marketing Manager, or Analyst. Store Owner is not the same as Django superuser. See [Staff Roles](docs/STAFF_ROLES.md).

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

For Sanaei/3X-UI v3.x and multi-node panels, read the compatibility notes before routing paid checkout traffic. Qasedak supports node-aware read-only discovery, guarded operations, and deferred paid provisioning: modern paid orders do not create a remote client until payment approval, then create and verify an enabled client on the frozen panel/node/inbound scope. See [3X-UI Multi-Node Operations](docs/integrations/3x-ui-multinode.md) and the [compatibility audit](docs/integrations/3x-ui-multinode-compatibility-audit.md).
- Payment details
- Service workbench review for active/expiring/expired clients
- Service reconciliation for local vs X-UI status and local-only soft-delete of confirmed missing clients. See [Service Reconciliation](docs/SERVICE_RECONCILIATION.md).
- Revenue Control Center dry-run review before any real-send rollout
- Test purchase

The wizard and Setup Center read local state and do not run Telegram/X-UI live checks automatically.

Guide: [Post-Install Setup](docs/POST_INSTALL_SETUP.md)

## Useful Commands

Admin CSS:

```bash
npm run build:admin-css
```

The compiled `static/admin/qasedak_admin_tailwind.css` file is part of the release artifact, so production runtime does not need Node.js.

Doctor:

```bash
sudo /opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --no-fail
```

Backup:

```bash
sudo /opt/qasedak/scripts/backup.sh --install-dir /opt/qasedak --output-dir /opt/qasedak/backups --yes
```

Backups are engine-aware: SQLite uses `sqlite3 .backup` when available, and PostgreSQL uses `pg_dump -Fc` to `database/db.postgres.dump`.

## Notes

- Telegram and X-UI live checks are opt-in.
- PostgreSQL is the default production database; SQLite remains for development, tests, small installs, and legacy fallback.
- Service Workbench GET never calls X-UI; reconciliation is POST-only and never deletes remote clients.
- Revenue Engine starts enabled but dry-run by default.
- Real-send is a POST-only admin action with explicit confirmation and local safety checks.
- Staff workflow access is enforced in custom admin views and POST actions, not only hidden from navigation.
