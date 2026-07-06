# Update

For the default install path:

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/update_from_github.sh | sudo bash
```

If you installed somewhere else:

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/update_from_github.sh | sudo env QASEDAK_INSTALL_DIR=/your/path bash
```

The update command:

- creates a backup first
- uses an engine-aware backup (`sqlite3 .backup` or PostgreSQL `pg_dump -Fc`)
- syncs the latest code
- installs dependencies
- installs Python 3.12/venv if the server Python is older
- runs Django checks and migrations
- collects static files
- restarts services when they exist
- runs doctor after update

## Dry Run

```bash
curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/update_from_github.sh | sudo bash -s -- --dry-run --no-restart
```

## Manual Backup

```bash
sudo /opt/qasedak/scripts/backup.sh --install-dir /opt/qasedak --output-dir /opt/qasedak/backups --yes
```

## Rollback Notes

Do not drop databases during a normal update or rollback. If an update fails, keep services stopped, inspect the pre-update backup path printed by `update.sh`, and follow `docs/BACKUP.md`.

SQLite rollback restores `database/db.sqlite3` to the configured `SQLITE_DATABASE_PATH`.

PostgreSQL rollback uses the custom-format `database/db.postgres.dump` with `pg_restore` after taking a fresh pre-restore backup. The target database must be empty unless a destructive clean restore is explicitly confirmed.
