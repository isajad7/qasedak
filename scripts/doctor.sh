#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DRY_RUN=0
LIVE_BOT=0
LIVE_XUI=0
CHECK_SYSTEMD=0
CHECK_NGINX=0
CHECK_TIMERS=0
CHECK_DISK=1
CHECK_PERMISSIONS=1
JSON_OUTPUT=0
NO_FAIL=0
VERBOSE=0
WEB_SERVICE_NAME="vpn-store-web"
TELEGRAM_SERVICE_NAME="vpn-store-telegram"
NGINX_SITE_NAME="vpn-store"
RESULTS_FILE=""

KNOWN_TIMERS=(
  vpn-store-renewal-reminders.timer
  vpn-store-panel-health.timer
  vpn-store-daily-admin-report.timer
  vpn-store-panel-usage-snapshots.timer
  vpn-store-panel-daily-usage.timer
  vpn-store-revenue-scan-dry-run.timer
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
  scripts/doctor.sh [--install-dir DIR] [options]

Core checks run by default. Live network checks and system integrations are opt-in.

Options:
  --install-dir DIR          Installed VPN Store directory.
  --dry-run                  Print/check the plan without running Django, nginx, or systemd commands.
  --systemd                  Check web and Telegram systemd services.
  --nginx                    Check nginx config and VPN Store site files.
  --live-bot                 Pass --live-bot to check_integrations.
  --live-xui                 Pass --live-xui to check_integrations.
  --timers                   Check known VPN Store systemd timers.
  --disk                     Check disk space. Enabled by default.
  --permissions              Check runtime file permissions. Enabled by default.
  --json                     Emit a JSON summary instead of human output.
  --no-fail                  Exit 0 even when checks fail.
  --verbose                  Print extra non-secret detail.
  --web-service-name NAME
  --telegram-service-name NAME
  --nginx-site-name NAME
  -h, --help                 Show this help.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
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
      --live-bot)
        LIVE_BOT=1
        ;;
      --live-xui)
        LIVE_XUI=1
        ;;
      --systemd)
        CHECK_SYSTEMD=1
        ;;
      --nginx)
        CHECK_NGINX=1
        ;;
      --timers)
        CHECK_TIMERS=1
        ;;
      --disk)
        CHECK_DISK=1
        ;;
      --permissions)
        CHECK_PERMISSIONS=1
        ;;
      --json)
        JSON_OUTPUT=1
        ;;
      --no-fail)
        NO_FAIL=1
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

record() {
  local status="$1"
  local name="$2"
  local message="$3"
  message="${message//$'\t'/ }"
  printf '%s\t%s\t%s\n' "$status" "$name" "$message" >> "$RESULTS_FILE"
  if (( ! JSON_OUTPUT )); then
    printf '[%s] %s - %s\n' "${status^^}" "$name" "$message"
  fi
}

record_pass() { record "pass" "$1" "$2"; }
record_warn() { record "warn" "$1" "$2"; }
record_fail() { record "fail" "$1" "$2"; }
record_info() { record "info" "$1" "$2"; }

env_value() {
  local env_path="$1"
  local key="$2"
  [[ -f "$env_path" ]] || return 0
  python3 - "$env_path" "$key" <<'PY'
import shlex
import sys

path, wanted = sys.argv[1:3]
try:
    lines = open(path, encoding="utf-8").read().splitlines()
except OSError:
    raise SystemExit(0)

for line in lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        continue
    key, value = stripped.split("=", 1)
    if key.strip() != wanted:
        continue
    try:
        parts = shlex.split(value, posix=True)
        print(parts[0] if parts else "")
    except ValueError:
        print(value.strip().strip("'\""))
    break
PY
}

