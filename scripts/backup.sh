#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

INSTALL_DIR="$DEFAULT_INSTALL_DIR"
OUTPUT_DIR=""
DRY_RUN=0
YES=0
INCLUDE_MEDIA=0
COMPRESS=1
KEEP_LAST=""
VERBOSE=0
BACKUP_PREFIX="vpn-store-backup"
TEMP_STAGE=""

on_error() {
  local line="$1"
  local command="$2"
  printf 'ERROR: %s failed at line %s while running: %s\n' "$SCRIPT_NAME" "$line" "$command" >&2
}
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR

usage() {
  cat <<'EOF'
Usage:
  scripts/backup.sh --install-dir DIR [--output-dir DIR] [options]

Options:
  --install-dir DIR   Installed VPN Store directory.
  --output-dir DIR    Backup output directory. Default: <install-dir>/backups
  --dry-run           Print the backup plan only. No files are created.
  --yes               Accept confirmation prompts.
  --include-media     Include <install-dir>/media in the archive.
  --exclude-media     Exclude media files. Default.
  --compress          Write a gzip-compressed tar archive. Default.
  --keep-last N       Keep only the newest N vpn-store-backup-*.tar.gz files.
  --verbose           Print extra non-secret diagnostic detail.
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
  exit 1
}

verbose_log() {
  if (( VERBOSE )); then
    log "$*"
  fi
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
      --output-dir)
        shift
        [[ $# -gt 0 ]] || die "--output-dir requires a value."
        OUTPUT_DIR="$1"
        ;;
      --dry-run)
        DRY_RUN=1
        ;;
      --yes)
        YES=1
        ;;
      --include-media)
        INCLUDE_MEDIA=1
        ;;
      --exclude-media)
        INCLUDE_MEDIA=0
        ;;
      --compress)
        COMPRESS=1
        ;;
      --keep-last)
        shift
        [[ $# -gt 0 ]] || die "--keep-last requires a value."
        [[ "$1" =~ ^[0-9]+$ ]] || die "--keep-last must be a non-negative integer."
        KEEP_LAST="$1"
        ;;
      --verbose)
        VERBOSE=1
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

resolve_paths() {
  OUTPUT_DIR="${OUTPUT_DIR:-$INSTALL_DIR/backups}"
  local env_path="$INSTALL_DIR/.env"
  local db_path="${SQLITE_DATABASE_PATH:-}"
  if [[ -z "$db_path" ]]; then
    db_path="$(env_value "$env_path" SQLITE_DATABASE_PATH)"
  fi
  db_path="${db_path:-$INSTALL_DIR/data/db.sqlite3}"
  DB_PATH="$(absolute_path "$db_path" "$INSTALL_DIR")"
}

validate_inputs() {
  [[ -d "$INSTALL_DIR" ]] || die "Install directory not found: $INSTALL_DIR"
  [[ -f "$DB_PATH" ]] || die "SQLite database not found: $DB_PATH"
  if [[ -n "$KEEP_LAST" && "$KEEP_LAST" == "0" ]]; then
    warn "--keep-last 0 would remove all matching backup archives after this run."
  fi
}

print_plan() {
  log "Backup plan:"
  log "  install dir: $INSTALL_DIR"
  log "  output dir: $OUTPUT_DIR"
  log "  database: $DB_PATH"
  log "  include .env: $([[ -f "$INSTALL_DIR/.env" ]] && printf yes || printf no)"
  log "  include install.config.json: $([[ -f "$INSTALL_DIR/install.config.json" ]] && printf yes || printf no)"
  log "  include media: $([[ "$INCLUDE_MEDIA" == "1" ]] && printf yes || printf no)"
  log "  include static_root: no (rebuildable by collectstatic)"
  log "  include generated systemd/nginx configs when present: yes"
  if [[ -n "$KEEP_LAST" ]]; then
    log "  retention: keep newest $KEEP_LAST archives"
  fi
  if (( DRY_RUN )); then
    log "DRY-RUN: no archive, manifest, checksum, or retention deletion will be written."
  fi
}

add_section() {
  local section="$1"
  case ",$INCLUDED_SECTIONS," in
    *,"$section",*) ;;
    *) INCLUDED_SECTIONS="${INCLUDED_SECTIONS:+$INCLUDED_SECTIONS,}$section" ;;
  esac
}

