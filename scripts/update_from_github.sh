#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${QASEDAK_REPO_URL:-https://github.com/isajad7/qasedak.git}"
REPO_REF="${QASEDAK_REF:-main}"
INSTALL_DIR="${QASEDAK_INSTALL_DIR:-/opt/qasedak}"
TMPDIR=""

log() {
  printf '%s\n' "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

cleanup_tmpdir() {
  if [[ -n "${TMPDIR:-}" ]]; then
    rm -rf "$TMPDIR"
  fi
}

ensure_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    return 0
  fi
  if [[ -r "$0" && "$0" != "bash" && "$0" != "-bash" ]]; then
    command -v sudo >/dev/null 2>&1 || die "Run as root or install sudo."
    exec sudo -E bash "$0" "$@"
  fi
  die "Run with sudo: curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/update_from_github.sh | sudo bash"
}

ensure_git() {
  if command -v git >/dev/null 2>&1; then
    return 0
  fi
  command -v apt-get >/dev/null 2>&1 || die "git is required and apt-get is not available."
  apt-get update
  apt-get install -y git ca-certificates
}

run_child_script() {
  local script_path="$1"
  shift
  if [[ -t 0 ]]; then
    bash "$script_path" "$@"
  elif [[ -r /dev/tty ]]; then
    bash "$script_path" "$@" < /dev/tty
  else
    bash "$script_path" "$@"
  fi
}

main() {
  ensure_root "$@"
  ensure_git

  TMPDIR="$(mktemp -d)"
  trap cleanup_tmpdir EXIT

  log "Downloading latest Qasedak source..."
  git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$TMPDIR/qasedak"
  run_child_script "$TMPDIR/qasedak/scripts/update.sh" \
    --install-dir "$INSTALL_DIR" \
    --source-dir "$TMPDIR/qasedak" \
    --yes \
    --restart \
    "$@"
}

main "$@"
