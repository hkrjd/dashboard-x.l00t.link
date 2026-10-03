#!/bin/bash
# One-time, as ROOT, from a checkout of this repository:
#
#   bash deploy/setup-secrets.sh
#
# Makes the hub's secret key and the DealOps API token, writes them straight
# into shared/.env and DealOps' .env, and prints neither. Also installs
# dealops/config.toml. Refuses to run a second time over existing secrets.
#
# DealOps reads its .env when its container is recreated (its next deploy).
set -Eeuo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root." >&2
  exit 1
fi

HERE="$(cd "$(dirname "$0")/.." && pwd)"
BASE=/home/deploy/Bots_web_dashboard
HUB_ENV="$BASE/shared/.env"
BOT_ENV=/home/deploy/dealops/.env

if [ ! -f "$BOT_ENV" ]; then
  echo "$BOT_ENV not found; nothing changed." >&2
  exit 1
fi
if grep -q '^HUB_SECRET_KEY=.' "$HUB_ENV" 2>/dev/null; then
  echo "$HUB_ENV already has a secret key; nothing changed." >&2
  exit 1
fi
if grep -q '^HUB_API_TOKEN=' "$BOT_ENV"; then
  echo "$BOT_ENV already has HUB_API_TOKEN; nothing changed." >&2
  exit 1
fi

secret="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(40))')"

# A copy of DealOps' .env before it is touched (same owner and mode).
cp -p "$BOT_ENV" "$BOT_ENV.bak-$(date +%Y%m%d-%H%M%S)"

umask 077
cat > "$HUB_ENV" <<EOF
HUB_SECRET_KEY=$secret
HUB_PUBLIC_ORIGIN=https://dashboard-x.l00t.link
HUB_BOT_TOKEN_DEALOPS=$token
HUB_ALERT_BOT=dealops
HUB_TIMEZONE=Asia/Kolkata
EOF
chown deploy:deploy "$HUB_ENV"
chmod 600 "$HUB_ENV"

# Append on a line of its own, keeping the file's owner and mode.
if [ -n "$(tail -c1 "$BOT_ENV")" ]; then
  echo >> "$BOT_ENV"
fi
printf 'HUB_API_TOKEN=%s\n' "$token" >> "$BOT_ENV"

install -o deploy -g deploy -m 0644 "$HERE/bots/dealops/config.example.toml" "$BASE/dealops/config.toml"

unset secret token

hub_token="$(grep '^HUB_BOT_TOKEN_DEALOPS=' "$HUB_ENV" | cut -d= -f2-)"
bot_token="$(grep '^HUB_API_TOKEN=' "$BOT_ENV" | cut -d= -f2-)"
if [ -n "$hub_token" ] && [ "$hub_token" = "$bot_token" ]; then
  echo "Tokens match (length ${#hub_token})."
else
  echo "Tokens do NOT match -- stop and report." >&2
  exit 1
fi
unset hub_token bot_token
stat -c '%a %U %n' "$HUB_ENV" "$BOT_ENV" "$BASE/dealops/config.toml"
echo "Secrets written. Nothing secret was printed."
