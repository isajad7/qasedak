#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

die() { printf 'Development deploy: %s\n' "$*" >&2; exit 1; }
revision="${1:-}"
expected_checksum="${2:-}"
archive="${3:-}"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || die 'Invalid revision.'
[[ "$expected_checksum" =~ ^[0-9a-f]{64}$ ]] || die 'Invalid archive checksum.'
[[ "$archive" =~ ^/tmp/qasedak-dev\.[a-zA-Z0-9]{8}/release\.tar\.gz$ ]] || die 'Invalid archive path.'
[[ -f "$archive" ]] || die 'Release archive is missing.'
[[ "$(sha256sum "$archive" | cut -d' ' -f1)" == "$expected_checksum" ]] || die 'Release checksum mismatch.'

config=/etc/qasedak/dev-deploy.conf
[[ -r "$config" ]] || die 'Missing reviewed /etc/qasedak/dev-deploy.conf.'
# This root-owned configuration contains paths and names only; runtime secrets stay in Compose env_file.
# shellcheck source=/dev/null
source "$config"
: "${QASEDAK_DEV_ROOT:?Set QASEDAK_DEV_ROOT}"
: "${QASEDAK_DEV_COMPOSE_FILE:?Set QASEDAK_DEV_COMPOSE_FILE}"
: "${QASEDAK_DEV_PROJECT:?Set QASEDAK_DEV_PROJECT}"
: "${QASEDAK_DEV_SERVICE:?Set QASEDAK_DEV_SERVICE}"
: "${QASEDAK_DEV_TENANT_ID:?Set QASEDAK_DEV_TENANT_ID}"
: "${QASEDAK_DEV_HEALTH_URL:?Set QASEDAK_DEV_HEALTH_URL}"
: "${QASEDAK_DEV_BACKUP_HOOK:?Set QASEDAK_DEV_BACKUP_HOOK}"

