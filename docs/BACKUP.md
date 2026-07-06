# Backup

Qasedak production backups are PostgreSQL-first. SQLite remains supported for development, small installs, and legacy rollback packages.

Backups can be created from Django Admin:

```text
/admin/store/backups/
```

The Admin page title is:

```text
پشتیبان‌گیری و انتقال سرور
```

Admin-created archives are stored under the private backup root, not `media/` or `static/`:

```text
QASEDAK_PRIVATE_BACKUP_ROOT=/opt/qasedak/backups/admin_center
```

## Package Format

Standard archive name:

```text
qasedak-backup-YYYYMMDD-HHMMSS.tar.gz
```

Required archive structure:

```text
manifest.json
checksums.sha256
database/
  db.postgres.dump
media/ optional
env/
  production.env optional
system/
  systemd/*.service optional
  nginx/*.conf optional
```

SQLite legacy packages use:

```text
database/db.sqlite3
```

`manifest.json` records the backup version, timestamp, app/git metadata, DB engine/vendor, backup type, media/env/system flags, migration summary, file counts/sizes, warnings, and redaction policy. It must not contain raw secret keys, bot tokens, panel passwords, DB passwords, full card numbers, config links, UUIDs, full phone/email/chat IDs, or subscription links.

`checksums.sha256` covers the archive payload files and is verified before any restore plan is built.

## Backup Types

- `فقط دیتابیس`: database only.
- `دیتابیس + فایل‌ها`: database plus media.
- `بسته انتقال سرور`: database, media, `.env`, install config, and system reference files when available.

The `.env` option is useful for server migration, but the archive then contains secrets. CLI backups exclude env and system reference files unless `--include-env` or `--include-system` is passed. Keep env-containing archives on the server with restricted permissions and download them only when necessary.

## CLI Backup

Dry-run:

```bash
sudo /opt/qasedak/scripts/backup.sh \
  --install-dir /opt/qasedak \
  --output-dir /opt/qasedak/backups \
  --dry-run
```

Create a PostgreSQL backup:

```bash
sudo /opt/qasedak/scripts/backup.sh \
  --install-dir /opt/qasedak \
  --output-dir /opt/qasedak/backups \
  --yes
```

PostgreSQL uses `pg_dump -Fc` and writes:

```text
database/db.postgres.dump
```

The script reads `POSTGRES_PASSWORD` from `.env` through `PGPASSWORD`; it does not print the password.

Include media:

```bash
sudo /opt/qasedak/scripts/backup.sh \
  --install-dir /opt/qasedak \
  --output-dir /opt/qasedak/backups \
  --include-media \
  --yes
```

Create a server-transfer package:

```bash
sudo /opt/qasedak/scripts/backup.sh \
  --install-dir /opt/qasedak \
  --output-dir /opt/qasedak/backups \
  --include-media \
  --include-env \
  --include-system \
  --yes
```

`static_root/` is not backed up because it is rebuildable:

```bash
/opt/qasedak/venv/bin/python /opt/qasedak/manage.py collectstatic --noinput
```

## Retention

Keep the newest 10 archives:

```bash
sudo /opt/qasedak/scripts/backup.sh \
  --install-dir /opt/qasedak \
  --output-dir /opt/qasedak/backups \
  --keep-last 10 \
  --yes
```

Recommended baseline: keep daily backups for 7 days, weekly backups for 4 weeks, and one fresh server-transfer package before every migration or major update.

## Verify

Compare archive SHA256:

```bash
sha256sum /opt/qasedak/backups/qasedak-backup-YYYYMMDD-HHMMSS.tar.gz
```

Validate a backup package without applying it:

```bash
sudo /opt/qasedak/scripts/restore.sh \
  --install-dir /opt/qasedak \
  --backup-file /opt/qasedak/backups/qasedak-backup-YYYYMMDD-HHMMSS.tar.gz \
  --dry-run
```

## Server Migration Flow

1. On the old server, create a server-transfer backup.
2. Copy the archive through a private channel.
3. Install Qasedak on the new server.
4. Open `/admin/store/backups/`.
5. Upload and Validate the package.
6. Review the restore plan.
7. Generate the command and run it over SSH.

Restore is intentionally command-based. Django Admin validates and generates the command; it does not replace a live database inside a web request.

See [Restore](RESTORE.md) for the controlled restore runbook.