absolute_path() {
  local path="$1"
  local base="$2"
  if [[ "$path" = /* ]]; then
    printf '%s' "$path"
  else
    printf '%s/%s' "$base" "$path"
  fi
}

resolve_db_path() {
  local env_path="$INSTALL_DIR/.env"
  local db_path="${SQLITE_DATABASE_PATH:-}"
  if [[ -z "$db_path" ]]; then
    db_path="$(env_value "$env_path" SQLITE_DATABASE_PATH)"
  fi
  db_path="${db_path:-$INSTALL_DIR/data/db.sqlite3}"
  absolute_path "$db_path" "$INSTALL_DIR"
}

source_env_for_django() {
  if (( DRY_RUN )); then
    record_info "env" "would source $INSTALL_DIR/.env with secrets redacted"
    return 0
  fi
  [[ -f "$INSTALL_DIR/.env" ]] || return 1
  set -a
  # shellcheck source=/dev/null
  . "$INSTALL_DIR/.env"
  set +a
}

summarize_output() {
  local output="$1"
  if (( VERBOSE && ! JSON_OUTPUT )); then
    DOCTOR_OUTPUT="$output" python3 - <<'PY'
import os
import re

secret_re = re.compile(r"(?i)(secret|token|password|credential|api[_-]?key|webhook|card)([^=\s:]*)([=\s:]+)(\S+)")
for line in os.environ.get("DOCTOR_OUTPUT", "").splitlines()[:40]:
    print(secret_re.sub(lambda m: m.group(1) + m.group(2) + m.group(3) + "[REDACTED]", line))
PY
  fi
}

check_core_paths() {
  if [[ -d "$INSTALL_DIR" ]]; then
    record_pass "install-dir" "exists: $INSTALL_DIR"
  else
    record_fail "install-dir" "missing: $INSTALL_DIR"
  fi

  if [[ -f "$INSTALL_DIR/.env" ]]; then
    record_pass "env-file" "exists"
  else
    record_fail "env-file" "missing: $INSTALL_DIR/.env"
  fi

  if [[ -d "$INSTALL_DIR/venv" ]]; then
    record_pass "venv" "exists"
  else
    record_fail "venv" "missing: $INSTALL_DIR/venv"
  fi

  if [[ -f "$INSTALL_DIR/manage.py" ]]; then
    record_pass "manage.py" "exists"
  else
    record_fail "manage.py" "missing: $INSTALL_DIR/manage.py"
  fi

  local db_path
  db_path="$(resolve_db_path)"
  if [[ -f "$db_path" ]]; then
    record_pass "database" "exists: $db_path"
  else
    record_fail "database" "missing: $db_path"
  fi

  local dir=""
  for dir in media static_root backups logs; do
    if [[ -d "$INSTALL_DIR/$dir" ]]; then
      record_pass "dir:$dir" "exists"
    else
      record_warn "dir:$dir" "missing: $INSTALL_DIR/$dir"
    fi
  done
}

check_permissions() {
  (( CHECK_PERMISSIONS )) || return 0
  local path=""
  local mode=""
  local group_digit=""
  local other_digit=""

  path="$INSTALL_DIR/.env"
  if [[ -f "$path" ]]; then
    mode="$(stat -c '%a' "$path" 2>/dev/null || true)"
    group_digit="${mode: -2:1}"
    other_digit="${mode: -1}"
    if [[ -n "$mode" && "$group_digit" =~ ^[0-7]$ && "$other_digit" =~ ^[0-7]$ && $((10#$group_digit)) -eq 0 && $((10#$other_digit)) -eq 0 ]]; then
      record_pass "permissions:.env" "mode $mode is owner-only"
    else
      record_fail "permissions:.env" "mode ${mode:-unknown} is not owner-only"
    fi
  else
    record_fail "permissions:.env" "cannot check missing .env"
  fi

  for path in "$INSTALL_DIR/install.config.json" "$INSTALL_DIR/scripts/backup.sh" "$INSTALL_DIR/scripts/update.sh"; do
    [[ -e "$path" ]] || continue
    mode="$(stat -c '%a' "$path" 2>/dev/null || true)"
    other_digit="${mode: -1}"
    if [[ -n "$mode" && "$other_digit" =~ ^[0-7]$ && $((10#$other_digit & 4)) -eq 0 ]]; then
      record_pass "permissions:$(basename "$path")" "not world-readable (mode $mode)"
    else
      record_warn "permissions:$(basename "$path")" "world-readable or unknown mode ${mode:-unknown}"
    fi
  done
}

check_disk() {
  (( CHECK_DISK )) || return 0
  local target="$INSTALL_DIR"
  [[ -d "$target" ]] || target="$(dirname "$INSTALL_DIR")"
  if (( DRY_RUN )); then
    record_info "disk" "would check free disk space for $target"
    return 0
  fi
  local line=""
  line="$(df -Pk "$target" 2>/dev/null | awk 'NR==2 {print $4, $5}' || true)"
  if [[ -z "$line" ]]; then
    record_warn "disk" "could not read disk usage for $target"
    return 0
  fi
  local available_kb used_pct
  available_kb="$(awk '{print $1}' <<<"$line")"
  used_pct="$(awk '{print $2}' <<<"$line" | tr -d '%')"
  if (( available_kb < 1048576 || used_pct >= 90 )); then
    record_warn "disk" "low space: ${available_kb}KB available, ${used_pct}% used"
  else
    record_pass "disk" "${available_kb}KB available, ${used_pct}% used"
  fi
}

run_django_command() {
  local check_name="$1"
  shift
  local python="$INSTALL_DIR/venv/bin/python"
  if (( DRY_RUN )); then
    record_info "$check_name" "would run: $python $INSTALL_DIR/manage.py $*"
    return 0
  fi
  if [[ ! -x "$python" ]]; then
    record_fail "$check_name" "virtualenv python missing: $python"
    return 0
  fi
  if [[ ! -f "$INSTALL_DIR/manage.py" ]]; then
    record_fail "$check_name" "manage.py missing"
    return 0
  fi
  local output=""
  if output="$(cd "$INSTALL_DIR" && "$python" "$INSTALL_DIR/manage.py" "$@" 2>&1)"; then
    record_pass "$check_name" "passed"
    summarize_output "$output"
  else
    record_fail "$check_name" "failed"
    summarize_output "$output"
  fi
}

check_django() {
  source_env_for_django || record_fail "env" "could not source $INSTALL_DIR/.env"
  run_django_command "django-check" check

  local integration_args=(check_integrations --no-fail)
  if (( LIVE_BOT )); then
    integration_args+=(--live-bot)
  fi
  if (( LIVE_XUI )); then
    integration_args+=(--live-xui)
  fi
  run_django_command "check-integrations" "${integration_args[@]}"

  if (( DRY_RUN )); then
    record_info "migrations" "would run showmigrations --plan"
    return 0
  fi

  local python="$INSTALL_DIR/venv/bin/python"
  local output=""
  if output="$(cd "$INSTALL_DIR" && "$python" "$INSTALL_DIR/manage.py" showmigrations --plan 2>&1)"; then
    local unapplied
    unapplied="$(printf '%s\n' "$output" | grep -c '^[[:space:]]*\[ \]' || true)"
    if [[ "$unapplied" == "0" ]]; then
      record_pass "migrations" "no unapplied migrations"
    else
      record_warn "migrations" "$unapplied unapplied migration(s)"
      if (( VERBOSE && ! JSON_OUTPUT )); then
        printf '%s\n' "$output" | grep '^[[:space:]]*\[ \]' | head -40 || true
      fi
    fi
  else
    record_fail "migrations" "could not inspect migration plan"
    summarize_output "$output"
  fi
}

check_systemd() {
  (( CHECK_SYSTEMD )) || return 0
  if (( DRY_RUN )); then
    record_info "systemd" "would check $WEB_SERVICE_NAME.service and $TELEGRAM_SERVICE_NAME.service"
    return 0
  fi
  command -v systemctl >/dev/null 2>&1 || {
    record_fail "systemd" "systemctl not found"
    return 0
  }
  local unit=""
  local enabled=""
  local active=""
  for unit in "$WEB_SERVICE_NAME.service" "$TELEGRAM_SERVICE_NAME.service"; do
    enabled="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
    active="$(systemctl is-active "$unit" 2>/dev/null || true)"
    if [[ "$active" == "active" ]]; then
      record_pass "systemd:$unit" "enabled=$enabled active=$active"
    else
      record_warn "systemd:$unit" "enabled=$enabled active=$active"
    fi
  done
}

check_nginx() {
  (( CHECK_NGINX )) || return 0
  local available_path="/etc/nginx/sites-available/$NGINX_SITE_NAME.conf"
  local enabled_path="/etc/nginx/sites-enabled/$NGINX_SITE_NAME.conf"
  if (( DRY_RUN )); then
    record_info "nginx" "would run nginx -t and check $available_path / $enabled_path"
    return 0
  fi
  if command -v nginx >/dev/null 2>&1; then
    local output=""
    if output="$(nginx -t 2>&1)"; then
      record_pass "nginx -t" "passed"
      summarize_output "$output"
    else
      record_fail "nginx -t" "failed"
      summarize_output "$output"
    fi
  else
    record_fail "nginx" "nginx command not found"
  fi
  if [[ -e "$available_path" || -L "$available_path" ]]; then
    record_pass "nginx:available" "exists: $available_path"
  else
    record_warn "nginx:available" "missing: $available_path"
  fi
  if [[ -e "$enabled_path" || -L "$enabled_path" ]]; then
    record_pass "nginx:enabled" "exists: $enabled_path"
  else
    record_warn "nginx:enabled" "missing: $enabled_path"
  fi
}

check_timers() {
  (( CHECK_TIMERS )) || return 0
  if (( DRY_RUN )); then
    record_info "timers" "would check known vpn-store timer enabled/active states"
    return 0
  fi
  command -v systemctl >/dev/null 2>&1 || {
    record_fail "timers" "systemctl not found"
    return 0
  }
  local timer=""
  local enabled=""
  local active=""
  for timer in "${KNOWN_TIMERS[@]}"; do
    enabled="$(systemctl is-enabled "$timer" 2>/dev/null || true)"
    active="$(systemctl is-active "$timer" 2>/dev/null || true)"
    if [[ "$enabled" == "enabled" || "$active" == "active" ]]; then
      record_pass "timer:$timer" "enabled=$enabled active=$active"
    else
      record_info "timer:$timer" "enabled=$enabled active=$active"
    fi
  done
}

emit_summary() {
  local fail_count warn_count pass_count
  fail_count="$(awk -F '\t' '$1=="fail"{count++} END{print count+0}' "$RESULTS_FILE")"
  warn_count="$(awk -F '\t' '$1=="warn"{count++} END{print count+0}' "$RESULTS_FILE")"
  pass_count="$(awk -F '\t' '$1=="pass"{count++} END{print count+0}' "$RESULTS_FILE")"

  if (( JSON_OUTPUT )); then
    python3 - "$RESULTS_FILE" "$INSTALL_DIR" "$DRY_RUN" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
results = []
for line in path.read_text(encoding="utf-8").splitlines():
    status, name, message = (line.split("\t", 2) + ["", ""])[:3]
    results.append({"status": status, "name": name, "message": message})
summary = {
    "install_dir": sys.argv[2],
    "dry_run": sys.argv[3] == "1",
    "counts": {
        "pass": sum(1 for item in results if item["status"] == "pass"),
        "warn": sum(1 for item in results if item["status"] == "warn"),
        "fail": sum(1 for item in results if item["status"] == "fail"),
        "info": sum(1 for item in results if item["status"] == "info"),
    },
    "results": results,
}
print(json.dumps(summary, indent=2, ensure_ascii=True))
PY
  else
    printf 'Summary: pass=%s warn=%s fail=%s\n' "$pass_count" "$warn_count" "$fail_count"
  fi

  if (( fail_count > 0 && ! NO_FAIL )); then
    return 1
  fi
  return 0
}

main() {
  parse_args "$@"
  RESULTS_FILE="$(mktemp)"
  trap 'rm -f "$RESULTS_FILE"' EXIT

  check_core_paths
  check_permissions
  check_disk
  check_django
  check_systemd
  check_nginx
  check_timers
  emit_summary
}

main "$@"