[[ "$QASEDAK_DEV_ROOT" == /* && "$QASEDAK_DEV_ROOT" != / ]] || die 'Invalid release root.'
[[ -d "$QASEDAK_DEV_ROOT" && ! -L "$QASEDAK_DEV_ROOT" ]] || die 'Release root must exist and not be a symlink.'
[[ "$(cat "$QASEDAK_DEV_ROOT/.qasedak-development-target" 2>/dev/null)" == 'isajad7/qasedak:development' ]] || die 'Development target marker is missing.'
[[ "$QASEDAK_DEV_COMPOSE_FILE" == /* && -f "$QASEDAK_DEV_COMPOSE_FILE" ]] || die 'Reviewed Compose file is missing.'
[[ "$QASEDAK_DEV_PROJECT" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || die 'Invalid Compose project.'
[[ "$QASEDAK_DEV_SERVICE" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] || die 'Invalid Compose service.'
[[ "$QASEDAK_DEV_TENANT_ID" =~ ^[a-zA-Z0-9_-]+$ ]] || die 'Invalid tenant ID.'
[[ "$QASEDAK_DEV_HEALTH_URL" =~ ^http://127\.0\.0\.1:[0-9]{2,5}/health/$ ]] || die 'Health URL must target a loopback port.'
[[ "$QASEDAK_DEV_BACKUP_HOOK" == /* && -x "$QASEDAK_DEV_BACKUP_HOOK" ]] || die 'Executable backup hook is required.'
command -v docker >/dev/null || die 'Docker is required.'
command -v curl >/dev/null || die 'curl is required.'
command -v flock >/dev/null || die 'flock is required.'
command -v python3 >/dev/null || die 'python3 is required.'
docker compose version >/dev/null || die 'Docker Compose v2 is required.'

exec 9>"$QASEDAK_DEV_ROOT/.deploy.lock"
flock -n 9 || die 'Another development deployment is in progress.'
image="qasedak-development:$revision"
compose=(docker compose -p "$QASEDAK_DEV_PROJECT" -f "$QASEDAK_DEV_COMPOSE_FILE")
export QASEDAK_IMAGE_TAG="$image"
bootstrap_refresh=0
if "${compose[@]}" config --format json | python3 -c '
import json, sys
config = json.load(sys.stdin)
service = config["services"].get(sys.argv[1])
if service is None or service.get("image") != sys.argv[2] or service.get("build"):
    sys.exit("Reviewed app must use QASEDAK_IMAGE_TAG without a build block")
sys.exit(10 if "subscription-refresh" not in config["services"] else 0)
' "$QASEDAK_DEV_SERVICE" "$image"; then
    :
else
    status=$?
    [[ "$status" == 10 ]] || die 'Reviewed Compose app service did not match the image.'
    [[ "$QASEDAK_DEV_COMPOSE_FILE" =~ ^/[a-zA-Z0-9_./-]+$ ]] || die 'Compose path cannot be used in the refresh override.'
    override="$QASEDAK_DEV_ROOT/.subscription-refresh.compose.yaml"
    tmp_override="$(mktemp "$QASEDAK_DEV_ROOT/.subscription-refresh.XXXXXXXX.yaml")"
    cat > "$tmp_override" <<EOF
services:
  subscription-refresh:
    extends:
      file: "$QASEDAK_DEV_COMPOSE_FILE"
      service: "$QASEDAK_DEV_SERVICE"
    image: \${QASEDAK_IMAGE_TAG:?Set the exact revision image tag}
    build: !reset null
    ports: !reset []
    container_name: !reset null
    healthcheck: !reset null
    entrypoint: ["/bin/sh", "-c"]
    command: >-
      while true; do
        python manage.py refresh_external_subscription_feeds;
        sleep 300;
      done
    restart: unless-stopped
EOF
    if [[ -e "$override" || -L "$override" ]]; then
        [[ -f "$override" && ! -L "$override" ]] || die 'Subscription-refresh override is not a regular file.'
        cmp -s "$tmp_override" "$override" || die 'Existing subscription-refresh override differs from the reviewed version.'
        rm -f -- "$tmp_override"
    else
        mv -- "$tmp_override" "$override"
    fi
    bootstrap_refresh=1
    compose+=( -f "$override" )
fi
"${compose[@]}" config --format json | python3 -c '
import json, sys
config = json.load(sys.stdin)
for name in (sys.argv[1], "subscription-refresh"):
    service = config["services"].get(name)
    if service is None or service.get("image") != sys.argv[2] or service.get("build"):
        sys.exit(f"Compose service {name!r} must use QASEDAK_IMAGE_TAG without a build block")
refresh = config["services"]["subscription-refresh"]
if refresh.get("ports"):
    sys.exit("subscription-refresh must not publish host ports")
' "$QASEDAK_DEV_SERVICE" "$image" || die 'Compose services did not match the reviewed image.'

old_container="$("${compose[@]}" ps -q "$QASEDAK_DEV_SERVICE")"
[[ "$old_container" =~ ^[0-9a-f]{12,64}$ ]] || die 'Expected exactly one existing target container; the first install must be reviewed manually.'
[[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$old_container")" == "$QASEDAK_DEV_PROJECT" ]] || die 'Container project mismatch.'
[[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$old_container")" == "$QASEDAK_DEV_SERVICE" ]] || die 'Container service mismatch.'
old_image="$(docker inspect -f '{{.Config.Image}}' "$old_container")"
[[ -n "$old_image" ]] || die 'Existing image is unknown.'
old_refresh_container="$("${compose[@]}" ps -q subscription-refresh)"
if [[ -n "$old_refresh_container" ]]; then
    [[ "$old_refresh_container" =~ ^[0-9a-f]{12,64}$ ]] || die 'Invalid existing subscription-refresh container.'
    [[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$old_refresh_container")" == "$QASEDAK_DEV_PROJECT" ]] || die 'Subscription refresh container project mismatch.'
    [[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$old_refresh_container")" == 'subscription-refresh' ]] || die 'Subscription refresh container service mismatch.'
    old_refresh_image="$(docker inspect -f '{{.Config.Image}}' "$old_refresh_container")"
    [[ "$old_refresh_image" == "$old_image" ]] || die 'App and subscription-refresh containers are on different revisions.'
else
    (( bootstrap_refresh )) || die 'Expected an existing subscription-refresh container.'
fi
docker inspect -f '{{json .NetworkSettings.Ports}}' "$old_container" | python3 -c '
import json, sys
from urllib.parse import urlsplit
port = urlsplit(sys.argv[1]).port
bindings = json.load(sys.stdin) or {}
if not any(item.get("HostIp") == "127.0.0.1" and item.get("HostPort") == str(port)
           for values in bindings.values() for item in values or []):
    sys.exit("Selected container does not own the configured loopback health port")
' "$QASEDAK_DEV_HEALTH_URL" || die 'Health port does not belong to the selected container.'

health_ok() {
    local response
    response="$(mktemp "$QASEDAK_DEV_ROOT/.health.XXXXXXXX")"
    if ! curl --noproxy '*' --silent --show-error --fail --max-time 5 \
        -o "$response" "$QASEDAK_DEV_HEALTH_URL" 2>/dev/null; then
        rm -f -- "$response"
        return 1
    fi
    python3 - "$response" "$QASEDAK_DEV_TENANT_ID" <<'PY'
import json, sys
try:
    payload = json.load(open(sys.argv[1], encoding="utf-8"))
    valid = payload.get("service") == "alive" and payload.get("tenant_id") == sys.argv[2] and payload.get("database", {}).get("reachable") is True
except (ValueError, OSError, TypeError):
    valid = False
sys.exit(0 if valid else 1)
PY
    local result=$?
    rm -f -- "$response"
    return "$result"
}

if [[ "$old_image" == "$image" && -n "$old_refresh_container" ]]; then
    health_ok || die 'Existing revision is not healthy.'
    [[ "$(docker inspect -f '{{.State.Running}}' "$old_refresh_container")" == true ]] || die 'Subscription refresh container is not running.'
    printf 'Development revision %s already healthy.\n' "$revision"
    exit 0
fi

# Fail if the current loopback endpoint is not this tenant before changing any container.
health_ok || die 'Current target health/tenant check failed.'

mkdir -p "$QASEDAK_DEV_ROOT/backups" "$QASEDAK_DEV_ROOT/releases"
backup="$QASEDAK_DEV_ROOT/backups/predeploy-$revision-$(date -u +%Y%m%dT%H%M%SZ)-$$.dump"
if ! "$QASEDAK_DEV_BACKUP_HOOK" "$backup" >/dev/null 2>&1; then
    die 'Pre-deploy backup hook failed.'
fi
[[ -s "$backup" ]] || die 'Backup hook returned without a nonempty backup.'
chmod 600 "$backup"
printf 'Pre-deploy backup completed.\n'

release="$QASEDAK_DEV_ROOT/releases/$revision"
if [[ -e "$release" ]]; then
    [[ -f "$release/.qasedak-revision" && "$(cat "$release/.qasedak-revision")" == "$revision" ]] || die 'Existing release directory does not match the revision.'
else
    tmp_release="$(mktemp -d "$QASEDAK_DEV_ROOT/releases/.incoming.XXXXXXXX")"
    python3 - "$archive" <<'PY'
import posixpath, sys, tarfile
with tarfile.open(sys.argv[1], "r:gz") as release:
    for member in release.getmembers():
        path = posixpath.normpath(member.name)
        if member.name.startswith("/") or path in (".", "..") or path.startswith("../"):
            sys.exit("Archive path escaped the release directory")
        if member.issym():
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), member.linkname))
            if member.linkname.startswith("/") or target == ".." or target.startswith("../"):
                sys.exit("Archive symlink escaped the release directory")
        elif not (member.isfile() or member.isdir()):
            sys.exit("Unsupported archive entry type")
PY
    tar -xzf "$archive" --no-same-owner --no-same-permissions -C "$tmp_release"
    [[ -f "$tmp_release/manage.py" && -f "$tmp_release/Dockerfile" ]] || die 'Release archive is not Qasedak source.'
    printf '%s\n' "$revision" > "$tmp_release/.qasedak-revision"
    mv -- "$tmp_release" "$release"
fi

docker build -t "$image" "$release" >/dev/null
rollback() {
    if [[ -n "$old_refresh_container" ]]; then
        QASEDAK_IMAGE_TAG="$old_image" "${compose[@]}" up -d --no-deps --no-build "$QASEDAK_DEV_SERVICE" subscription-refresh >/dev/null || true
    else
        QASEDAK_IMAGE_TAG="$old_image" "${compose[@]}" up -d --no-deps --no-build "$QASEDAK_DEV_SERVICE" >/dev/null || true
        "${compose[@]}" rm -sf subscription-refresh >/dev/null || true
    fi
}
if ! "${compose[@]}" up -d --no-deps --no-build "$QASEDAK_DEV_SERVICE" subscription-refresh >/dev/null; then
    rollback
    die 'Compose update failed; attempted to restore the previous image. Inspect migrations before relying on rollback.'
fi
healthy=0
for attempt in {1..24}; do
    current="$("${compose[@]}" ps -q "$QASEDAK_DEV_SERVICE")"
    current_refresh="$("${compose[@]}" ps -q subscription-refresh)"
    if [[ -n "$current" && "$(docker inspect -f '{{.Config.Image}}' "$current")" == "$image" \
        && -n "$current_refresh" && "$(docker inspect -f '{{.Config.Image}}' "$current_refresh")" == "$image" \
        && "$(docker inspect -f '{{.State.Running}}' "$current_refresh")" == true ]] && health_ok; then
        healthy=1
        break
    fi
    sleep 5
done
if (( ! healthy )); then
    rollback
    die 'New image did not pass the matching tenant/database health check; previous image restart attempted. Database migrations require manual review.'
fi
printf 'Development deployed revision %s to the reviewed Compose service.\n' "$revision"