copy_file_into_payload() {
  local source="$1"
  local relative="$2"
  local payload="$3"
  [[ -e "$source" || -L "$source" ]] || return 0
  [[ -r "$source" || -L "$source" ]] || {
    warn "Skipping unreadable file: $source"
    return 0
  }
  mkdir -p "$payload/$(dirname "$relative")"
  cp -a "$source" "$payload/$relative"
  verbose_log "Included $relative"
}

copy_dir_into_payload() {
  local source="$1"
  local relative="$2"
  local payload="$3"
  [[ -d "$source" ]] || return 0
  mkdir -p "$payload/$(dirname "$relative")"
  cp -a "$source" "$payload/$relative"
  verbose_log "Included $relative/"
}

backup_sqlite() {
  local source="$1"
  local target="$2"
  mkdir -p "$(dirname "$target")"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$source" ".backup '$target'"
  else
    warn "sqlite3 command not found; falling back to file copy for SQLite backup."
    cp -a "$source" "$target"
  fi
}

collect_systemd_files() {
  local payload="$1"
  local found=0
  local unit=""
  shopt -s nullglob
  for unit in /etc/systemd/system/vpn-store*.service /etc/systemd/system/vpn-store*.timer; do
    copy_file_into_payload "$unit" "systemd/$(basename "$unit")" "$payload"
    found=1
  done
  shopt -u nullglob
  if (( found )); then
    add_section "systemd"
  fi
}

collect_nginx_files() {
  local payload="$1"
  local found=0
  local conf=""
  shopt -s nullglob
  for conf in /etc/nginx/sites-available/vpn-store*.conf /etc/nginx/sites-enabled/vpn-store*.conf; do
    copy_file_into_payload "$conf" "nginx/${conf#/etc/nginx/}" "$payload"
    found=1
  done
  shopt -u nullglob
  if (( found )); then
    add_section "nginx"
  fi
}

write_manifest() {
  local payload="$1"
  local manifest="$payload/manifest.json"
  local git_commit=""
  if command -v git >/dev/null 2>&1 && [[ -d "$INSTALL_DIR/.git" ]]; then
    git_commit="$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || true)"
  fi
  MANIFEST_TIMESTAMP="$BACKUP_TIMESTAMP" \
  MANIFEST_INSTALL_DIR="$INSTALL_DIR" \
  MANIFEST_DB_PATH="$DB_PATH" \
  MANIFEST_GIT_COMMIT="$git_commit" \
  MANIFEST_INCLUDED_SECTIONS="$INCLUDED_SECTIONS" \
  python3 - "$payload" "$manifest" <<'PY'
import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path

payload = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])
secret_re = re.compile(r"(secret|token|password|credential|api[_-]?key|private|webhook|card)", re.I)

def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def redact_value(key, value):
    if secret_re.search(str(key)):
        return "[REDACTED]" if value not in ("", None) else ""
    return value

def parse_env(path):
    if not path.exists():
        return {}
    output = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        try:
            parts = shlex.split(value, posix=True)
            parsed = parts[0] if parts else ""
        except ValueError:
            parsed = value.strip().strip("'\"")
        output[key] = redact_value(key, parsed)
    return output

