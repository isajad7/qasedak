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
INCLUDE_ENV=0
INCLUDE_SYSTEM=0
COMPRESS=1
KEEP_LAST=""
VERBOSE=0
BACKUP_PREFIX="qasedak-backup"
TEMP_STAGE=""
DATABASE_ENGINE=""
DB_PATH=""
POSTGRES_DB=""
POSTGRES_USER=""
POSTGRES_PASSWORD=""
POSTGRES_HOST=""
POSTGRES_PORT=""
POSTGRES_SSLMODE=""

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
  --include-env       Include .env and install.config.json under env/. Off by default.
  --exclude-env       Exclude env files. Default.
  --include-system    Include generated systemd/nginx reference files. Off by default.
  --exclude-system    Exclude system reference files. Default.
  --compress          Write a gzip-compressed tar archive. Default.
  --keep-last N       Keep only the newest N qasedak-backup-*.tar.gz files.
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
      --include-env)
        INCLUDE_ENV=1
        ;;
      --exclude-env)
        INCLUDE_ENV=0
        ;;
      --include-system)
        INCLUDE_SYSTEM=1
        ;;
      --exclude-system)
        INCLUDE_SYSTEM=0
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

env_or_file() {
  local key="$1"
  local env_path="$2"
  local value="${!key:-}"
  if [[ -z "$value" ]]; then
    value="$(env_value "$env_path" "$key")"
  fi
  printf '%s' "$value"
}

normalize_database_engine() {
  local engine="${1:-sqlite}"
  engine="${engine,,}"
  case "$engine" in
    sqlite|sqlite3)
      printf 'sqlite'
      ;;
    postgres|postgresql)
      printf 'postgres'
      ;;
    *)
      die "Unsupported DATABASE_ENGINE: $engine"
      ;;
  esac
}

validate_pg_identifier() {
  local label="$1"
  local value="$2"
  [[ "$value" =~ ^[a-zA-Z0-9_]+$ ]] || die "$label may contain only letters, numbers, and underscores."
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
  DATABASE_ENGINE="$(normalize_database_engine "$(env_or_file DATABASE_ENGINE "$env_path")")"
  if [[ "$DATABASE_ENGINE" == "sqlite" ]]; then
    local db_path
    db_path="$(env_or_file SQLITE_DATABASE_PATH "$env_path")"
    db_path="${db_path:-$INSTALL_DIR/data/db.sqlite3}"
    DB_PATH="$(absolute_path "$db_path" "$INSTALL_DIR")"
    return 0
  fi
  POSTGRES_DB="$(env_or_file POSTGRES_DB "$env_path")"
  POSTGRES_USER="$(env_or_file POSTGRES_USER "$env_path")"
  POSTGRES_PASSWORD="$(env_or_file POSTGRES_PASSWORD "$env_path")"
  POSTGRES_HOST="$(env_or_file POSTGRES_HOST "$env_path")"
  POSTGRES_PORT="$(env_or_file POSTGRES_PORT "$env_path")"
  POSTGRES_SSLMODE="$(env_or_file POSTGRES_SSLMODE "$env_path")"
  POSTGRES_DB="${POSTGRES_DB:-qasedak}"
  POSTGRES_USER="${POSTGRES_USER:-qasedak}"
  POSTGRES_HOST="${POSTGRES_HOST:-127.0.0.1}"
  POSTGRES_PORT="${POSTGRES_PORT:-5432}"
  POSTGRES_SSLMODE="${POSTGRES_SSLMODE:-prefer}"
}

validate_inputs() {
  [[ -d "$INSTALL_DIR" ]] || die "Install directory not found: $INSTALL_DIR"
  if [[ "$DATABASE_ENGINE" == "sqlite" ]]; then
    [[ -f "$DB_PATH" ]] || die "SQLite database not found: $DB_PATH"
  else
    validate_pg_identifier "PostgreSQL database name" "$POSTGRES_DB"
    validate_pg_identifier "PostgreSQL user name" "$POSTGRES_USER"
    [[ "$POSTGRES_PORT" =~ ^[0-9]+$ ]] || die "PostgreSQL port must be numeric."
    if (( DRY_RUN )); then
      [[ -n "$POSTGRES_PASSWORD" ]] || warn "POSTGRES_PASSWORD is not set; a real PostgreSQL backup would fail."
      command -v pg_dump >/dev/null 2>&1 || warn "pg_dump is not installed; a real PostgreSQL backup would fail."
    else
      [[ -n "$POSTGRES_PASSWORD" ]] || die "POSTGRES_PASSWORD is required for PostgreSQL backups."
      command -v pg_dump >/dev/null 2>&1 || die "pg_dump is required for PostgreSQL backups."
    fi
  fi
  if [[ -n "$KEEP_LAST" && "$KEEP_LAST" == "0" ]]; then
    warn "--keep-last 0 would remove all matching backup archives after this run."
  fi
}

