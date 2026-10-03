#!/bin/bash
# Forced command for the hub's CI deploy key ("ci-dashboard-x").
#
# authorized_keys:
#   command="/opt/dashboard-x-deploy/deploy.sh",restrict ssh-ed25519 AAAA... ci-dashboard-x
#
# Whatever command the client asks for, sshd runs THIS instead, and this never
# reads SSH_ORIGINAL_COMMAND. The only input is a git bundle on stdin; the only
# effect is building that commit with the pinned Dockerfile and running it with
# the pinned compose file, both root-owned next to this script. No sudo.
#
# A copy of DealOps' ops/dealops-deploy/deploy.sh (2026-09-30) with the names
# changed; keep the two in step.
#
# Installed root:root 0755 at /opt/dashboard-x-deploy/deploy.sh.
# Source of truth: deploy/ in the dashboard-x repository.
set -Eeuo pipefail
# 022, not 077: the image runs the code as uid 10001, which must be able to read
# it (FastForwarderXBot's first deploy through this pattern crashed under 077).
# mktemp -d still creates the work area 0700 whatever the umask.
umask 022

PIN_DIR=/opt/dashboard-x-deploy
STATE_DIR=/var/lib/dashboard-x-deploy
IMAGE=dashboard-x
SERVICE=hub
CONTAINER=dashboard-x-hub
MAX_BUNDLE_BYTES=$((20 * 1024 * 1024))
# start_period 20s + three 30s retries, plus margin.
HEALTH_TIMEOUT=120
KEEP_BUILDS=5

compose=(docker compose -f "$PIN_DIR/docker-compose.yml")

if [ "$(id -u)" -eq 0 ]; then
  # Run as root, the lock and VERSION files would end up root-owned and every
  # later deploy by the deploy user would fail on them.
  echo "❌ Refusing to run as root; this runs as the deploy user."
  exit 1
fi

# Printed so a CI log shows which version of this script ran: compare it with
# `sha256sum deploy/deploy.sh` in the repository.
echo "[deploy] script $(sha256sum "$0" | cut -c1-12)"

exec 9>"$STATE_DIR/lock"
if ! flock -n 9; then
  echo "❌ Another deploy is already running."
  exit 1
fi

WORK="$(mktemp -d "$STATE_DIR/run.XXXXXX")"
trap 'rm -rf -- "$WORK"' EXIT

# ---------------------------------------------------------------- receive ----
echo "[receive] reading bundle from stdin"
head -c "$((MAX_BUNDLE_BYTES + 1))" > "$WORK/in.bundle"
size="$(wc -c < "$WORK/in.bundle")"
if [ "$size" -eq 0 ]; then
  echo "[receive] ERROR: empty bundle"
  exit 1
fi
if [ "$size" -gt "$MAX_BUNDLE_BYTES" ]; then
  echo "[receive] ERROR: bundle larger than $MAX_BUNDLE_BYTES bytes"
  exit 1
fi

git init -q "$WORK/repo"
if ! git -C "$WORK/repo" bundle verify -q "$WORK/in.bundle" >/dev/null 2>&1; then
  echo "[receive] ERROR: not a valid, self-contained git bundle"
  exit 1
fi
# fsck refuses malformed objects -- ".." or ".git" tree entries, bad modes --
# before anything is written out of them.
git -C "$WORK/repo" -c fetch.fsckObjects=true fetch -q "$WORK/in.bundle" "HEAD:refs/deploy"
SHA="$(git -C "$WORK/repo" rev-parse --verify 'refs/deploy^{commit}')"
if ! [[ "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[receive] ERROR: unexpected commit id"
  exit 1
fi
echo "📌 Deploying commit ${SHA:0:7}"

mkdir "$WORK/tree"
git -C "$WORK/repo" archive "$SHA" | tar -x -C "$WORK/tree"

# ------------------------------------------------------------------ build ----
echo "🔨 Building $IMAGE:$SHA with the pinned Dockerfile"
# The hub keeps running on the previous image while this builds; a failed build
# exits here and never touches the container.
docker build -f "$PIN_DIR/Dockerfile" -t "$IMAGE:$SHA" "$WORK/tree"

# The last build that passed its health check -- the rollback target. On the
# very first deploy there is none.
PREV="$(cat "$STATE_DIR/VERSION" 2>/dev/null || true)"
if ! [[ "$PREV" =~ ^[0-9a-f]{40}$ ]]; then
  PREV=current
fi

rollback() {
  if ! docker image inspect "$IMAGE:$PREV" >/dev/null 2>&1; then
    echo "❌ No earlier build ($IMAGE:$PREV) to roll back to -- the dashboard may be DOWN."
    return
  fi
  echo "↩️  Rolling back to $IMAGE:$PREV"
  if ! APP_VERSION="$PREV" "${compose[@]}" up -d --force-recreate --no-build "$SERVICE"; then
    echo "❌ Rollback failed too -- the dashboard may be DOWN. Investigate on the server."
    return
  fi
  # Running without restarts is what a rollback can promise.
  sleep 15
  docker inspect --type container \
    -f '   after rollback: {{.State.Status}}, restarts={{.RestartCount}}, image={{.Config.Image}}' \
    "$CONTAINER" || true
}

# ------------------------------------------------------------ run + gate ----
echo "🔄 Starting ${SHA:0:7} (rollback target: $PREV)"
if ! APP_VERSION="$SHA" "${compose[@]}" up -d --force-recreate --no-build "$SERVICE"; then
  echo "❌ Could not start ${SHA:0:7}"
  rollback
  exit 1
fi

echo "🩺 Waiting up to ${HEALTH_TIMEOUT}s for the health check"
status=starting
deadline=$((SECONDS + HEALTH_TIMEOUT))
while [ "$SECONDS" -lt "$deadline" ]; do
  status="$(docker inspect --type container \
    -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
    "$CONTAINER" 2>/dev/null || echo missing)"
  case "$status" in
    healthy|unhealthy|none|missing) break ;;
  esac
  sleep 5
done
restarts="$(docker inspect --type container -f '{{.RestartCount}}' "$CONTAINER" 2>/dev/null || echo '?')"
running_image="$(docker inspect --type container -f '{{.Config.Image}}' "$CONTAINER" 2>/dev/null || echo '?')"

if [ "$status" != healthy ] || [ "$restarts" != 0 ] || [ "$running_image" != "$IMAGE:$SHA" ]; then
  echo "❌ ${SHA:0:7} is not healthy (health=$status, restarts=$restarts, image=$running_image). Recent logs:"
  docker logs --tail 50 "$CONTAINER" 2>&1 || true
  rollback
  exit 1
fi

# Only now: a build that never became healthy must not become :current or the
# rollback target.
docker tag "$IMAGE:$SHA" "$IMAGE:current"
echo "$SHA" > "$STATE_DIR/VERSION"

echo "🧹 Keeping the last $KEEP_BUILDS builds"
# Only the hub's commit-tagged images -- never `docker image prune`, which on
# this shared server would touch every other bot's images. `|| true`: grep
# exits 1 on no match, which under pipefail would fail a finished deploy.
{ docker image ls "$IMAGE" --format '{{.Tag}}' | grep -E '^[0-9a-f]{40}$' || true; } \
  | tail -n "+$((KEEP_BUILDS + 1))" \
  | while read -r tag; do
      if [ "$tag" != "$SHA" ] && [ "$tag" != "$PREV" ]; then
        docker image rm "$IMAGE:$tag" >/dev/null 2>&1 || true
      fi
    done

echo "✨ Deployed ${SHA:0:7}"
