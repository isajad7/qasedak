#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_SOURCE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

INSTALL_DIR="/opt/qasedak"
SOURCE_DIR="$DEFAULT_SOURCE_DIR"
DRY_RUN=0
YES=0
SKIP_BACKUP=0
BACKUP_DIR=""
RESTART=0
RUN_TESTS=0
VERBOSE=0
CURRENT_BACKUP=""
SUDO_CMD=()
WEB_SERVICE_NAME="vpn-store-web"
TELEGRAM_SERVICE_NAME="vpn-store-telegram"

on_error() {
  local line="$1"
  local command="$2"
  printf 'ERROR: %s failed at line %s while running: %s\n' "$SCRIPT_NAME" "$line" "$command" >&2
  if [[ -n "$CURRENT_BACKUP" ]]; then
    printf 'A pre-update backup is available at: %s\n' "$CURRENT_BACKUP" >&2
    printf 'Manual rollback guidance: docs/UPGRADE.md and docs/BACKUP.md\n' >&2
  fi
}
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR

usage() {
  cat <<'EOF'
Usage:
  scripts/update.sh --install-dir DIR --source-dir DIR [options]

Options:
  --install-dir DIR   Installed VPN Store directory.
  --source-dir DIR    Source repo/directory to sync from. Default: current repo.
  --dry-run           Print the update plan only. No backup, rsync, migrate, or restart.
  --yes               Accept ordinary confirmation prompts.
  --skip-backup       Dangerous: skip the required pre-update backup after explicit confirmation.
  --backup-dir DIR    Backup output directory. Default: <install-dir>/backups
  --restart           Restart existing systemd services after update.
  --no-restart        Do not restart services. Default.
  --run-tests         Run focused Django tests before migrations.
  --verbose           Print extra non-secret diagnostic detail.
  --web-service-name NAME
  --telegram-service-name NAME
  -h, --help          Show this help.
EOF
}

log() {
  printf '%s\n' "$*"
}

warn() {
  printf 'WARNING: %s\n' "$*" >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  if [[ -n "$CURRENT_BACKUP" ]]; then
    printf 'A pre-update backup is available at: %s\n' "$CURRENT_BACKUP" >&2
    printf 'Manual rollback guidance: docs/UPGRADE.md and docs/BACKUP.md\n' >&2
  fi
  exit 1
}

verbose_log() {
  if (( VERBOSE )); then
    log "$*"
  fi
}

