#!/usr/bin/env bash
# Cron watchdog. Runs every minute; starts the stack only if a port is down.
# No systemd, no systemctl.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$SCRIPT_DIR}"
ENV_FILE="${ENV_FILE:-/etc/opencode-stack.env}"
STATE_DIR="${STATE_DIR:-/var/lib/opencode-proxy}"
LOG_DIR="${LOG_DIR:-/var/log/opencode-proxy}"
SERVICE_USER="${SERVICE_USER:-opencode}"
SERVICE_HOME="${SERVICE_HOME:-/home/$SERVICE_USER}"
SERVE_PORT="${SERVE_PORT:-4096}"
PRIMARY_PORT="${PRIMARY_PORT:-7015}"
TEST_PORT="${TEST_PORT:-4097}"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }

[ -f "$ENV_FILE" ] || { log "env nei: $ENV_FILE"; exit 1; }
# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

OPENCODE_USERNAME="${OPENCODE_USERNAME:-opencode}"
OPENCODE_PASSWORD="${OPENCODE_PASSWORD:-${OPENCODE_SERVER_PASSWORD:-}}"

mkdir -p "$STATE_DIR" "$LOG_DIR"
exec 8>"$STATE_DIR/watchdog.lock"
flock -n 8 || exit 0

serve_ok() {
  curl -fsS -m 2 -u "$OPENCODE_USERNAME:${OPENCODE_PASSWORD:-}" \
    "http://127.0.0.1:$SERVE_PORT/global/health" >/dev/null 2>&1
}

proxy_ok() {
  curl -fsS -m 2 "http://127.0.0.1:$1/health" >/dev/null 2>&1
}

NEED=0
serve_ok || { log "serve :$SERVE_PORT down"; NEED=1; }
proxy_ok "$PRIMARY_PORT" || { log "proxy :$PRIMARY_PORT down"; NEED=1; }
proxy_ok "$TEST_PORT" || { log "proxy :$TEST_PORT down"; NEED=1; }

if [ "$NEED" -eq 1 ]; then
  log "restarting stack"
  # 8>&-: start.sh must not inherit the watchdog lock.
  bash "$APP_DIR/start.sh" 8>&-
fi
