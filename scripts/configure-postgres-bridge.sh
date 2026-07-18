#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APPLY_ARGS=()

usage() {
  cat <<'EOF'
Usage:
  scripts/configure-postgres-bridge.sh [--install-dir DIR] [--apply] [--yes] [--no-fail]

Safely repairs host PostgreSQL Docker bridge access for existing Qasedak installs.
It backs up postgresql.conf and pg_hba.conf, sets listen_addresses to only
127.0.0.1 and 172.17.0.1, appends the Docker bridge pg_hba rule when missing,
then restarts PostgreSQL only after confirmation or --yes.

Options:
  --install-dir DIR  Installed Qasedak directory. Default: parent of this script.
  --apply            Write changes, restart PostgreSQL, and run check_postgres_bridge.
  --yes              Confirm the PostgreSQL restart without prompting.
  --no-fail          Exit 0 even when post-restart bridge checks fail.
  -h, --help         Show this help.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while (($#)); do
  case "$1" in
    --install-dir)
      shift
      [[ $# -gt 0 ]] || die "--install-dir requires a value."
      INSTALL_DIR="$1"
      ;;
    --apply|--yes|--no-fail)
      APPLY_ARGS+=("$1")
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown option: $1"
      ;;
  esac
  shift
done

PYTHON_BIN="$INSTALL_DIR/venv/bin/python"
MANAGE_PY="$INSTALL_DIR/manage.py"
[[ -x "$PYTHON_BIN" ]] || die "Python virtualenv not found or not executable: $PYTHON_BIN"
[[ -f "$MANAGE_PY" ]] || die "manage.py not found: $MANAGE_PY"

if [[ -f "$INSTALL_DIR/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  . "$INSTALL_DIR/.env"
  set +a
fi

cd "$INSTALL_DIR"
exec "$PYTHON_BIN" "$MANAGE_PY" configure_postgres_bridge "${APPLY_ARGS[@]}"