print_plan() {
  log "Backup plan:"
  log "  install dir: $INSTALL_DIR"
  log "  output dir: $OUTPUT_DIR"
  log "  database engine: $DATABASE_ENGINE"
  if [[ "$DATABASE_ENGINE" == "sqlite" ]]; then
    log "  database: $DB_PATH"
  else
    log "  database: postgres $POSTGRES_USER@$POSTGRES_HOST:$POSTGRES_PORT/$POSTGRES_DB"
    log "  database dump: pg_dump -Fc to database/db.postgres.dump"
  fi
  log "  .env available: $([[ -f "$INSTALL_DIR/.env" ]] && printf yes || printf no)"
  log "  include env files: $([[ "$INCLUDE_ENV" == "1" ]] && printf yes || printf no)"
  log "  include media: $([[ "$INCLUDE_MEDIA" == "1" ]] && printf yes || printf no)"
  log "  include static_root: no (rebuildable by collectstatic)"
  log "  include generated systemd/nginx configs when present: $([[ "$INCLUDE_SYSTEM" == "1" ]] && printf yes || printf no)"
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

backup_postgres() {
  local target="$1"
  mkdir -p "$(dirname "$target")"
  PGPASSWORD="$POSTGRES_PASSWORD" pg_dump \
    -Fc \
    -h "$POSTGRES_HOST" \
    -p "$POSTGRES_PORT" \
    -U "$POSTGRES_USER" \
    -d "$POSTGRES_DB" \
    -f "$target"
  chmod 600 "$target"
}

collect_systemd_files() {
  local payload="$1"
  local found=0
  local unit=""
  shopt -s nullglob
  for unit in /etc/systemd/system/vpn-store*.service /etc/systemd/system/vpn-store*.timer; do
    copy_file_into_payload "$unit" "system/systemd/$(basename "$unit")" "$payload"
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
    copy_file_into_payload "$conf" "system/nginx/${conf#/etc/nginx/}" "$payload"
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
  MANIFEST_DB_ENGINE="$DATABASE_ENGINE" \
  MANIFEST_DB_PATH="$DB_PATH" \
  MANIFEST_POSTGRES_DB="$POSTGRES_DB" \
  MANIFEST_POSTGRES_USER="$POSTGRES_USER" \
  MANIFEST_POSTGRES_HOST="$POSTGRES_HOST" \
  MANIFEST_POSTGRES_PORT="$POSTGRES_PORT" \
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
secret_re = re.compile(
    r"(secret|token|password|credential|api[_-]?key|private|webhook|card|uuid|config|sub_?link|phone|email|chat)",
    re.I,
)
secret_value_re = re.compile(
    r"(?i)(vless|vmess|trojan)://|"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
    r"\b\d{6,}:[A-Za-z0-9_-]{16,}\b|"
    r"(?:\d[ -]?){16,19}|"
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|"
    r"\+?\d[\d\s().-]{7,}\d"
)

def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def redact_value(key, value):
    if secret_re.search(str(key)):
        return "[REDACTED]" if value not in ("", None) else ""
    if isinstance(value, str) and secret_value_re.search(value):
        return "[REDACTED]"
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
db_size = 0
media_count = 0
for path in sorted(payload.rglob("*")):
    if path == manifest_path or path.name == "checksums.sha256" or not path.is_file() or path.is_symlink():
        continue
    rel = str(path.relative_to(payload))
    checksums[rel] = sha256(path)
    if rel.startswith("database/"):
        db_size += path.stat().st_size
    if rel.startswith("media/"):
        media_count += 1

runtime_env = payload / "env" / "production.env"
legacy_runtime_env = payload / "runtime" / ".env"
env_preview = parse_env(runtime_env if runtime_env.exists() else legacy_runtime_env)
engine = os.environ.get("MANIFEST_DB_ENGINE") or "sqlite"
included_sections = [item for item in os.environ.get("MANIFEST_INCLUDED_SECTIONS", "").split(",") if item]

manifest = {
    "qasedak_backup_version": "1",
    "created_at": os.environ["MANIFEST_TIMESTAMP"],
    "app_version": "",
    "git_commit": os.environ.get("MANIFEST_GIT_COMMIT") or None,
    "django_settings_module": os.environ.get("DJANGO_SETTINGS_MODULE") or "",
    "database_engine": engine,
    "database_vendor": "postgresql" if engine == "postgres" else "sqlite",
    "postgres_dump_format": "custom" if engine == "postgres" else "",
    "database_name_redacted": os.environ.get("MANIFEST_POSTGRES_DB") and "[configured]",
    "database_schema_migrations": {},
    "backup_type": "full_transfer" if any(item in included_sections for item in ("env", "systemd", "nginx")) else ("db_and_media" if "media" in included_sections else "db_only"),
    "includes_media": "media" in included_sections,
    "includes_env": "env" in included_sections,
    "includes_system": any(item in included_sections for item in ("systemd", "nginx")),
    "media_file_count": media_count,
    "db_size_bytes": db_size,
    "archive_size_bytes": 0,
    "created_by": os.environ.get("USER") or "",
    "hostname": os.uname().nodename if hasattr(os, "uname") else "",
    "install_dir": os.environ["MANIFEST_INSTALL_DIR"],
    "revenue_engine_dry_run": None,
    "allow_global_inbound_fallback": None,
    "warnings": [],
    "checksums": checksums,
    "redaction_policy": "Manifest redacts secrets, credentials, card data, UUIDs, config links, subscription links, and direct contact identifiers.",
    "timestamp": os.environ["MANIFEST_TIMESTAMP"],
    "project": {
        "git_commit": os.environ.get("MANIFEST_GIT_COMMIT") or None,
    },
    "install_dir": os.environ["MANIFEST_INSTALL_DIR"],
    "database": {
        "engine": engine,
        "path": os.environ.get("MANIFEST_DB_PATH") or None,
        "postgres": {
            "database": os.environ.get("MANIFEST_POSTGRES_DB") or None,
            "user": os.environ.get("MANIFEST_POSTGRES_USER") or None,
            "host": os.environ.get("MANIFEST_POSTGRES_HOST") or None,
            "port": os.environ.get("MANIFEST_POSTGRES_PORT") or None,
            "dump_file": "database/db.postgres.dump",
            "dump_format": "custom",
        } if engine == "postgres" else None,
    },
    "database_path": os.environ["MANIFEST_DB_PATH"],
    "included_sections": included_sections,
    "file_checksums_sha256": checksums,
    "redacted_runtime": {
        ".env": env_preview,
        "install.config.json": parse_config(payload / "env" / "install.config.json"),
    },
}
manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
PY
}

write_checksums() {
  local payload="$1"
  (
    cd "$payload"
    find . -type f ! -name checksums.sha256 -printf '%P\n' | sort | while IFS= read -r relative; do
      sha256sum "$relative"
    done > checksums.sha256
  )
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
  payload="$stage/payload"
  mkdir -p "$payload"

  INCLUDED_SECTIONS=""
  if [[ "$DATABASE_ENGINE" == "postgres" ]]; then
    backup_postgres "$payload/database/db.postgres.dump"
  else
    backup_sqlite "$DB_PATH" "$payload/database/db.sqlite3"
  fi
  add_section "database"

  if (( INCLUDE_ENV )); then
    if [[ -f "$INSTALL_DIR/.env" ]]; then
      copy_file_into_payload "$INSTALL_DIR/.env" "env/production.env" "$payload"
      add_section "env"
    else
      warn ".env not found; continuing without it."
    fi

    if [[ -f "$INSTALL_DIR/install.config.json" ]]; then
      copy_file_into_payload "$INSTALL_DIR/install.config.json" "env/install.config.json" "$payload"
      add_section "install_config"
    else
      warn "install.config.json not found; continuing without it."
    fi
  fi

  if (( INCLUDE_MEDIA )); then
    if [[ -d "$INSTALL_DIR/media" ]]; then
      copy_dir_into_payload "$INSTALL_DIR/media" "media" "$payload"
      add_section "media"
    else
      warn "media directory not found; continuing without media."
    fi
  fi

  if (( INCLUDE_SYSTEM )); then
    collect_systemd_files "$payload"
    collect_nginx_files "$payload"
  fi
  add_section "manifest"
  write_manifest "$payload"
  write_checksums "$payload"

  find "$payload" -mindepth 1 -maxdepth 1 -printf '%P\0' | sort -z | tar -C "$payload" --null --files-from=- -czf "$tmp_archive"
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
    list(directory.glob("qasedak-backup-*.tar.gz")) + list(directory.glob("vpn-store-backup-*.tar.gz")),
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
