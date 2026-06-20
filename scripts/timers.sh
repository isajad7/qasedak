#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

INSTALL_DIR="/opt/vpn-store"
ACTION=""
DRY_RUN=0
YES=0
TIMER_NAME=""
ALL_TIMERS=0
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
  scripts/timers.sh --install-dir DIR (--status|--enable|--disable) (--all|--timer NAME) [options]

Options:
  --install-dir DIR  Installed VPN Store directory.
  --enable           Render and enable selected systemd timers.
  --disable          Disable selected systemd timers.
  --status           Show selected timer states. Default when no action is provided.
  --dry-run          Print planned systemd actions only.
  --yes              Accept enable/disable confirmation prompts.
  --timer NAME       One timer key, for example renewal-reminders or revenue-scan-dry-run.
  --all              Select all known VPN Store timers.
  -h, --help         Show this help.

Known timers:
  renewal-reminders, panel-health, daily-admin-report,
  panel-usage-snapshots, panel-daily-usage, revenue-scan-dry-run
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

sed_escape() {
  printf '%s' "$1" | sed 's/[&|]/\\&/g'
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
      --enable)
        ACTION="enable"
        ;;
      --disable)
        ACTION="disable"
        ;;
      --status)
        ACTION="status"
        ;;
      --dry-run)
        DRY_RUN=1
        ;;
      --yes)
        YES=1
        ;;
      --timer)
        shift
        [[ $# -gt 0 ]] || die "--timer requires a value."
        TIMER_NAME="$1"
        ;;
      --all)
        ALL_TIMERS=1
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

normalize_timer() {
  local value="$1"
  value="${value%.timer}"
  value="${value%.service}"
  value="${value#vpn-store-}"
  case "$value" in
    renewal|renewals|renewal-reminder|renewal-reminders)
      printf 'renewal-reminders'
      ;;
    panel-health|health)
      printf 'panel-health'
      ;;
    daily-admin-report|admin-report)
      printf 'daily-admin-report'
      ;;
    panel-usage-snapshots|usage-snapshots|snapshots)
      printf 'panel-usage-snapshots'
      ;;
    panel-daily-usage|daily-usage)
      printf 'panel-daily-usage'
      ;;
    revenue|revenue-scan|revenue-scan-dry-run)
      printf 'revenue-scan-dry-run'
      ;;
    *)
      return 1
      ;;
  esac
}

unit_base() {
  printf 'vpn-store-%s' "$1"
}

template_path() {
  local key="$1"
  local suffix="$2"
  local install_template="$INSTALL_DIR/scripts/templates/systemd/timers/vpn-store-$key.$suffix.template"
  local repo_template="$REPO_DIR/scripts/templates/systemd/timers/vpn-store-$key.$suffix.template"
  if [[ -f "$install_template" ]]; then
    printf '%s' "$install_template"
  else
    printf '%s' "$repo_template"
  fi
}

selected_timers() {
  if (( ALL_TIMERS )); then
    printf '%s\n' "${KNOWN_TIMERS[@]}"
    return 0
  fi
  if [[ -n "$TIMER_NAME" ]]; then
    normalize_timer "$TIMER_NAME"
    return 0
  fi
  if [[ "${ACTION:-status}" == "status" ]]; then
    printf '%s\n' "${KNOWN_TIMERS[@]}"
    return 0
  fi
  die "Select --all or --timer NAME."
}

validate_preflight() {
  ACTION="${ACTION:-status}"
  if (( DRY_RUN )); then
    [[ -d "$INSTALL_DIR" ]] || warn "Install directory does not exist yet: $INSTALL_DIR"
    return 0
  fi
  [[ -d "$INSTALL_DIR" ]] || die "Install directory not found: $INSTALL_DIR"
  [[ -f "$INSTALL_DIR/.env" ]] || die ".env not found at $INSTALL_DIR/.env"
  command -v systemctl >/dev/null 2>&1 || die "systemctl is required for timer management."
}

validate_revenue_timer_safe() {
  local service_template="$1"
  if (( DRY_RUN )); then
    return 0
  fi
  [[ -f "$service_template" ]] || die "Revenue timer service template missing: $service_template"
  grep -q -- 'run_revenue_scan --dry-run' "$service_template" || die "Revenue timer service must run run_revenue_scan --dry-run."
}

render_template() {
  local template="$1"
  local target="$2"
  if (( DRY_RUN )); then
    log "DRY-RUN: would render $template to $target."
    return 0
  fi
  [[ -f "$template" ]] || die "Template not found: $template"
  local tmp
  tmp="$(mktemp)"
  sed "s|{{INSTALL_DIR}}|$(sed_escape "$INSTALL_DIR")|g" "$template" > "$tmp"
  run_cmd "${SUDO_CMD[@]}" install -m 0644 "$tmp" "$target"
  rm -f "$tmp"
}

enable_timer() {
  local key="$1"
  local base
  local service_template
  local timer_template
  base="$(unit_base "$key")"
  service_template="$(template_path "$key" service)"
  timer_template="$(template_path "$key" timer)"
  if [[ "$key" == "revenue-scan-dry-run" ]]; then
    validate_revenue_timer_safe "$service_template"
  fi
  render_template "$service_template" "/etc/systemd/system/$base.service"
  render_template "$timer_template" "/etc/systemd/system/$base.timer"
  run_cmd "${SUDO_CMD[@]}" systemctl daemon-reload
  run_cmd "${SUDO_CMD[@]}" systemctl enable --now "$base.timer"
  if (( DRY_RUN )); then
    log "DRY-RUN: timer enable planned: $base.timer"
  else
    log "Timer enabled: $base.timer"
  fi
}

disable_timer() {
  local key="$1"
  local base
  base="$(unit_base "$key")"
  run_cmd "${SUDO_CMD[@]}" systemctl disable --now "$base.timer"
  run_cmd "${SUDO_CMD[@]}" systemctl daemon-reload
  if (( DRY_RUN )); then
    log "DRY-RUN: timer disable planned: $base.timer"
  else
    log "Timer disabled: $base.timer"
  fi
}

status_timer() {
  local key="$1"
  local base
  local enabled="unknown"
  local active="unknown"
  base="$(unit_base "$key")"
  if (( DRY_RUN )); then
    log "DRY-RUN: would check $base.timer enabled/active state."
    return 0
  fi
  enabled="$(systemctl is-enabled "$base.timer" 2>/dev/null || true)"
  active="$(systemctl is-active "$base.timer" 2>/dev/null || true)"
  printf '%-42s enabled=%s active=%s\n' "$base.timer" "$enabled" "$active"
}

main() {
  parse_args "$@"
  preflight_sudo
  validate_preflight

  mapfile -t timers < <(selected_timers)
  [[ "${#timers[@]}" -gt 0 ]] || die "No timers selected."

  case "$ACTION" in
    enable)
      confirm "Enable selected VPN Store timers?" || die "Timer enable cancelled."
      for timer in "${timers[@]}"; do
        enable_timer "$timer"
      done
      ;;
    disable)
      confirm "Disable selected VPN Store timers?" || die "Timer disable cancelled."
      for timer in "${timers[@]}"; do
        disable_timer "$timer"
      done
      ;;
    status)
      for timer in "${timers[@]}"; do
        status_timer "$timer"
      done
      ;;
    *)
      die "Invalid action: $ACTION"
      ;;
  esac
}

main "$@"
