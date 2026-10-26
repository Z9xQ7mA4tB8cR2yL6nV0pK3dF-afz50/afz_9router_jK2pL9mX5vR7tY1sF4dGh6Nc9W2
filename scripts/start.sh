#!/usr/bin/env bash
# Start the complete OpenCode proxy stack WITHOUT systemd.
# Starts: opencode serve :4096, proxy :7015, proxy :4097.
# Idempotent: safe to run from cron @reboot, the watchdog, or by hand.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$SCRIPT_DIR}"
ENV_FILE="${ENV_FILE:-/etc/opencode-stack.env}"
STATE_DIR="${STATE_DIR:-/var/lib/opencode-proxy}"
LOG_DIR="${LOG_DIR:-/var/log/opencode-proxy}"
SERVICE_USER="${SERVICE_USER:-opencode}"
SERVICE_HOME="${SERVICE_HOME:-/home/$SERVICE_USER}"
OPENCODE_BIN="${OPENCODE_BIN:-$SERVICE_HOME/.opencode/bin/opencode}"
NODE_BIN="${NODE_BIN:-$(command -v node || true)}"
SERVE_PORT="${SERVE_PORT:-4096}"
PRIMARY_PORT="${PRIMARY_PORT:-7015}"
TEST_PORT="${TEST_PORT:-4097}"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[ -f "$APP_DIR/proxy.mjs" ] || die "proxy.mjs nei: $APP_DIR/proxy.mjs"
[ -f "$ENV_FILE" ] || die "env nei: $ENV_FILE"
[ -n "$NODE_BIN" ] || die "node nei; Node 22 install koro"
[ -x "$OPENCODE_BIN" ] || die "opencode binary nei: $OPENCODE_BIN"

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

OPENCODE_USERNAME="${OPENCODE_USERNAME:-opencode}"
OPENCODE_PASSWORD="${OPENCODE_PASSWORD:-${OPENCODE_SERVER_PASSWORD:-}}"
OPENCODE_URL="${OPENCODE_URL:-http://127.0.0.1:$SERVE_PORT}"
MODEL="${MODEL:-opencode/deepseek-v4.1-flash-free}"
VARIANT="${VARIANT:-max}"
TOOLS_MODE="${TOOLS_MODE:-safe}"
PROXY_API_KEY="${PROXY_API_KEY:-}"
SANDBOX_DIR="${SANDBOX_DIR:-$STATE_DIR/sandbox}"

mkdir -p "$STATE_DIR" "$STATE_DIR/sandbox" "$LOG_DIR" /tmp/opencode
if [ "$(id -u)" -eq 0 ]; then
  chown -R "$SERVICE_USER:$SERVICE_USER" "$STATE_DIR" "$LOG_DIR" 2>/dev/null || true
fi

# One start at a time. fd 9 is closed in every long-lived child below.
exec 9>"$STATE_DIR/start.lock"
flock -n 9 || { log "another start.sh is already running"; exit 0; }

if [ "$(id -un)" = "$SERVICE_USER" ]; then
  RUN=()
else
  RUN=(runuser -u "$SERVICE_USER" --)
fi

port_pid() {
  ss -tlnp "sport = :$1" 2>/dev/null \
    | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' \
    | head -1
}

kill_port_if() {
  local port="$1" pattern="$2" pid
  pid="$(port_pid "$port")"
  [ -n "$pid" ] || return 0
  if ! ps -p "$pid" -o args= 2>/dev/null | grep -q "$pattern"; then
    log "port $port pid $pid is not $pattern; not killing"
    return 1
  fi
  log "stopping stale pid $pid on port $port"
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 0.25
  done
  kill -9 "$pid" 2>/dev/null || true
}

serve_health() {
  curl -fsS -m 2 -u "$OPENCODE_USERNAME:${OPENCODE_PASSWORD:-}" \
    "http://127.0.0.1:$SERVE_PORT/global/health" >/dev/null 2>&1
}

proxy_health() {
  curl -fsS -m 2 "http://127.0.0.1:$1/health" >/dev/null 2>&1
}

start_serve() {
  if serve_health; then
    log "serve :$SERVE_PORT already healthy"
    return 0
  fi
  kill_port_if "$SERVE_PORT" 'opencode.*serve' || true

  log "starting opencode serve :$SERVE_PORT"
  (
    # opencode uses the current directory as the project directory.
    # It must be the service home, not /root (which the service user
    # cannot read). This was the cause of empty providers on a fresh user.
    cd "$SERVICE_HOME" || exit 1
    setsid -f "${RUN[@]}" env \
      HOME="$SERVICE_HOME" \
      XDG_CONFIG_HOME="$SERVICE_HOME/.config" \
      XDG_DATA_HOME="$SERVICE_HOME/.local/share" \
      OPENCODE_SERVER_PASSWORD="${OPENCODE_PASSWORD:-}" \
      "$OPENCODE_BIN" serve --port "$SERVE_PORT" --hostname 127.0.0.1 \
      >>"$LOG_DIR/serve.log" 2>&1 </dev/null 9>&-
  )

  for _ in $(seq 1 120); do
    if serve_health; then
      log "serve :$SERVE_PORT ready"
      return 0
    fi
    sleep 1
  done
  log "serve :$SERVE_PORT start failed; log tail:" >&2
  tail -20 "$LOG_DIR/serve.log" >&2 || true
  return 1
}

start_proxy() {
  local port="$1" trust="$2" logfile="$3"
  if proxy_health "$port"; then
    log "proxy :$port already healthy"
    return 0
  fi
  kill_port_if "$port" 'proxy\.mjs' || true

  log "starting proxy :$port (TRUST_LOOPBACK=$trust)"
  setsid -f "${RUN[@]}" env \
    HOME="$SERVICE_HOME" \
    OPENCODE_URL="$OPENCODE_URL" \
    OPENCODE_USERNAME="$OPENCODE_USERNAME" \
    OPENCODE_PASSWORD="${OPENCODE_PASSWORD:-}" \
    PROXY_API_KEY="${PROXY_API_KEY:-}" \
    MODEL="$MODEL" \
    VARIANT="$VARIANT" \
    TOOLS_MODE="$TOOLS_MODE" \
    SANDBOX_DIR="$SANDBOX_DIR" \
    HOST="127.0.0.1" \
    PORT="$port" \
    TRUST_LOOPBACK="$trust" \
    "$NODE_BIN" "$APP_DIR/proxy.mjs" \
    >>"$LOG_DIR/$logfile" 2>&1 </dev/null 9>&-

  for _ in $(seq 1 90); do
    if proxy_health "$port"; then
      log "proxy :$port ready"
      return 0
    fi
    sleep 0.5
  done
  log "proxy :$port start failed; log tail:" >&2
  tail -20 "$LOG_DIR/$logfile" >&2 || true
  return 1
}

start_serve
start_proxy "$PRIMARY_PORT" 1 "proxy-$PRIMARY_PORT.log"
start_proxy "$TEST_PORT" 0 "proxy-$TEST_PORT.log"

echo
log "stack status"
ss -tlnp 2>/dev/null | grep -E ":($SERVE_PORT|$PRIMARY_PORT|$TEST_PORT)\b" || true

if [ -n "${PROXY_API_KEY:-}" ]; then
  AUTH=(-H "Authorization: Bearer $PROXY_API_KEY")
else
  AUTH=()
fi
COUNT="$(curl -fsS -m 15 "${AUTH[@]}" "http://127.0.0.1:$PRIMARY_PORT/v1/models" \
  | jq '.data | length' 2>/dev/null || echo 0)"
log "models on :$PRIMARY_PORT = $COUNT"
log "done"
