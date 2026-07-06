#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_NAME="$(basename "$0")"
INSTALL_DIR="/opt/qasedak"
SQLITE_PATH=""
POSTGRES_DB="qasedak"
POSTGRES_USER="qasedak"
POSTGRES_HOST="127.0.0.1"
POSTGRES_PORT="5432"
DRY_RUN=0
REHEARSAL=0
APPLY=0
YES=0

on_error() {
  local line="$1"
  local command="$2"
  printf 'ERROR: %s failed at line %s while running: %s\n' "$SCRIPT_NAME" "$line" "$command" >&2
}
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR

usage() {
  cat <<'EOF'
Usage:
  scripts/migrate_sqlite_to_postgres.sh --dry-run [options]
  scripts/migrate_sqlite_to_postgres.sh --rehearsal [options]

Options:
  --install-dir DIR    Installed VPN Store directory. Default: /opt/qasedak
  --sqlite-path FILE   Source SQLite database. Default: <install-dir>/data/db.sqlite3
  --postgres-db NAME   Target PostgreSQL database. Default: qasedak
  --postgres-user NAME Target PostgreSQL user. Default: qasedak
  --postgres-host HOST Target PostgreSQL host. Default: 127.0.0.1
  --postgres-port PORT Target PostgreSQL port. Default: 5432
  --dry-run            Print the migration plan only.
  --rehearsal          Print rehearsal steps against a copied SQLite DB.
  --apply              Reserved for the next production migration phase.
  --yes                Reserved for future confirmations.
  -h, --help           Show this help.

This phase does not move production data. Use docs/productization/POSTGRES_MIGRATION.md
for the rehearsal plan and run the real migration only in the dedicated phase.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

validate_pg_identifier() {
  local label="$1"
  local value="$2"
  [[ "$value" =~ ^[a-zA-Z0-9_]+$ ]] || die "$label may contain only letters, numbers, and underscores."
}

parse_args() {
  while (($#)); do
    case "$1" in
      --install-dir)
        shift
        [[ $# -gt 0 ]] || die "--install-dir requires a value."
        INSTALL_DIR="$1"
        ;;
      --sqlite-path)
        shift
        [[ $# -gt 0 ]] || die "--sqlite-path requires a value."
        SQLITE_PATH="$1"
        ;;
      --postgres-db)
        shift
        [[ $# -gt 0 ]] || die "--postgres-db requires a value."
        POSTGRES_DB="$1"
        ;;
      --postgres-user)
        shift
        [[ $# -gt 0 ]] || die "--postgres-user requires a value."
        POSTGRES_USER="$1"
        ;;
      --postgres-host)
        shift
        [[ $# -gt 0 ]] || die "--postgres-host requires a value."
        POSTGRES_HOST="$1"
        ;;
      --postgres-port)
        shift
        [[ $# -gt 0 ]] || die "--postgres-port requires a value."
        POSTGRES_PORT="$1"
        ;;
      --dry-run)
        DRY_RUN=1
        ;;
      --rehearsal)
        REHEARSAL=1
        ;;
      --apply)
        APPLY=1
        ;;
      --yes)
        YES=1
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "Unknown argument: $1"
        ;;
    esac
    shift
  done
}

main() {
  parse_args "$@"
  SQLITE_PATH="${SQLITE_PATH:-$INSTALL_DIR/data/db.sqlite3}"
  validate_pg_identifier "PostgreSQL database name" "$POSTGRES_DB"
  validate_pg_identifier "PostgreSQL user name" "$POSTGRES_USER"
  [[ "$POSTGRES_PORT" =~ ^[0-9]+$ ]] || die "PostgreSQL port must be numeric."

  printf 'SQLite to PostgreSQL migration plan:\n'
  printf '  install dir: %s\n' "$INSTALL_DIR"
  printf '  sqlite source: %s\n' "$SQLITE_PATH"
  printf '  postgres target: %s@%s:%s/%s\n' "$POSTGRES_USER" "$POSTGRES_HOST" "$POSTGRES_PORT" "$POSTGRES_DB"
  printf '  source data mutation: no\n'
  printf '  required rehearsal: yes\n'
  printf '  pre-migration backup: required\n'
  printf '  production apply in this phase: disabled\n'

  if (( APPLY )); then
    die "--apply is reserved for the next production migration phase."
  fi
  if (( REHEARSAL )); then
    printf 'REHEARSAL: copy SQLite DB, create disposable Postgres DB, import, verify counts, reset sequences, then discard rehearsal target.\n'
    return 0
  fi
  if (( DRY_RUN )); then
    printf 'DRY-RUN: no files or databases were changed.\n'
    return 0
  fi
  die "Choose --dry-run or --rehearsal. Production apply is disabled."
}

main "$@"
