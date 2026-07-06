#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_NAME="$(basename "$0")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BACKUP_FILE=""
RESTORE_JOB_ID=""
DRY_RUN=0
INCLUDE_MEDIA=0
RESTORE_ENV=0
YES=0
VERBOSE=0
CONFIRMATION=""
TEMP_STAGE=""

on_error() {
  local line="$1"
  local command="$2"
  printf 'ERROR: %s failed at line %s while running: %s\n' "$SCRIPT_NAME" "$line" "$command" >&2
  if [[ -n "${TEMP_STAGE:-}" ]]; then
    printf 'Temporary validation directory retained only until process exit.\n' >&2
  fi
}
trap 'on_error "$LINENO" "$BASH_COMMAND"' ERR

usage() {
  cat <<'EOF'
Usage:
  scripts/restore.sh --install-dir DIR --backup-file FILE --dry-run [options]
  scripts/restore.sh --install-dir DIR --backup-file FILE --confirm RESTORE_QASEDAK_BACKUP_<id> [options]

Options:
  --install-dir DIR       Installed Qasedak directory.
  --backup-file FILE      Private backup archive to validate/restore.
  --backup-archive FILE   Legacy alias for --backup-file.
  --restore-job-id ID     Admin RestoreJob id, used for confirmation text and audit context.
  --dry-run               Validate package and print restore plan; no mutation.
  --include-media         Restore media files if a future apply run is enabled.
  --restore-env           Restore env file if a future apply run is enabled.
  --confirm TEXT          Required for any non-dry-run operation.
  --yes                   Accept ordinary prompts. Does not bypass --confirm.
  --verbose               Print extra non-secret validation detail.
  -h, --help              Show this help.

P1 restore apply is command-based and guarded. Dry-run validation is fully
implemented. Destructive apply refuses unless future explicit implementation
is enabled; it never claims success without restoring.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

log() {
  printf '%s\n' "$*"
}

parse_args() {
  while (($#)); do
    case "$1" in
      --install-dir)
        shift
        [[ $# -gt 0 ]] || die "--install-dir requires a value."
        INSTALL_DIR="$1"
        ;;
      --backup-file|--backup-archive)
        local opt="$1"
        shift
        [[ $# -gt 0 ]] || die "$opt requires a value."
        BACKUP_FILE="$1"
        ;;
      --restore-job-id)
        shift
        [[ $# -gt 0 ]] || die "--restore-job-id requires a value."
        RESTORE_JOB_ID="$1"
        ;;
      --dry-run)
        DRY_RUN=1
        ;;
      --include-media)
        INCLUDE_MEDIA=1
        ;;
      --restore-env)
        RESTORE_ENV=1
        ;;
      --confirm)
        shift
        [[ $# -gt 0 ]] || die "--confirm requires a value."
        CONFIRMATION="$1"
        ;;
      --yes)
        YES=1
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

target_engine() {
  local engine
  engine="$(env_value "$INSTALL_DIR/.env" DATABASE_ENGINE)"
  engine="${engine:-sqlite}"
  engine="${engine,,}"
  case "$engine" in
    postgres|postgresql) printf 'postgres' ;;
    sqlite|sqlite3) printf 'sqlite' ;;
    *) printf 'invalid:%s' "$engine" ;;
  esac
}

validate_confirmation() {
  (( DRY_RUN )) && return 0
  [[ -n "$CONFIRMATION" ]] || die "--confirm is required for non-dry-run restore."
  if [[ -n "$RESTORE_JOB_ID" ]]; then
    local expected="RESTORE_QASEDAK_BACKUP_${RESTORE_JOB_ID}"
    [[ "$CONFIRMATION" == "$expected" ]] || die "Confirmation phrase does not match $expected."
  elif [[ "$CONFIRMATION" != RESTORE_QASEDAK_BACKUP_* ]]; then
    die "Confirmation phrase must start with RESTORE_QASEDAK_BACKUP_."
  fi
}

validate_archive() {
  local target="$1"
  local expected_engine="$2"
  python3 - "$BACKUP_FILE" "$target" "$expected_engine" "$VERBOSE" <<'PY'
import hashlib
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

archive = Path(sys.argv[1])
target_engine = sys.argv[3]
verbose = sys.argv[4] == "1"
max_archive = int(os.environ.get("QASEDAK_BACKUP_MAX_UPLOAD_SIZE", str(512 * 1024 * 1024)))
max_extract = int(os.environ.get("QASEDAK_BACKUP_MAX_EXTRACTED_SIZE", str(2 * 1024 * 1024 * 1024)))

def fail(message):
    print(f"validation=failed")
    print(f"error={message}")
    raise SystemExit(1)

def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def unsafe_name(name):
    path = PurePosixPath(str(name))
    return path.is_absolute() or any(part in {"..", ""} for part in path.parts)

def payload_root(root):
    if (root / "manifest.json").exists():
        return root
    for child in root.iterdir():
        if child.is_dir() and (child / "manifest.json").exists():
            return child
    fail("manifest.json missing")

def read_checksums(root, manifest):
    checksum_path = root / "checksums.sha256"
    if checksum_path.exists():
        checksums = {}
        for line in checksum_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                fail("invalid checksums.sha256 line")
            rel = parts[1].strip().lstrip("*")
            if unsafe_name(rel):
                fail("unsafe checksum path")
            checksums[rel] = parts[0]
        return checksums, "checksums.sha256"
    checksums = manifest.get("checksums") or manifest.get("file_checksums_sha256")
    if isinstance(checksums, dict) and checksums:
        return checksums, "manifest"
    fail("checksums missing")

if not archive.exists() or not archive.is_file():
    fail("backup archive not found")
if archive.stat().st_size > max_archive:
    fail("backup archive too large")

tmp = Path(tempfile.mkdtemp(prefix="qasedak-restore-"))
try:
    total = 0
    try:
        tar = tarfile.open(archive, "r:gz")
    except tarfile.TarError as exc:
        fail(f"invalid tar archive: {exc.__class__.__name__}")
    with tar:
        members = tar.getmembers()
        for member in members:
            if unsafe_name(member.name):
                fail("archive contains path traversal")
            if member.issym() or member.islnk():
                fail("archive contains symlink or hardlink")
            if member.isdev() or stat.S_ISFIFO(member.mode or 0):
                fail("archive contains special file")
            if member.isfile():
                total += int(member.size or 0)
                if total > max_extract:
                    fail("archive extracted size too large")
        tar.extractall(tmp, members=members)
    root = payload_root(tmp)
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"invalid manifest: {exc.__class__.__name__}")
    version = str(manifest.get("qasedak_backup_version") or "")
    if version and version != "1":
        fail("unsupported backup version")
    database = manifest.get("database") or {}
    engine = (manifest.get("database_engine") or database.get("engine") or "").lower()
    if engine in {"postgresql", "postgres"}:
        engine = "postgres"
    elif engine in {"sqlite", "sqlite3"}:
        engine = "sqlite"
    else:
        fail("unsupported or missing database engine")
    checksums, source = read_checksums(root, manifest)
    for rel, expected in checksums.items():
        if unsafe_name(rel):
            fail("unsafe checksum path")
        path = root / rel
        if not path.exists() or not path.is_file() or path.is_symlink():
            fail(f"checksum target missing: {rel}")
        if sha256(path).lower() != str(expected).lower():
            fail(f"checksum mismatch: {rel}")
    if engine == "postgres":
        if not (root / "database" / "db.postgres.dump").is_file():
            fail("database/db.postgres.dump missing")
        if shutil.which("pg_restore") is None:
            fail("pg_restore missing")
    else:
        sqlite_path = root / "database" / "db.sqlite3"
        if not sqlite_path.is_file():
            fail("database/db.sqlite3 missing")
        if not sqlite_path.open("rb").read(16).startswith(b"SQLite format 3"):
            fail("database/db.sqlite3 is not SQLite")
    if target_engine.startswith("invalid:"):
        fail(f"target engine invalid: {target_engine}")
    if target_engine and target_engine != engine:
        fail(f"engine mismatch: backup={engine} target={target_engine}")
    print("validation=ok")
    print(f"engine={engine}")
    print(f"checksum_source={source}")
    print(f"verified_files={len(checksums)}")
    print(f"includes_media={str((root / 'media').exists() or bool(manifest.get('includes_media'))).lower()}")
    print(f"includes_env={str((root / 'env' / 'production.env').exists() or (root / 'runtime' / '.env').exists() or bool(manifest.get('includes_env'))).lower()}")
    print(f"backup_type={manifest.get('backup_type') or 'legacy'}")
    if verbose:
        print(f"manifest_created_at={manifest.get('created_at') or manifest.get('timestamp') or '-'}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
PY
}

print_restore_plan() {
  local engine="$1"
  log "Restore plan:"
  log "  install dir: $INSTALL_DIR"
  log "  backup file: $BACKUP_FILE"
  log "  target engine: $engine"
  log "  include media: $([[ "$INCLUDE_MEDIA" == "1" ]] && printf yes || printf no)"
  log "  restore env: $([[ "$RESTORE_ENV" == "1" ]] && printf yes || printf no)"
  log "  pre-restore backup: required before any apply"
  log "  writers to stop: telegram polling, vpn-store timers, gunicorn"
  log "  apply strategy: command-based; Admin web request does not restore"
}

main() {
  trap '[[ -z "${TEMP_STAGE:-}" ]] || rm -rf "$TEMP_STAGE"' EXIT
  parse_args "$@"
  [[ -n "$BACKUP_FILE" ]] || die "--backup-file is required."
  [[ -d "$INSTALL_DIR" ]] || die "Install directory not found: $INSTALL_DIR"
  [[ -f "$INSTALL_DIR/.env" ]] || die ".env not found at $INSTALL_DIR/.env"
  [[ -f "$BACKUP_FILE" ]] || die "Backup archive not found: $BACKUP_FILE"

  validate_confirmation
  local engine
  engine="$(target_engine)"
  print_restore_plan "$engine"
  validate_archive "$BACKUP_FILE" "$engine"

  if (( DRY_RUN )); then
    log "DRY-RUN: no files, database, services, timers, media, or env were changed."
    return 0
  fi

  die "P1 destructive restore apply is not enabled in this script. Validation passed; create a pre-restore backup and follow docs/RESTORE.md for the controlled restore runbook."
}

main "$@"
