#!/usr/bin/env bash
# Show stack status without systemd/systemctl.
set -euo pipefail

ENV_FILE="${ENV_FILE:-/etc/opencode-stack.env}"
SERVE_PORT="${SERVE_PORT:-4096}"
PRIMARY_PORT="${PRIMARY_PORT:-7015}"
TEST_PORT="${TEST_PORT:-4097}"

log() { printf '%s\n' "$*"; }

if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a; . "$ENV_FILE"; set +a
fi

OPENCODE_USERNAME="${OPENCODE_USERNAME:-opencode}"
OPENCODE_PASSWORD="${OPENCODE_PASSWORD:-${OPENCODE_SERVER_PASSWORD:-}}"
PROXY_API_KEY="${PROXY_API_KEY:-}"

log "== listening ports =="
ss -tlnp 2>/dev/null | grep -E ":($SERVE_PORT|$PRIMARY_PORT|$TEST_PORT)\b" || \
  log "no stack ports listening"

log
log "== opencode serve :$SERVE_PORT =="
curl -sS -m 3 -u "$OPENCODE_USERNAME:${OPENCODE_PASSWORD:-}" \
  "http://127.0.0.1:$SERVE_PORT/global/health" || true
echo

if [ -n "$PROXY_API_KEY" ]; then
  AUTH=(-H "Authorization: Bearer $PROXY_API_KEY")
else
  AUTH=()
fi

for port in "$PRIMARY_PORT" "$TEST_PORT"; do
  log "== proxy :$port =="
  curl -sS -m 3 "http://127.0.0.1:$port/health" || true
  echo
  log "models:"
  curl -sS -m 15 "${AUTH[@]}" "http://127.0.0.1:$port/v1/models" \
    | jq -r '.data[].id' 2>/dev/null || true
  echo
done
