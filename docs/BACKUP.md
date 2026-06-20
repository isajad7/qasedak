# Backup

Use `scripts/backup.sh` before every real update and before risky maintenance. Backups contain secrets because they include `.env`; store them with restricted permissions and do not share archives publicly.

## Dry-Run

```bash
scripts/backup.sh --dry-run --install-dir /opt/vpn-store --output-dir /opt/vpn-store/backups --yes
```

Dry-run validates the install directory and database path, prints the plan, and writes nothing.

## Create a Backup

```bash
sudo /opt/vpn-store/scripts/backup.sh \
  --install-dir /opt/vpn-store \
  --output-dir /opt/vpn-store/backups \
  --yes
```

Output format:

```text
/opt/vpn-store/backups/vpn-store-backup-YYYYMMDD-HHMMSS.tar.gz
```

The script prints a SHA256 checksum after a successful archive.

## Include Media

Media files are excluded by default so routine backups stay small. Include them when uploads, receipts, or user media must be captured:

```bash
sudo /opt/vpn-store/scripts/backup.sh \
  --install-dir /opt/vpn-store \
  --output-dir /opt/vpn-store/backups \
  --include-media \
  --yes
```

`static_root/` is not included because it is rebuildable with:

```bash
/opt/vpn-store/venv/bin/python /opt/vpn-store/manage.py collectstatic --noinput
```

## Retention

Keep only the newest 10 backup archives:

```bash
sudo /opt/vpn-store/scripts/backup.sh \
  --install-dir /opt/vpn-store \
  --output-dir /opt/vpn-store/backups \
  --keep-last 10 \
  --yes
```

## Verify Checksum

```bash
sha256sum /opt/vpn-store/backups/vpn-store-backup-YYYYMMDD-HHMMSS.tar.gz
```

Compare the output with the checksum printed by `backup.sh`.

## Archive Contents

Backups include:

- SQLite database copied through `sqlite3 .backup` when available
- `.env`
- `install.config.json`
- generated VPN Store systemd/nginx files when present
- `media/` only with `--include-media`
- `manifest.json` with timestamp, install path, included sections, SHA256 file checksums, and redacted runtime/config previews

## Manual Restore: SQLite

Stop services first if they exist:

```bash
sudo systemctl stop vpn-store-web.service vpn-store-telegram.service
```

Extract to a temporary directory:

```bash
TMPDIR=$(mktemp -d)
tar -xzf /opt/vpn-store/backups/vpn-store-backup-YYYYMMDD-HHMMSS.tar.gz -C "$TMPDIR"
```

Copy the database back to the configured path:

```bash
sudo install -m 0640 "$TMPDIR"/vpn-store-backup-*/database/db.sqlite3 /opt/vpn-store/data/db.sqlite3
```

Then run checks and restart only after review:

```bash
/opt/vpn-store/scripts/doctor.sh --install-dir /opt/vpn-store --no-fail
sudo systemctl start vpn-store-web.service vpn-store-telegram.service
```

## Manual Restore: Media

Only archives created with `--include-media` contain media:

```bash
sudo rsync -a "$TMPDIR"/vpn-store-backup-*/media/ /opt/vpn-store/media/
```

## Manual Restore: `.env` and Config

Review before overwriting live runtime files:

```bash
sudo install -m 0600 "$TMPDIR"/vpn-store-backup-*/runtime/.env /opt/vpn-store/.env
sudo install -m 0600 "$TMPDIR"/vpn-store-backup-*/runtime/install.config.json /opt/vpn-store/install.config.json
```

Never paste `.env` or backup archives into tickets, logs, chat, or public repos.