run_cmd() {
  if (( DRY_RUN )); then
    printf 'DRY-RUN:'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

confirm() {
  local question="$1"
  local answer=""
  if (( YES )); then
    return 0
  fi
  [[ -t 0 ]] || die "$question requires an interactive terminal or --yes."
  read -r -p "$question [y/N]: " answer
  [[ "$answer" =~ ^[Yy]$ || "$answer" =~ ^[Yy][Ee][Ss]$ ]]
}

confirm_skip_backup() {
  local answer=""
  if (( DRY_RUN )); then
    warn "DRY-RUN: --skip-backup selected; a real update would require explicit confirmation."
    return 0
  fi
  [[ -t 0 ]] || die "--skip-backup requires an interactive terminal and cannot be accepted by --yes."
  warn "Skipping the pre-update backup is dangerous and not recommended."
  read -r -p 'Type SKIP BACKUP to continue: ' answer
  [[ "$answer" == "SKIP BACKUP" ]] || die "Backup skip confirmation did not match."
}

parse_args() {
  while (($#)); do
    case "$1" in
      --install-dir)
        shift
        [[ $# -gt 0 ]] || die "--install-dir requires a value."
        INSTALL_DIR="$1"
        ;;
      --source-dir)
        shift
        [[ $# -gt 0 ]] || die "--source-dir requires a value."
        SOURCE_DIR="$1"
        ;;
      --dry-run)
        DRY_RUN=1
        ;;
      --yes)
        YES=1
        ;;
      --skip-backup)
        SKIP_BACKUP=1
        ;;
      --backup-dir)
        shift
        [[ $# -gt 0 ]] || die "--backup-dir requires a value."
        BACKUP_DIR="$1"
        ;;
      --restart)
        RESTART=1
        ;;
      --no-restart)
        RESTART=0
        ;;
      --run-tests)
        RUN_TESTS=1
        ;;
      --verbose)
        VERBOSE=1
        ;;
      --web-service-name)
        shift
        [[ $# -gt 0 ]] || die "--web-service-name requires a value."
        WEB_SERVICE_NAME="$1"
        ;;
      --telegram-service-name)
        shift
        [[ $# -gt 0 ]] || die "--telegram-service-name requires a value."
        TELEGRAM_SERVICE_NAME="$1"
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

preflight_sudo() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    SUDO_CMD=()
  elif command -v sudo >/dev/null 2>&1; then
    SUDO_CMD=(sudo)
  else
    SUDO_CMD=()
  fi
}

preflight_paths() {
  BACKUP_DIR="${BACKUP_DIR:-$INSTALL_DIR/backups}"
  [[ -d "$SOURCE_DIR" ]] || die "Source directory not found: $SOURCE_DIR"
  [[ -f "$SOURCE_DIR/manage.py" ]] || die "Source directory does not look like the VPN Store repo: $SOURCE_DIR"
  if (( DRY_RUN )); then
    [[ -d "$INSTALL_DIR" ]] || warn "Install directory does not exist yet: $INSTALL_DIR"
    [[ -f "$INSTALL_DIR/.env" ]] || warn ".env not found in install dir; real update would stop."
    return 0
  fi
  [[ -d "$INSTALL_DIR" ]] || die "Install directory not found: $INSTALL_DIR"
  [[ -f "$INSTALL_DIR/.env" ]] || die ".env not found at $INSTALL_DIR/.env"
  [[ -f "$INSTALL_DIR/manage.py" ]] || die "manage.py not found at $INSTALL_DIR/manage.py"
}

source_env() {
  if (( DRY_RUN )); then
    log "DRY-RUN: would source $INSTALL_DIR/.env with secrets redacted."
    return 0
  fi
  set -a
  # shellcheck source=/dev/null
  . "$INSTALL_DIR/.env"
  set +a
}

print_plan() {
  log "Update plan:"
  log "  install dir: $INSTALL_DIR"
  log "  source dir: $SOURCE_DIR"
  log "  backup dir: $BACKUP_DIR"
  log "  backup required: $([[ "$SKIP_BACKUP" == "1" ]] && printf 'skipped only after dangerous confirmation' || printf yes)"
  log "  rsync excludes runtime, secrets, venvs, caches, backups, logs, and node_modules"
  log "  restart services: $([[ "$RESTART" == "1" ]] && printf yes || printf no)"
  log "  run tests: $([[ "$RUN_TESTS" == "1" ]] && printf yes || printf no)"
  if (( DRY_RUN )); then
    log "DRY-RUN: no backup, rsync, pip install, tests, migrations, collectstatic, doctor, or restart will run."
  fi
}

run_backup() {
  if (( SKIP_BACKUP )); then
    confirm_skip_backup
    return 0
  fi
  if (( DRY_RUN )); then
    log "DRY-RUN: would run backup.sh before syncing source."
    return 0
  fi

  local backup_script="$SOURCE_DIR/scripts/backup.sh"
  if [[ ! -f "$backup_script" ]]; then
    backup_script="$SCRIPT_DIR/backup.sh"
  fi
  [[ -f "$backup_script" ]] || die "backup.sh not found in source or current script directory."

  local output=""
  if ! output="$(bash "$backup_script" --install-dir "$INSTALL_DIR" --output-dir "$BACKUP_DIR" --yes --compress 2>&1)"; then
    printf '%s\n' "$output" >&2
    die "Pre-update backup failed; update aborted."
  fi
  printf '%s\n' "$output"
  CURRENT_BACKUP="$(printf '%s\n' "$output" | awk -F': ' '/^Backup created:/ {print $2; exit}')"
  [[ -n "$CURRENT_BACKUP" ]] || warn "Backup completed, but backup path could not be parsed from output."
}

sync_source() {
  confirm "Sync $SOURCE_DIR to $INSTALL_DIR now?" || die "Update cancelled."
  run_cmd rsync -a --delete \
    --exclude '.git' \
    --exclude '.env' \
    --exclude 'install.config.json' \
    --exclude 'data/' \
    --exclude 'db.sqlite3' \
    --exclude 'db.sqlite3.backup' \
    --exclude 'media/' \
    --exclude 'venv/' \
    --exclude '.venv/' \
    --exclude 'backups/' \
    --exclude 'logs/' \
    --exclude 'node_modules/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    "$SOURCE_DIR/" "$INSTALL_DIR/"
}

setup_venv_and_deps() {
  local python="$INSTALL_DIR/venv/bin/python"
  if (( DRY_RUN )); then
    run_cmd python3 -m venv "$INSTALL_DIR/venv"
  elif [[ ! -x "$python" ]]; then
    run_cmd python3 -m venv "$INSTALL_DIR/venv"
  else
    verbose_log "Reusing existing virtualenv: $INSTALL_DIR/venv"
  fi
  run_cmd "$python" -m pip install -r "$INSTALL_DIR/requirements.txt"
}

run_django_steps() {
  local python="$INSTALL_DIR/venv/bin/python"
  source_env
  run_cmd "$python" "$INSTALL_DIR/manage.py" check
  if (( RUN_TESTS )); then
    run_cmd "$python" "$INSTALL_DIR/manage.py" test store.tests payments.tests
  fi
  run_cmd "$python" "$INSTALL_DIR/manage.py" migrate --plan
  run_cmd "$python" "$INSTALL_DIR/manage.py" migrate
  run_cmd "$python" "$INSTALL_DIR/manage.py" collectstatic --noinput
}

service_exists() {
  local unit="$1"
  [[ -e "/etc/systemd/system/$unit" || -L "/etc/systemd/system/$unit" ]] && return 0
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl list-unit-files "$unit" --no-legend 2>/dev/null | grep -q .
}

restart_services() {
  if (( ! RESTART )); then
    log "Service restart skipped."
    return 0
  fi
  local unit=""
  for unit in "$WEB_SERVICE_NAME.service" "$TELEGRAM_SERVICE_NAME.service"; do
    if (( DRY_RUN )); then
      log "DRY-RUN: would restart $unit only if it exists."
    elif service_exists "$unit"; then
      run_cmd "${SUDO_CMD[@]}" systemctl restart "$unit"
      run_cmd "${SUDO_CMD[@]}" systemctl status --no-pager "$unit"
    else
      warn "Skipping restart; service does not exist: $unit"
    fi
  done
}

run_post_update_doctor() {
  local doctor="$INSTALL_DIR/scripts/doctor.sh"
  if (( DRY_RUN )); then
    log "DRY-RUN: would run post-update doctor."
    if [[ -f "$SOURCE_DIR/scripts/doctor.sh" ]]; then
      run_cmd bash "$SOURCE_DIR/scripts/doctor.sh" --dry-run --install-dir "$INSTALL_DIR" --no-fail
    fi
    return 0
  fi
  [[ -f "$doctor" ]] || die "doctor.sh not found after update: $doctor"
  run_cmd bash "$doctor" --install-dir "$INSTALL_DIR"
}

main() {
  parse_args "$@"
  preflight_sudo
  preflight_paths
  print_plan
  run_backup
  sync_source
  setup_venv_and_deps
  run_django_steps
  restart_services
  run_post_update_doctor
  log "Update completed."
  if [[ -n "$CURRENT_BACKUP" ]]; then
    log "Pre-update backup: $CURRENT_BACKUP"
  fi
}

main "$@"
