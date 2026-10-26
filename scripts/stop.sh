#!/usr/bin/env bash
# Stop the complete stack WITHOUT systemd.
# Stops proxy :7015, proxy :4097, then opencode serve :4096.
# Only kills processes whose command line matches this stack.
set -euo pipefail

STATE_DIR="${STATE_DIR:-/var/lib/opencode-proxy}"
SERVE_PORT="${SERVE_PORT:-4096}"
PRIMARY_PORT="${PRIMARY_PORT:-7015}"
TEST_PORT="${TEST_PORT:-4097}"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }

port_pid() {
  ss -tlnp "sport = :$1" 2>/dev/null \
    | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' \
    | head -1
}

stop_port() {
  local port="$1" pattern="$2" pid
  pid="$(port_pid "$port")"
  if [ -z "$pid" ]; then
    log "port $port already free"
    return 0
  fi
  if ! ps -p "$pid" -o args= 2>/dev/null | grep -q "$pattern"; then
    log "port $port pid $pid is not $pattern; skipping"
    return 0
  fi
  log "stopping pid $pid on port $port"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || { log "port $port stopped"; return 0; }
    sleep 0.25
  done
  log "pid $pid did not exit; sending SIGKILL"
  kill -9 "$pid" 2>/dev/null || true
}

stop_port "$PRIMARY_PORT" 'proxy\.mjs'
stop_port "$TEST_PORT" 'proxy\.mjs'
stop_port "$SERVE_PORT" 'opencode.*serve'

rm -f "$STATE_DIR/start.lock" "$STATE_DIR/watchdog.lock" 2>/dev/null || true
log "stack stopped"
