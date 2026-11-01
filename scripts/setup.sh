#!/usr/bin/env bash
# OpenCode OpenAI Proxy — fresh Ubuntu 22.04/24.04 installer.
# NO systemd, NO systemctl. Uses setsid + cron watchdog.
#
# Run as root:
#   bash /root/proxy/setup.sh
#
# Idempotent: re-running updates the proxy/scripts, keeps the existing
# /etc/opencode-stack.env, restarts the stack, and refreshes the cron file.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROXY_SRC="$SCRIPT_DIR/proxy.mjs"

APP_DIR="${APP_DIR:-/opt/opencode-proxy}"
ENV_FILE="${ENV_FILE:-/etc/opencode-stack.env}"
STATE_DIR="${STATE_DIR:-/var/lib/opencode-proxy}"
LOG_DIR="${LOG_DIR:-/var/log/opencode-proxy}"
SERVICE_USER="${SERVICE_USER:-opencode}"
SERVICE_HOME="${SERVICE_HOME:-/home/$SERVICE_USER}"
OPENCODE_VERSION="${OPENCODE_VERSION:-1.19.4}"
SERVE_PORT="${SERVE_PORT:-4096}"
PRIMARY_PORT="${PRIMARY_PORT:-7015}"
TEST_PORT="${TEST_PORT:-4097}"
PROXY_KEY="${PROXY_API_KEY-affaz}"
DEFAULT_MODEL="${MODEL:-opencode/big-pickle}"
START_TIMEOUT_MS="${START_TIMEOUT_MS:-30000}"
CRON_FILE="${CRON_FILE:-/etc/cron.d/opencode-proxy}"

