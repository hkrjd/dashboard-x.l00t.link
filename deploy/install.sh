#!/bin/bash
# One-time (and on every change to this folder) install of the hub's pinned
# deploy files. Run as ROOT on the server, from a checkout of this repository:
#
#   sudo bash deploy/install.sh
#
# It installs deploy.sh, Dockerfile and docker-compose.yml root-owned into
# /opt/dashboard-x-deploy, makes the state and data folders, and creates the
# internal hub_net network if it is missing. It does NOT touch authorized_keys,
# .env files, DNS or the reverse proxy -- see deploy/README.md for those.
set -Eeuo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root." >&2
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
PIN_DIR=/opt/dashboard-x-deploy
STATE_DIR=/var/lib/dashboard-x-deploy
BASE=/home/deploy/Bots_web_dashboard
DEPLOY_USER=deploy
HUB_UID=10001

install -d -o root -g root -m 0755 "$PIN_DIR"
install -o root -g root -m 0755 "$HERE/deploy.sh" "$PIN_DIR/deploy.sh"
install -o root -g root -m 0644 "$HERE/Dockerfile" "$PIN_DIR/Dockerfile"
install -o root -g root -m 0644 "$HERE/docker-compose.yml" "$PIN_DIR/docker-compose.yml"

install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0750 "$STATE_DIR"

install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0755 "$BASE" "$BASE/shared" "$BASE/dealops"
# Only the hub (uid 10001) writes here.
install -d -o "$HUB_UID" -g "$HUB_UID" -m 0700 "$BASE/shared/data"
if [ ! -e "$BASE/shared/.env" ]; then
  install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0600 /dev/null "$BASE/shared/.env"
  echo "Created an empty $BASE/shared/.env -- fill it from .env.example."
fi

if ! docker network inspect hub_net >/dev/null 2>&1; then
  docker network create --internal hub_net
  echo "Created the internal network hub_net."
fi

echo
echo "Installed. Compare with the repository:"
sha256sum "$PIN_DIR/deploy.sh" "$PIN_DIR/Dockerfile" "$PIN_DIR/docker-compose.yml"
sha256sum "$HERE/deploy.sh" "$HERE/Dockerfile" "$HERE/docker-compose.yml"
