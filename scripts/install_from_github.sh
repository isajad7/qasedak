#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${QASEDAK_REPO_URL:-https://github.com/isajad7/qasedak.git}"
REPO_REF="${QASEDAK_REF:-main}"

log() {
  printf '%s\n' "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

ensure_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    return 0
  fi
  if [[ -r "$0" && "$0" != "bash" && "$0" != "-bash" ]]; then
    command -v sudo >/dev/null 2>&1 || die "Run as root or install sudo."
    exec sudo -E bash "$0" "$@"
  fi
  die "Run with sudo: curl -fsSL https://raw.githubusercontent.com/isajad7/qasedak/main/scripts/install_from_github.sh | sudo bash"
}

ensure_git() {
  if command -v git >/dev/null 2>&1; then
    return 0
  fi
  command -v apt-get >/dev/null 2>&1 || die "git is required and apt-get is not available."
  apt-get update
  apt-get install -y git ca-certificates
}

main() {
  ensure_root "$@"
  ensure_git

  local tmpdir=""
  tmpdir="$(mktemp -d)"
  trap 'rm -rf "$tmpdir"' EXIT

  log "Downloading Qasedak installer..."
  git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$tmpdir/qasedak"
  bash "$tmpdir/qasedak/scripts/install.sh" "$@"
}

main "$@"
