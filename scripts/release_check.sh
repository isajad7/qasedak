#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

QUICK=0
NO_TESTS=0
JSON_OUTPUT=0
RESULTS_FILE="$(mktemp)"
FILES_FILE="$(mktemp)"
FAILURES=0
WARNINGS=0

cleanup() {
  rm -f "$RESULTS_FILE" "$FILES_FILE"
}
trap cleanup EXIT

usage() {
  cat <<'EOF'
Usage:
  scripts/release_check.sh [--quick|--full] [--no-tests] [--json]

Options:
  --quick     Skip Django tests; run syntax, git, Django check, migration, and release scans.
  --full      Run all checks, including Django tests. This is the default.
  --no-tests  Skip Django tests.
  --json      Emit a JSON summary. Command failure output is suppressed.
  -h, --help  Show this help.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

parse_args() {
  while (($#)); do
    case "$1" in
      --quick)
        QUICK=1
        NO_TESTS=1
        ;;
      --full)
        QUICK=0
        ;;
      --no-tests)
        NO_TESTS=1
        ;;
      --json)
        JSON_OUTPUT=1
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
  case "$status" in
    fail) FAILURES=$((FAILURES + 1)) ;;
    warn) WARNINGS=$((WARNINGS + 1)) ;;
  esac
  if (( ! JSON_OUTPUT )); then
    printf '[%s] %s - %s\n' "${status^^}" "$name" "$message"
  fi
}

record_pass() { record "pass" "$1" "$2"; }
record_warn() { record "warn" "$1" "$2"; }
record_fail() { record "fail" "$1" "$2"; }

