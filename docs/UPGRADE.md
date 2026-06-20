# Upgrade

`scripts/update.sh` updates an installed VPN Store from a local source checkout. It is intentionally local-repo based; public GitHub release/curl install belongs to a later productization phase.

## Dry-Run

Run this first:

```bash
scripts/update.sh \
  --dry-run \
  --install-dir /opt/vpn-store \
  --source-dir "$(pwd)" \
  --yes
```

Dry-run prints the plan only. It does not create a backup, rsync files, install dependencies, run tests, migrate, collect static files, restart services, or run a real doctor check.

## Real Update

```bash
sudo /path/to/source/scripts/update.sh \
  --install-dir /opt/vpn-store \
  --source-dir /path/to/source \
  --backup-dir /opt/vpn-store/backups \
  --yes
```

Flow:

1. Validate install/source directories and `.env`.
2. Run `scripts/backup.sh` before changing files.
3. `rsync` source to install dir while excluding runtime and secret-heavy paths.
4. Reuse or create `venv`.
5. Run `pip install -r requirements.txt`.
6. Run `manage.py check`.
7. Optionally run focused tests with `--run-tests`.
8. Run `manage.py migrate --plan`.
9. Run `manage.py migrate`.
10. Run `collectstatic --noinput`.
11. Optionally restart existing services with `--restart`.
12. Run post-update `doctor.sh`.

## Backup Requirement

Updates are blocked unless a backup succeeds. `--skip-backup` exists only for emergencies and requires typing `SKIP BACKUP` interactively; `--yes` does not bypass that confirmation.

## Restart

Restarts are off by default:

```bash
sudo /path/to/source/scripts/update.sh \
  --install-dir /opt/vpn-store \
  --source-dir /path/to/source \
  --restart \
  --yes
```

Only existing `vpn-store-web.service` and `vpn-store-telegram.service` are restarted.

## Rollback Guidance

`update.sh` does not perform automatic rollback. If a step fails, it reports the backup path when available. Manual rollback is:

```bash
sudo systemctl stop vpn-store-web.service vpn-store-telegram.service
TMPDIR=$(mktemp -d)
tar -xzf /opt/vpn-store/backups/vpn-store-backup-YYYYMMDD-HHMMSS.tar.gz -C "$TMPDIR"
sudo install -m 0640 "$TMPDIR"/vpn-store-backup-*/database/db.sqlite3 /opt/vpn-store/data/db.sqlite3
sudo install -m 0600 "$TMPDIR"/vpn-store-backup-*/runtime/.env /opt/vpn-store/.env
sudo install -m 0600 "$TMPDIR"/vpn-store-backup-*/runtime/install.config.json /opt/vpn-store/install.config.json
/opt/vpn-store/scripts/doctor.sh --install-dir /opt/vpn-store --no-fail
sudo systemctl start vpn-store-web.service vpn-store-telegram.service
```

Restore `media/` with `rsync` only if the backup was created with `--include-media`.
