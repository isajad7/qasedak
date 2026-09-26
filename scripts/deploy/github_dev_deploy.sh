#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() { printf 'Development deploy: %s\n' "$*" >&2; exit 1; }

: "${RUNNER_TEMP:?GitHub runner is required}"
: "${GITHUB_SHA:?Commit SHA is required}"
: "${DEV_SSH_HOST:?Set DEV_SSH_HOST in the development environment}"
: "${DEV_SSH_USER:?Set DEV_SSH_USER in the development environment}"
: "${DEV_SSH_PRIVATE_KEY:?Set DEV_SSH_PRIVATE_KEY secret}"
: "${DEV_SSH_KNOWN_HOSTS:?Set DEV_SSH_KNOWN_HOSTS secret}"

[[ "$GITHUB_SHA" =~ ^[0-9a-f]{40}$ ]] || die 'Invalid commit SHA.'
[[ "$DEV_SSH_HOST" =~ ^[a-zA-Z0-9.-]+$ ]] || die 'Invalid SSH host.'
[[ "$DEV_SSH_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || die 'Invalid SSH user.'
port="${DEV_SSH_PORT:-22}"
[[ "$port" =~ ^[0-9]{1,5}$ ]] && (( 10#$port > 0 && 10#$port <= 65535 )) || die 'Invalid SSH port.'

ssh_dir="$RUNNER_TEMP/qasedak-deploy-ssh"
install -m 700 -d "$ssh_dir"
printf '%s\n' "$DEV_SSH_PRIVATE_KEY" > "$ssh_dir/key"
printf '%s\n' "$DEV_SSH_KNOWN_HOSTS" > "$ssh_dir/known_hosts"
chmod 600 "$ssh_dir/key" "$ssh_dir/known_hosts"

ssh_opts=(-i "$ssh_dir/key" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes
          -o UserKnownHostsFile="$ssh_dir/known_hosts" -o ConnectTimeout=15 -p "$port")
target="$DEV_SSH_USER@$DEV_SSH_HOST"
archive="$RUNNER_TEMP/qasedak-$GITHUB_SHA.tar.gz"
stage=''
cleanup() {
    if [[ -n "$stage" ]]; then
        ssh "${ssh_opts[@]}" "$target" "rm -f -- '$stage/release.tar.gz'; rmdir -- '$stage'" >/dev/null 2>&1 || true
    fi
    rm -f -- "$archive" "$ssh_dir/key" "$ssh_dir/known_hosts"
    rmdir -- "$ssh_dir" 2>/dev/null || true
}
trap cleanup EXIT

# Archive precisely the checked-out commit; no runtime data or local untracked files.
[[ "$(git rev-parse HEAD)" == "$GITHUB_SHA" ]] || die 'Checkout does not match workflow commit.'
git archive --format=tar "$GITHUB_SHA" | gzip -n > "$archive"
checksum="$(sha256sum "$archive" | cut -d' ' -f1)"

remote_stage="$(ssh "${ssh_opts[@]}" "$target" 'mktemp -d /tmp/qasedak-dev.XXXXXXXX')"
[[ "$remote_stage" =~ ^/tmp/qasedak-dev\.[a-zA-Z0-9]{8}$ ]] || die 'Unexpected server staging path.'
stage="$remote_stage"
scp -i "$ssh_dir/key" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile="$ssh_dir/known_hosts" -P "$port" \
    "$archive" "$target:$stage/release.tar.gz"

# Only the server's explicitly configured Compose project and service may be updated.
ssh "${ssh_opts[@]}" "$target" \
    "bash -s -- '$GITHUB_SHA' '$checksum' '$stage/release.tar.gz'" \
    < scripts/deploy/compose_dev_deploy.sh