log()  { printf '\n[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
warn() { printf '\n[WARN] %s\n' "$*" >&2; }
die()  { printf '\n[ERROR] %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "root lagbe: sudo bash setup.sh"
[ -f "$PROXY_SRC" ] || die "proxy.mjs nei. Puro proxy directory copy koro."
for f in start.sh stop.sh watchdog.sh status.sh; do
  [ -f "$SCRIPT_DIR/$f" ] || die "$f nei. Puro proxy directory copy koro."
done
command -v apt-get >/dev/null || die "ei script Ubuntu/Debian er jonno"
[[ "$OPENCODE_VERSION" =~ ^1\.[0-9]+\.[0-9]+$ ]] || die "OPENCODE_VERSION must be 1.x, e.g. 1.19.4"

# Refuse to fight a foreign process on our ports. Our own stack is allowed.
check_port_owner() {
  local port="$1" pattern="$2" pid cmd
  ss -tln 2>/dev/null | grep -qE "[:.]${port}[[:space:]]" || return 0
  pid="$(ss -tlnp "sport = :$port" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -1)"
  [ -n "$pid" ] || die "port $port already in use; owner detect kora jayni. Stop kore abar run koro."
  cmd="$(ps -p "$pid" -o args= 2>/dev/null || true)"
  printf '%s' "$cmd" | grep -q "$pattern" \
    || die "port $port occupied by pid $pid: $cmd"
}
check_port_owner "$SERVE_PORT" 'opencode.*serve'
check_port_owner "$PRIMARY_PORT" 'proxy\.mjs'
check_port_owner "$TEST_PORT" 'proxy\.mjs'

log "apt packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y curl ca-certificates gnupg git jq openssl iproute2 util-linux procps cron

# Node 22
NODE_MAJOR=0
if command -v node >/dev/null 2>&1; then
  NODE_MAJOR="$(node -p 'Number(process.versions.node.split(".")[0])')"
fi
if [ "$NODE_MAJOR" -lt 22 ]; then
  log "Node.js 22 install"
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi
NODE_BIN="$(command -v node)"
log "node: $("$NODE_BIN" --version) at $NODE_BIN"

log "service user: $SERVICE_USER"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$SERVICE_USER"
fi
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 755 "$SERVICE_HOME"

log "opencode $OPENCODE_VERSION install (pinned, 1.x)"
OPENCODE_BIN="$SERVICE_HOME/.opencode/bin/opencode"
INSTALLED_VERSION=""
if [ -x "$OPENCODE_BIN" ]; then
  INSTALLED_VERSION="$(runuser -u "$SERVICE_USER" -- env HOME="$SERVICE_HOME" "$OPENCODE_BIN" --version 2>/dev/null | tail -1 || true)"
fi
if [ "$INSTALLED_VERSION" != "$OPENCODE_VERSION" ]; then
  runuser -u "$SERVICE_USER" -- env HOME="$SERVICE_HOME" bash -c \
    "curl -fsSL https://opencode.ai/install | bash -s -- --version $OPENCODE_VERSION --no-modify-path"
fi
[ -x "$OPENCODE_BIN" ] || die "opencode install fail: $OPENCODE_BIN nei"
log "opencode: $(runuser -u "$SERVICE_USER" -- env HOME="$SERVICE_HOME" "$OPENCODE_BIN" --version | tail -1)"

log "proxy files"
install -d -m 755 "$APP_DIR"
install -m 644 "$PROXY_SRC" "$APP_DIR/proxy.mjs"
for f in start.sh stop.sh watchdog.sh status.sh; do
  install -m 755 "$SCRIPT_DIR/$f" "$APP_DIR/$f"
done
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 750 \
  "$STATE_DIR" "$STATE_DIR/sandbox" "$LOG_DIR"

if [ ! -f "$ENV_FILE" ]; then
  log "write $ENV_FILE"
  SERVE_PASS="${OPENCODE_SERVER_PASSWORD:-$(openssl rand -hex 18)}"
  umask 077
  cat >"$ENV_FILE" <<EOF
OPENCODE_URL=http://127.0.0.1:$SERVE_PORT
OPENCODE_USERNAME=opencode
OPENCODE_SERVER_PASSWORD=$SERVE_PASS
OPENCODE_PASSWORD=$SERVE_PASS
PROXY_API_KEY=$PROXY_KEY
MODEL=$DEFAULT_MODEL
VARIANT=max
TOOLS_MODE=safe
SANDBOX_DIR=$STATE_DIR/sandbox
HOST=127.0.0.1
REQUEST_TIMEOUT_MS=180000
START_TIMEOUT_MS=$START_TIMEOUT_MS
POLL_MS=350
EOF
  chmod 600 "$ENV_FILE"
else
  log "keeping existing $ENV_FILE"
fi

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a
[ -n "${OPENCODE_PASSWORD:-}" ] || die "OPENCODE_PASSWORD empty in $ENV_FILE"

log "starting stack (setsid, no systemd)"
export APP_DIR ENV_FILE STATE_DIR LOG_DIR SERVICE_USER SERVICE_HOME
export OPENCODE_BIN NODE_BIN SERVE_PORT PRIMARY_PORT TEST_PORT
export CRON_FILE
bash "$APP_DIR/start.sh"

log "cron @reboot + 1-minute watchdog"
install -d -m 755 "$(dirname "$CRON_FILE")"
cat >"$CRON_FILE" <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
@reboot root bash $APP_DIR/start.sh >> $LOG_DIR/cron.log 2>&1
* * * * * root bash $APP_DIR/watchdog.sh >> $LOG_DIR/cron.log 2>&1
EOF
chmod 644 "$CRON_FILE"
if ! pgrep -x cron >/dev/null 2>&1; then
  [ -x /usr/sbin/cron ] || die "cron daemon nei"
  /usr/sbin/cron || die "cron start fail"
fi
log "cron running: $(pgrep -x cron | head -1)"

log "verify"
  if [ -n "${PROXY_API_KEY:-}" ]; then
    AUTH=(-H "Authorization: Bearer $PROXY_API_KEY")
  else
    AUTH=()
  fi
  
  curl -fsS -m 5 "http://127.0.0.1:$PRIMARY_PORT/health" >/dev/null \
    || die "proxy :$PRIMARY_PORT health fail"
  curl -fsS -m 5 "http://127.0.0.1:$TEST_PORT/health" >/dev/null \
    || die "proxy :$TEST_PORT health fail"
  curl -fsS -m 5 "http://127.0.0.1:$PRIMARY_PORT/open/v1/" >/dev/null \
    || die "proxy /open/v1/ fail"
  
  # Wait for serve to be fully ready (opencode needs time to load models)
  log "waiting for serve to be ready..."
  for i in {1..30}; do
    if curl -fsS -m 3 "http://127.0.0.1:$SERVE_PORT/health" >/dev/null 2>&1; then
      log "serve :$SERVE_PORT ready"
      break
    fi
    sleep 1
  done
  
  MODEL_COUNT="$(curl -fsS -m 15 "${AUTH[@]}" \
    "http://127.0.0.1:$PRIMARY_PORT/v1/models" | jq '.data | length' 2>/dev/null || echo 0)"
  log "models returned: $MODEL_COUNT"
  [ "$MODEL_COUNT" -gt 0 ] || warn "model list empty — opencode provider check koro"

  # Dynamically pick the default model from whatever is actually available now,
  # instead of relying on a hardcoded model that may disappear from the free list.
  if [ "$MODEL_COUNT" -gt 0 ]; then
    FIRST_MODEL="$(curl -fsS -m 15 "${AUTH[@]}" \
      "http://127.0.0.1:$PRIMARY_PORT/v1/models" 2>/dev/null | jq -r '.data[0].id' 2>/dev/null || true)"
    if [ -n "$FIRST_MODEL" ] && [ "$FIRST_MODEL" != "null" ]; then
      DEFAULT_MODEL="$FIRST_MODEL"
      log "dynamic default model selected: $DEFAULT_MODEL"
      # Persist the live choice into the env file for future restarts.
      if [ -f "$ENV_FILE" ]; then
        sed -i "s|^MODEL=.*|MODEL=$DEFAULT_MODEL|" "$ENV_FILE" 2>/dev/null || true
      fi
    fi
  fi

  # Dynamically discover candidate models from the live model list so a single
  # slow/unavailable free model cannot fail the whole runner setup.
  CANDIDATE_MODELS=("$DEFAULT_MODEL")
  LIVE_MODELS="$(curl -fsS -m 15 "${AUTH[@]}" \
    "http://127.0.0.1:$PRIMARY_PORT/v1/models" 2>/dev/null | jq -r '.data[].id' 2>/dev/null || true)"
  if [ -n "$LIVE_MODELS" ]; then
    while IFS= read -r m; do
      [ -n "$m" ] || continue
      # de-duplicate
      if [[ " ${CANDIDATE_MODELS[*]} " != *" $m "* ]]; then
        CANDIDATE_MODELS+=("$m")
      fi
    done <<< "$LIVE_MODELS"
  fi

  log "chat test"
  CHAT_OK=0
  for TEST_MODEL in "${CANDIDATE_MODELS[@]:0:4}"; do
    log "trying chat model: $TEST_MODEL (variant=low)"
    CHAT_OUT="$(curl -fsS -m 120 "${AUTH[@]}" \
      -H 'content-type: application/json' \
      "http://127.0.0.1:$PRIMARY_PORT/v1/chat/completions" \
      -d "{\"model\":\"$TEST_MODEL\",\"variant\":\"low\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply exactly: SETUP_OK\"}]}" \
      2>/dev/null | jq -r '.choices[0].message.content // .error.message' 2>/dev/null || true)"
    if [ -n "$CHAT_OUT" ]; then
      CHAT_OK=1
      printf 'chat result: %s\n' "$CHAT_OUT"
      break
    fi
    warn "chat attempt failed for $TEST_MODEL"
  done

  if [ "$CHAT_OK" -ne 1 ]; then
    warn "chat verification failed on all candidate models; continuing startup (models endpoint is healthy)"
  fi

log "done"
cat <<EOF

NO SYSTEMD USED.

Proxy:       http://127.0.0.1:$PRIMARY_PORT   (TRUST_LOOPBACK=1)
Test proxy:  http://127.0.0.1:$TEST_PORT   (TRUST_LOOPBACK=0)
Models:      GET /v1/models        (dynamic, 60s refresh)
Chat:        POST /v1/chat/completions   (also /v1/chat)
Root:        GET /open/v1/         (info; /open/v1/models = list)

Start:       bash $APP_DIR/start.sh
Stop:        bash $APP_DIR/stop.sh
Status:      bash $APP_DIR/status.sh
Logs:        tail -f $LOG_DIR/proxy-$PRIMARY_PORT.log
             tail -f $LOG_DIR/proxy-$TEST_PORT.log
             tail -f $LOG_DIR/serve.log

Auto-start:  $CRON_FILE
             @reboot start.sh + every minute watchdog.sh
Env file:    $ENV_FILE
Proxy key:   ${PROXY_API_KEY:-<empty: passwordless>}
Serve pass:  $OPENCODE_PASSWORD (in $ENV_FILE)

SECURITY:
- Both proxy ports bind 127.0.0.1. Do not expose them directly.
- $PRIMARY_PORT trusts loopback because the hidden router does not forward
  the client Authorization header.
- External auth must be enforced at the router/Cloudflare edge.
- PROXY_API_KEY=$PROXY_KEY is a test key. Set PROXY_API_KEY= (empty) in
  $ENV_FILE for passwordless local mode, or use a strong key.
EOF
