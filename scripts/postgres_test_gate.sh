#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-}"
SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-core.settings.production}"
DATABASE_URL_VALUE=""
ENV_FILE=""
KEEPDB=0
QUICK=0
FULL=1

usage() {
  cat <<'EOF'
Usage:
  scripts/postgres_test_gate.sh [options]

Options:
  --database-url URL  Parse a postgres:// URL into POSTGRES_* environment variables.
  --env-file PATH     Source environment variables before running checks.
  --settings MODULE   Django settings module. Defaults to core.settings.production.
  --keepdb            Reuse the Django test database.
  --quick             Run only the critical PostgreSQL compatibility subset.
  --full              Run the critical subset, then full store/payments tests. Default.
  -h, --help          Show this help.

The script never prints POSTGRES_PASSWORD.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

python_bin() {
  if [[ -n "$PYTHON_BIN" ]]; then
    printf '%s' "$PYTHON_BIN"
  elif [[ -x "$REPO_DIR/venv/bin/python" ]]; then
    printf '%s' "$REPO_DIR/venv/bin/python"
  elif [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
    printf '%s' "$REPO_DIR/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  else
    die "python3 not found"
  fi
}

parse_args() {
  while (($#)); do
    case "$1" in
      --database-url)
        shift
        [[ $# -gt 0 ]] || die "--database-url requires a value."
        DATABASE_URL_VALUE="$1"
        ;;
      --env-file)
        shift
        [[ $# -gt 0 ]] || die "--env-file requires a value."
        ENV_FILE="$1"
        ;;
      --settings)
        shift
        [[ $# -gt 0 ]] || die "--settings requires a value."
        SETTINGS_MODULE="$1"
        ;;
      --keepdb)
        KEEPDB=1
        ;;
      --quick)
        QUICK=1
        FULL=0
        ;;
      --full)
        QUICK=0
        FULL=1
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

load_env_file() {
  [[ -z "$ENV_FILE" ]] && return 0
  [[ -f "$ENV_FILE" ]] || die "env file not found: $ENV_FILE"
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
}

load_database_url() {
  [[ -z "$DATABASE_URL_VALUE" ]] && return 0
  local parsed_env
  parsed_env="$("$PY" - "$DATABASE_URL_VALUE" <<'PY'
import shlex
import sys
from urllib.parse import unquote, urlparse

url = urlparse(sys.argv[1])
if url.scheme not in {"postgres", "postgresql"}:
    raise SystemExit("database URL must use postgres:// or postgresql://")

values = {
    "DATABASE_ENGINE": "postgres",
    "POSTGRES_DB": unquote((url.path or "").lstrip("/")),
    "POSTGRES_USER": unquote(url.username or ""),
    "POSTGRES_PASSWORD": unquote(url.password or ""),
    "POSTGRES_HOST": url.hostname or "127.0.0.1",
    "POSTGRES_PORT": str(url.port or 5432),
}
query = dict(part.split("=", 1) for part in url.query.split("&") if "=" in part)
if "sslmode" in query:
    values["POSTGRES_SSLMODE"] = unquote(query["sslmode"])

for key, value in values.items():
    if value:
        print(f"export {key}={shlex.quote(value)}")
PY
)"
  eval "$parsed_env"
}

verify_postgres_vendor() {
  "$PY" - <<'PY'
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", os.environ.get("DJANGO_SETTINGS_MODULE", "core.settings.production"))

import django
django.setup()

from django.db import connection

print(f"Django database vendor: {connection.vendor}")
if connection.vendor != "postgresql":
    print("postgres_test_gate requires an active PostgreSQL Django connection.", file=sys.stderr)
    raise SystemExit(1)
PY
}

run_manage() {
  "$PY" "$REPO_DIR/manage.py" "$@"
}

main() {
  parse_args "$@"
  PY="$(python_bin)"
  export PYTHON_BIN="$PY"
  export DJANGO_SETTINGS_MODULE="$SETTINGS_MODULE"

  cd "$REPO_DIR"
  load_env_file
  load_database_url
  export DATABASE_ENGINE="${DATABASE_ENGINE:-postgres}"

  verify_postgres_vendor
  run_manage check

  test_args=()
  if (( KEEPDB )); then
    test_args+=(--keepdb)
  fi

  critical_tests=(
    store.tests.ModernPaidProvisioningTests.test_nullable_order_relation_lock_query_is_postgres_compatible
    store.tests.ModernPaidProvisioningTests.test_nullable_vpn_client_relation_lock_query_is_postgres_compatible
    store.tests.ModernPaidProvisioningTests.test_approval_lock_path_handles_nullable_relations_before_remote_lookup
    store.tests.ModernPaidProvisioningTests.test_approval_lock_path_handles_existing_related_rows
    store.tests.ModernPaidProvisioningTests.test_central_approval_refuses_terminal_or_rejected_payment_states
    store.tests.ModernPaidProvisioningTests.test_modern_approval_refuses_changed_frozen_node_scope_before_remote_call
    store.tests.AdminNotificationTests.test_new_order_notification_is_idempotent
    payments.tests.PaymentMatchingTests.test_process_sms_schedules_actionable_order_review_after_commit
    payments.tests.PaymentMatchingTests.test_process_sms_notification_is_idempotent_for_same_matches
  )

  run_manage test "${critical_tests[@]}" "${test_args[@]}"

  if (( FULL )); then
    run_manage test store.tests payments.tests "${test_args[@]}"
  elif (( QUICK )); then
    printf 'Quick PostgreSQL gate completed; full store/payments tests were skipped.\n'
  fi
}

main "$@"
