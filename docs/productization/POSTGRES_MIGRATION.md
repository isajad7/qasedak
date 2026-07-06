# SQLite to PostgreSQL Migration Design

This document is the rehearsal plan for moving an existing Qasedak install from SQLite to PostgreSQL. Do not run a production switch from this phase.

## Why Migrate

PostgreSQL is better for production concurrency, backups, operational visibility, and future growth. SQLite remains supported for development, tests, small installs, and fallback.

## Prerequisites

- Code deployed with PostgreSQL-ready settings and dependencies.
- Current production still running on SQLite.
- Full backup from `/admin/store/backups/` or `scripts/backup.sh`.
- PostgreSQL packages installed: `postgresql`, `postgresql-client`, `libpq-dev`.
- `pg_dump`, `pg_restore`, and `psql` available.
- Database role/password generated at runtime and stored only in `.env` with mode `600`.

## Rehearsal First

1. Copy the SQLite database to a disposable host or disposable path.
2. Create a disposable PostgreSQL database and role.
3. Run migrations against the disposable PostgreSQL database.
4. Export data from the copied SQLite database.
5. Import into PostgreSQL.
6. Verify counts for critical models: `Customer`, `Order`, `VPNClient`, `Plan`, `Panel`, `Inbound`, payments, support, bot logs, and Revenue Engine logs.
7. Reset PostgreSQL sequences after import.
8. Run `manage.py check`, focused tests, and `doctor.sh --no-fail`.
9. Discard the rehearsal target unless all checks pass.

## PostgreSQL Test Gate

Before switching production `.env`, run the PostgreSQL gate against a disposable PostgreSQL test database or a PostgreSQL connection whose Django test database can be created and destroyed:

```bash
scripts/postgres_test_gate.sh --env-file /path/to/postgres.env --quick
scripts/postgres_test_gate.sh --env-file /path/to/postgres.env --full
```

The gate verifies Django is using PostgreSQL, runs `manage.py check`, runs the critical PostgreSQL locking tests first, and then runs the full `store.tests payments.tests` suite in `--full` mode. The script accepts `--database-url`, `--settings`, and `--keepdb`; it must not print the database password.

## PostgreSQL select_for_update compatibility

SQLite does not enforce row-level `SELECT ... FOR UPDATE`, so it did not catch querysets that lock rows while joining nullable relations. PostgreSQL rejects `FOR UPDATE` on the nullable side of an outer join. Qasedak uses base-row locking for those cases: either split the lock from later related-object loading, or call the small `select_for_update_self()` helper so PostgreSQL emits `FOR UPDATE OF` only for the base model table.

## Export and Import Strategy

Use Django-aware data movement where possible so model serialization, content types, and migrations stay consistent. Avoid raw production mutation during rehearsal. If a raw path is selected later, document every excluded table and sequence reset.

## Count Verification

Record before/after counts for every installed app model. Differences must be explained before production cutover. Media files are unaffected by the DB migration and should not be copied through the database process.

## Production Cutover Outline

1. Stop web, Telegram, timers, and any workers that write to the DB.
2. Take a fresh backup.
3. Validate the backup package in `/admin/store/backups/` or with `scripts/restore.sh --dry-run`.
4. Create PostgreSQL DB/user idempotently.
5. Run the rehearsed import.
6. Reset sequences.
7. Switch `.env` to `DATABASE_ENGINE=postgres`.
8. Run `manage.py check`, migrations, and doctor.
9. Start services only after verification.

## Rollback

If rehearsal fails, do not touch production. If production cutover fails, restore `.env` to SQLite, verify `SQLITE_DATABASE_PATH`, and start services against the original SQLite DB after checking the pre-cutover backup. Do not drop the PostgreSQL database until a postmortem is complete.

## Downtime Estimate

Downtime is the time to stop services, take a final backup, import data, verify counts, switch `.env`, and run checks. Measure it during rehearsal with a database copy similar in size to production.

## Risks

- Missed tables or content types during export/import.
- Sequence values not reset after import.
- Long downtime for large SQLite files.
- Production `.env` switched before verification.
- Telegram/X-UI side effects if workers are not stopped.
- Accidental destructive restore without a fresh backup.

## Helper Skeleton

Use `scripts/migrate_sqlite_to_postgres.sh --dry-run` or `--rehearsal` to print the planned inputs. `--apply` is reserved for the dedicated production migration phase.