run_step() {
  local name="$1"
  shift
  local output=""
  if output="$("$@" 2>&1)"; then
    record_pass "$name" "ok"
  else
    local status=$?
    record_fail "$name" "exit $status"
    if (( ! JSON_OUTPUT && ${#output} > 0 )); then
      printf '%s\n' "$output" >&2
    fi
  fi
}

python_bin() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    printf '%s' "$PYTHON_BIN"
  elif [[ -x "$REPO_DIR/venv/bin/python" ]]; then
    printf '%s' "$REPO_DIR/venv/bin/python"
  elif command -v python3.12 >/dev/null 2>&1; then
    command -v python3.12
  else
    command -v python3
  fi
}

collect_release_files() {
  : > "$FILES_FILE"
  git -C "$REPO_DIR" ls-files -z >> "$FILES_FILE"
  git -C "$REPO_DIR" ls-files --others --exclude-standard -z >> "$FILES_FILE"
}

check_shell_syntax() {
  local scripts=()
  while IFS= read -r -d '' script; do
    scripts+=("$script")
  done < <(find "$REPO_DIR/scripts" -type f -name '*.sh' -print0 | sort -z)

  if ((${#scripts[@]} == 0)); then
    record_warn "shell-syntax" "no shell scripts found"
    return 0
  fi

  run_step "shell-syntax" bash -n "${scripts[@]}"
}

check_tracked_artifacts() {
  local found=0
  local path base
  while IFS= read -r -d '' path; do
    base="$(basename "$path")"
    case "$path" in
      .env|*/.env|.env.*|*/.env.*)
        [[ "$base" == ".env.example" ]] && continue
        ;;
      db.sqlite3|db.sqlite3.backup|*.sqlite3|*.sqlite3-*|data/*|media/*|venv/*|.venv/*|*/.venv/*|backups/*|logs/*|node_modules/*|static_root/*|private_imports/*|*.tar.gz|*/__pycache__/*|*.pyc)
        ;;
      *)
        continue
        ;;
    esac
    found=1
    record_fail "tracked-artifact" "$path"
  done < <(git -C "$REPO_DIR" ls-files -z)

  if (( ! found )); then
    record_pass "tracked-artifact" "no tracked runtime/generated artifacts"
  fi
}

check_unignored_artifacts() {
  local found=0
  local path base
  while IFS= read -r -d '' path; do
    base="$(basename "$path")"
    case "$path" in
      .env|*/.env|.env.*|*/.env.*)
        [[ "$base" == ".env.example" ]] && continue
        ;;
      db.sqlite3|db.sqlite3.backup|*.sqlite3|*.sqlite3-*|data/*|media/*|venv/*|.venv/*|*/.venv/*|backups/*|logs/*|node_modules/*|static_root/*|private_imports/*|*.tar.gz|*/__pycache__/*|*.pyc)
        ;;
      *)
        continue
        ;;
    esac
    found=1
    record_fail "release-artifact" "$path"
  done < <(git -C "$REPO_DIR" ls-files --others --exclude-standard -z)

  if (( ! found )); then
    record_pass "release-artifact" "no unignored runtime/generated artifacts"
  fi
}

check_production_values() {
  local findings
  findings="$(
    python3 - "$REPO_DIR" "$FILES_FILE" <<'PY'
import os
import re
import sys

repo_dir, files_path = sys.argv[1:3]

legacy_person_a = "sa" + "jad"
legacy_person_b = "ma" + "tin"
legacy_brand_en = "Azad" + "Net"
legacy_brand_fa = "آزاد" + "نت"

patterns = [
    ("legacy-domain-primary", re.compile(r"botsell" + r"\.panelwpvideo" + r"\.ir")),
    ("legacy-domain-secondary", re.compile(r"panel" + r"\.vawmusic" + r"\.ir|panel" + r"\.wavmusic" + r"\.ir")),
    ("legacy-ip-primary", re.compile(r"194" + r"\.62")),
    ("legacy-ip-secondary", re.compile(r"194" + r"\.5")),
    ("legacy-path-primary", re.compile(r"/var/www/" + "vpn_store")),
    ("legacy-path-secondary", re.compile(r"/var/www/" + legacy_person_b + "_panel")),
    ("legacy-service-primary", re.compile(r"gunicorn-" + legacy_person_a + r"|telegram-polling-" + legacy_person_a)),
    ("legacy-service-secondary", re.compile(r"gunicorn-" + legacy_person_b + r"|telegram-polling-" + legacy_person_b)),
    ("legacy-brand", re.compile(legacy_brand_en + r"|Azad" + r" Net|" + legacy_brand_fa)),
    ("legacy-name-primary", re.compile(r"(?<![A-Za-z0-9_])" + legacy_person_a + r"(?![A-Za-z0-9_])", re.IGNORECASE)),
    ("legacy-name-secondary", re.compile(r"(?<![A-Za-z0-9_])" + legacy_person_b + r"(?![A-Za-z0-9_])", re.IGNORECASE)),
    ("proxy-credentials", re.compile(r"\b(?:https?|socks5?h?)://[^\s/@:]+:[^\s/@]+@")),
    ("telegram-bot-token", re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")),
]
card_re = re.compile(r"(?<![A-Za-z0-9_-])(?:[0-9][ -]?){16,19}(?![A-Za-z0-9_-])")
allowed_cards = {
    "0000000000000000",
    "0000000000000001",
    "0000000000000002",
    "0000000000000009",
}

with open(files_path, "rb") as file:
    paths = [item.decode("utf-8", "surrogateescape") for item in file.read().split(b"\0") if item]

seen = set()
for rel_path in paths:
    if rel_path in seen:
        continue
    seen.add(rel_path)
    abs_path = os.path.join(repo_dir, rel_path)
    if not os.path.isfile(abs_path):
        continue
    try:
        with open(abs_path, "rb") as file:
            raw = file.read()
    except OSError:
        continue
    if b"\0" in raw:
        continue
    text = raw.decode("utf-8", "ignore")
    for label, regex in patterns:
        if regex.search(text):
            print(f"{rel_path}\t{label}")
    for match in card_re.finditer(text):
        normalized = re.sub(r"[^0-9]", "", match.group(0))
        if normalized in allowed_cards:
            continue
        print(f"{rel_path}\tcard-number-like")
        break
PY
  )"

  if [[ -z "$findings" ]]; then
    record_pass "production-scan" "no known production values or secret-like patterns"
    return 0
  fi

  while IFS=$'\t' read -r path label; do
    [[ -n "$path" ]] || continue
    record_fail "production-scan" "$path ($label)"
  done <<< "$findings"
}

emit_json() {
  python3 - "$RESULTS_FILE" "$FAILURES" "$WARNINGS" <<'PY'
import json
import sys

path, failures, warnings = sys.argv[1:4]
checks = []
with open(path, encoding="utf-8") as file:
    for line in file:
        status, name, message = line.rstrip("\n").split("\t", 2)
        checks.append({"status": status, "name": name, "message": message})

print(json.dumps({
    "status": "fail" if int(failures) else "pass",
    "failures": int(failures),
    "warnings": int(warnings),
    "checks": checks,
}, ensure_ascii=False, indent=2))
PY
}

main() {
  parse_args "$@"
  cd "$REPO_DIR"
  collect_release_files

  local py
  py="$(python_bin)"

  export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-core.settings.development}"
  export DJANGO_SECRET_KEY="${DJANGO_SECRET_KEY:-release-check-secret}"
  export DJANGO_DEBUG="${DJANGO_DEBUG:-False}"
  export SQLITE_DATABASE_PATH="${SQLITE_DATABASE_PATH:-/tmp/vpn-store-release-check.sqlite3}"
  export DJANGO_ALLOWED_HOSTS="${DJANGO_ALLOWED_HOSTS:-localhost,127.0.0.1}"
  export DJANGO_CSRF_TRUSTED_ORIGINS="${DJANGO_CSRF_TRUSTED_ORIGINS:-http://localhost}"
  export INSTALL_RUN_LIVE_BOT_CHECK=false
  export INSTALL_RUN_LIVE_XUI_CHECK=false
  export INSTALL_SEND_TELEGRAM_TEST_MESSAGE=false

  check_shell_syntax
  run_step "git-diff-check" git diff --check
  run_step "django-check" "$py" manage.py check
  if (( NO_TESTS )); then
    record_warn "django-tests" "skipped"
  else
    run_step "django-tests" "$py" manage.py test store.tests payments.tests
  fi
  run_step "migration-check" "$py" manage.py makemigrations --check --dry-run
  check_tracked_artifacts
  check_unignored_artifacts
  check_production_values

  if (( FAILURES )); then
    record_fail "release-readiness" "NOT_READY_SECRET_OR_ARTIFACTS"
  elif (( QUICK )); then
    record_pass "release-readiness" "READY_FOR_PRIVATE_GITHUB quick checks passed; run --full before release"
  else
    record_pass "release-readiness" "READY_FOR_PRIVATE_GITHUB; public release still requires license choice"
  fi

  if (( JSON_OUTPUT )); then
    emit_json
  fi

  (( FAILURES == 0 ))
}

main "$@"
