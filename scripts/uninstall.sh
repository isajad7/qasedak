#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_NAME="$(basename "$0")"

INSTALL_DIR="/opt/qasedak"
DRY_RUN=0
YES=0
SERVICE_PREFIX="vpn-store"
WEB_SERVICE_NAME="vpn-store-web"
TELEGRAM_SERVICE_NAME="vpn-store-telegram"
NGINX_SITE_NAME="vpn-store"
SUDO_CMD=()

KNOWN_TIMERS=(
  renewal-reminders
  panel-health
  daily-admin-report
  panel-usage-snapshots
  panel-daily-usage
  revenue-scan-dry-run
)

on_error() {
  local line="$1"
  local command="$2"
  printf 'ERROR: %s failed at line %s while running: %s\n' "$SCRIPT_NAME" "$line" "$command" >&2
}
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR

usage() {
  cat <<'EOF'
Usage:
  scripts/uninstall.sh [--install-dir DIR] [options]

Options:
  --install-dir DIR  Installed Qasedak directory. Default: /opt/qasedak
  --dry-run          Print the cleanup plan only.
  --yes              Accept the destructive cleanup confirmation.
  --service-prefix NAME
  --web-service-name NAME
  --telegram-service-name NAME
  --nginx-site-name NAME
  -h, --help         Show this help.

This removes Qasedak systemd units, nginx site files, and the install directory.
It does not remove shared apt packages, Python, nginx, certbot, or Let's Encrypt certs.
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
  exit 1
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

parse_args() {
  while (($#)); do
    case "$1" in
      --install-dir)
        shift
        [[ $# -gt 0 ]] || die "--install-dir requires a value."
        INSTALL_DIR="$1"
        ;;
      --dry-run)
        DRY_RUN=1
        ;;
      --yes)
        YES=1
        ;;
      --service-prefix)
        shift
        [[ $# -gt 0 ]] || die "--service-prefix requires a value."
        SERVICE_PREFIX="$1"
        WEB_SERVICE_NAME="$SERVICE_PREFIX-web"
        TELEGRAM_SERVICE_NAME="$SERVICE_PREFIX-telegram"
        NGINX_SITE_NAME="$SERVICE_PREFIX"
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
      --nginx-site-name)
        shift
        [[ $# -gt 0 ]] || die "--nginx-site-name requires a value."
        NGINX_SITE_NAME="$1"
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
  elif (( ! DRY_RUN )); then
    die "Run as root or install sudo."
  else
    SUDO_CMD=()
  fi
}

validate_install_dir_safe() {
  [[ -n "$INSTALL_DIR" ]] || die "Install directory is empty."
  case "$INSTALL_DIR" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/media|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
      die "Refusing to remove broad system directory: $INSTALL_DIR"
      ;;
  esac
}

unit_exists() {
  local unit="$1"
  [[ -e "/etc/systemd/system/$unit" || -L "/etc/systemd/system/$unit" ]] && return 0
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl list-unit-files "$unit" --no-legend 2>/dev/null | grep -q .
}

nginx_file_paths() {
  printf '%s\n' \
    "/etc/nginx/sites-enabled/$NGINX_SITE_NAME.conf" \
    "/etc/nginx/sites-available/$NGINX_SITE_NAME.conf"
}

systemd_units() {
  local timer=""
  printf '%s\n' "$WEB_SERVICE_NAME.service" "$TELEGRAM_SERVICE_NAME.service"
  for timer in "${KNOWN_TIMERS[@]}"; do
    printf 'vpn-store-%s.timer\n' "$timer"
    printf 'vpn-store-%s.service\n' "$timer"
  done
}

detect_markers() {
  local path=""
  local unit=""
  local paths=()
  local units=()
  if [[ -e "$INSTALL_DIR" || -L "$INSTALL_DIR" ]]; then
    printf '%s\n' "$INSTALL_DIR"
  fi
  mapfile -t paths < <(nginx_file_paths)
  for path in "${paths[@]}"; do
    [[ -e "$path" || -L "$path" ]] && printf '%s\n' "$path"
  done
  mapfile -t units < <(systemd_units)
  for unit in "${units[@]}"; do
    unit_exists "$unit" && printf '/etc/systemd/system/%s\n' "$unit"
  done
  return 0
}

print_plan() {
  log "Qasedak uninstall plan:"
  log "  install dir: $INSTALL_DIR"
  log "  web service: $WEB_SERVICE_NAME.service"
  log "  telegram service: $TELEGRAM_SERVICE_NAME.service"
  log "  nginx site: $NGINX_SITE_NAME.conf"
  log "  timers: vpn-store-*.timer known timers"
  log "  apt packages/certs: kept"
}

remove_systemd_units() {
  local unit=""
  local units=()
  local removed=0
  command -v systemctl >/dev/null 2>&1 || {
    warn "systemctl not found; removing unit files only."
  }
  mapfile -t units < <(systemd_units)
  for unit in "${units[@]}"; do
    if unit_exists "$unit"; then
      if command -v systemctl >/dev/null 2>&1; then
        run_cmd "${SUDO_CMD[@]}" systemctl disable --now "$unit" || true
      fi
      run_cmd "${SUDO_CMD[@]}" rm -f "/etc/systemd/system/$unit"
      removed=1
    fi
  done
  if (( removed )) && command -v systemctl >/dev/null 2>&1; then
    run_cmd "${SUDO_CMD[@]}" systemctl daemon-reload
  fi
}

remove_nginx_site() {
  local path=""
  local paths=()
  local removed=0
  mapfile -t paths < <(nginx_file_paths)
  for path in "${paths[@]}"; do
    if [[ -e "$path" || -L "$path" ]]; then
      run_cmd "${SUDO_CMD[@]}" rm -f "$path"
      removed=1
    fi
  done
  if (( removed )) && command -v nginx >/dev/null 2>&1; then
    run_cmd "${SUDO_CMD[@]}" nginx -t
    if command -v systemctl >/dev/null 2>&1; then
      run_cmd "${SUDO_CMD[@]}" systemctl reload nginx || true
    fi
  fi
}

remove_install_dir() {
  if [[ -e "$INSTALL_DIR" || -L "$INSTALL_DIR" ]]; then
    run_cmd "${SUDO_CMD[@]}" rm -rf --one-file-system "$INSTALL_DIR"
  fi
}

main() {
  parse_args "$@"
  preflight_sudo
  validate_install_dir_safe
  print_plan

  mapfile -t markers < <(detect_markers)
  if [[ "${#markers[@]}" -eq 0 ]]; then
    log "No Qasedak install traces found."
    return 0
  fi

  warn "This will remove Qasedak files. Database, media, backups, and logs inside $INSTALL_DIR will be deleted."
  printf 'Detected traces:\n'
  printf '  %s\n' "${markers[@]}"
  if (( DRY_RUN )); then
    log "DRY-RUN: cleanup not executed."
    return 0
  fi

  confirm "Delete these Qasedak files now?" || die "Uninstall cancelled."
  remove_systemd_units
  remove_nginx_site
  remove_install_dir
  log "Qasedak uninstall completed."
}

main "$@"