def redact_json(value, key=""):
    if isinstance(value, dict):
        return {k: redact_json(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_json(item, key) for item in value]
    return redact_value(key, value)

def parse_config(path):
    if not path.exists():
        return None
    try:
        return redact_json(json.loads(path.read_text(encoding="utf-8")))
    except Exception as exc:
        return {"error": f"could not parse config: {exc.__class__.__name__}"}

checksums = {}
for path in sorted(payload.rglob("*")):
    if path == manifest_path or not path.is_file() or path.is_symlink():
        continue
    checksums[str(path.relative_to(payload))] = sha256(path)

manifest = {
    "timestamp": os.environ["MANIFEST_TIMESTAMP"],
    "project": {
        "git_commit": os.environ.get("MANIFEST_GIT_COMMIT") or None,
    },
    "install_dir": os.environ["MANIFEST_INSTALL_DIR"],
    "database_path": os.environ["MANIFEST_DB_PATH"],
    "included_sections": [item for item in os.environ.get("MANIFEST_INCLUDED_SECTIONS", "").split(",") if item],
    "file_checksums_sha256": checksums,
    "redacted_runtime": {
        ".env": parse_env(payload / "runtime" / ".env"),
        "install.config.json": parse_config(payload / "runtime" / "install.config.json"),
    },
}
manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
PY
}

create_backup() {
  local timestamp="$1"
  local archive_name="$BACKUP_PREFIX-$timestamp"
  local archive_path="$OUTPUT_DIR/$archive_name.tar.gz"
  local tmp_archive="$archive_path.tmp"
  local stage=""
  local payload=""

  confirm "Create backup at $archive_path?" || die "Backup cancelled."
  umask 077
  install -d -m 0700 "$OUTPUT_DIR"
  stage="$(mktemp -d)"
  TEMP_STAGE="$stage"
  payload="$stage/$archive_name"
  mkdir -p "$payload"

  INCLUDED_SECTIONS=""
  backup_sqlite "$DB_PATH" "$payload/database/db.sqlite3"
  add_section "database"

  if [[ -f "$INSTALL_DIR/.env" ]]; then
    copy_file_into_payload "$INSTALL_DIR/.env" "runtime/.env" "$payload"
    add_section "env"
  else
    warn ".env not found; continuing without it."
  fi

  if [[ -f "$INSTALL_DIR/install.config.json" ]]; then
    copy_file_into_payload "$INSTALL_DIR/install.config.json" "runtime/install.config.json" "$payload"
    add_section "install_config"
  else
    warn "install.config.json not found; continuing without it."
  fi

  if (( INCLUDE_MEDIA )); then
    if [[ -d "$INSTALL_DIR/media" ]]; then
      copy_dir_into_payload "$INSTALL_DIR/media" "media" "$payload"
      add_section "media"
    else
      warn "media directory not found; continuing without media."
    fi
  fi

  collect_systemd_files "$payload"
  collect_nginx_files "$payload"
  add_section "manifest"
  write_manifest "$payload"

  tar -C "$stage" -czf "$tmp_archive" "$archive_name"
  chmod 600 "$tmp_archive"
  mv "$tmp_archive" "$archive_path"
  chmod 600 "$archive_path"
  rm -rf "$stage"
  TEMP_STAGE=""

  local checksum
  checksum="$(sha256sum "$archive_path" | awk '{print $1}')"
  log "Backup created: $archive_path"
  log "SHA256: $checksum"
}

apply_retention() {
  [[ -n "$KEEP_LAST" ]] || return 0
  if (( DRY_RUN )); then
    log "DRY-RUN: would keep newest $KEEP_LAST matching backup archive(s) in $OUTPUT_DIR."
    return 0
  fi
  python3 - "$OUTPUT_DIR" "$KEEP_LAST" <<'PY' | while IFS= read -r old_backup; do
import sys
from pathlib import Path

directory = Path(sys.argv[1])
keep = int(sys.argv[2])
backups = sorted(
    directory.glob("vpn-store-backup-*.tar.gz"),
    key=lambda path: path.stat().st_mtime,
    reverse=True,
)
for path in backups[keep:]:
    print(path)
PY
    rm -f "$old_backup"
    log "Removed old backup: $old_backup"
  done
}

main() {
  trap '[[ -z "${TEMP_STAGE:-}" ]] || rm -rf "$TEMP_STAGE"' EXIT
  parse_args "$@"
  resolve_paths
  validate_inputs
  BACKUP_TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
  print_plan
  if (( DRY_RUN )); then
    apply_retention
    return 0
  fi
  create_backup "$BACKUP_TIMESTAMP"
  apply_retention
}

main "$@"
