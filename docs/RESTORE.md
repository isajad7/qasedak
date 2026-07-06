# Restore

Qasedak P1 restore is validation-first and command-based. Django Admin never applies a database restore inside a normal request.

Admin restore center:

```text
/admin/store/backups/
/admin/store/restore/<restore_id>/
```

Private upload root:

```text
QASEDAK_RESTORE_UPLOAD_ROOT=/opt/qasedak/backups/restore_uploads
```

Uploaded files must be Qasedak `.tar.gz` packages with `manifest.json`, `checksums.sha256`, and either `database/db.postgres.dump` or legacy `database/db.sqlite3`.

## Validation Flow

1. Upload the backup package in Admin.
2. Click Validate.
3. Qasedak extracts to a private temp directory.
4. Validation blocks absolute paths, `../`, empty path segments, symlinks, hardlinks, devices, FIFOs, oversized archives, and oversized extracted payloads.
5. Checksums are verified.
6. Manifest version and DB engine are checked.
7. PostgreSQL packages require `database/db.postgres.dump` and `pg_restore`.
8. SQLite packages require a valid `database/db.sqlite3` header and warn when the target is PostgreSQL.
9. A sanitized restore plan is stored on the restore job.

The plan shows source/target DB engine, backup type, app version, migration metadata, media/env flags, warnings, estimated steps, and the confirmation phrase. It does not show raw `.env`, tokens, passwords, config links, UUIDs, full phone/email/chat IDs, or card numbers.

## Generate Command

After validation, Admin can generate a command like:

```bash
sudo /opt/qasedak/scripts/restore.sh \
  --install-dir /opt/qasedak \
  --restore-job-id 12 \
  --backup-file /opt/qasedak/backups/restore_uploads/restore-...-qasedak-backup.tar.gz \
  --confirm RESTORE_QASEDAK_BACKUP_12
```

Optional flags:

```bash
--include-media
--restore-env
--verbose
```

Use `--restore-env` only when intentionally moving runtime configuration to a new server. Review `.env` before replacing a live file.

## Dry-Run

Use dry-run before every real restore attempt:

```bash
sudo /opt/qasedak/scripts/restore.sh \
  --install-dir /opt/qasedak \
  --backup-file /opt/qasedak/backups/restore_uploads/qasedak-backup-YYYYMMDD-HHMMSS.tar.gz \
  --dry-run \
  --verbose
```

Dry-run validates the package and prints the restore plan. It does not change files, databases, services, timers, media, or env.

## Apply Strategy

P1 intentionally refuses destructive apply after validation:

```text
P1 destructive restore apply is not enabled in this script.
```

This avoids a fake success path and prevents accidental production mutation. A future apply implementation must still:

- create a fresh pre-restore backup,
- stop Telegram polling, timers, web workers, and other writers,
- restore PostgreSQL with `pg_restore` into a controlled target,
- verify migrations/counts before switching,
- restore media only with `--include-media`,
- restore env only with `--restore-env`,
- run `manage.py check`, migrations, `collectstatic`, and `doctor.sh`,
- restart services only after verification.

## PostgreSQL Notes

Production uses PostgreSQL by default. Backups are custom-format dumps from `pg_dump -Fc`; restore validation requires `pg_restore`.

Before a controlled PostgreSQL restore:

1. Confirm the target `.env` has the intended `DATABASE_ENGINE=postgres`.
2. Confirm `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`, and `POSTGRES_SSLMODE`.
3. Take a fresh backup of the current target.
4. Stop writers.
5. Validate the uploaded package.
6. Restore only through the approved runbook, not by manually untarring over a running DB.

## Rollback Guidance

Keep the pre-restore backup until the restored system has passed:

```bash
/opt/qasedak/venv/bin/python /opt/qasedak/manage.py check
/opt/qasedak/venv/bin/python /opt/qasedak/manage.py migrate --plan
/opt/qasedak/scripts/doctor.sh --install-dir /opt/qasedak --no-fail
```

If validation fails, do not apply anything. If a future apply flow fails mid-run, keep services stopped until the operator chooses either the pre-restore backup or a known-good database target.

## Disaster Recovery Checklist

- Confirm the archive SHA256 with the value recorded in Admin.
- Validate in Admin or with `restore.sh --dry-run`.
- Check backup age, source app version, and migrations.
- Confirm target DB engine.
- Confirm whether media and env should be restored.
- Take a fresh pre-restore backup.
- Stop writers before any destructive step.
- Run checks before restarting services.
- Never paste backup files or `.env` contents into logs, tickets, or chat.
