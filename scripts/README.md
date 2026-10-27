# OpenCode OpenAI Proxy — No-Systemd Setup, Operations, and Security Notes

Tested on: **Ubuntu 24.04.5 LTS**, **Node.js 22.23.3**, **opencode 1.19.4**.
Proxy: pure Node ESM, no npm dependencies.

**This guide does not use systemd or systemctl.** The stack runs with `setsid`,
and cron gives `@reboot` + a 1-minute watchdog.

---

## 1. What this is

```
OpenAI client / 9router / Cloudflare Worker
        |
        |  POST /v1/chat/completions   (or /v1/chat)
        v
opencode-proxy  (127.0.0.1:7015 primary, 127.0.0.1:4097 test)
        |
        |  opencode HTTP API: /session, /session/:id/prompt_async, ...
        v
opencode serve  (127.0.0.1:4096, Basic auth)
        |
        v
Free models from the `opencode` provider (dynamic list)
```

For every chat request the proxy creates a real opencode session, sends the
prompt with `prompt_async`, polls for the assistant text, returns OpenAI JSON
or SSE, then deletes the session unless `KEEP_SESSIONS=1`.

---

## 2. Ports

| Port | Process | Bind | Purpose |
|---|---|---|---|
| 4096 | `opencode serve` | `127.0.0.1` | Upstream agent server |
| 7015 | `proxy.mjs` | `127.0.0.1` | Hidden router / external route target |
| 4097 | `proxy.mjs` | `127.0.0.1` | Direct API testing, key enforced |

`7015` is the router-facing port. `4097` is optional but useful for testing.
Both proxy ports are loopback-only. Never expose 4096/4097/7015 directly.

---

## 3. Quick start on a fresh VM (one command)

The bundle contains everything:

```bash
/root/opencode-proxy-bundle.tar.gz
```

### 3.1 Build and copy the bundle

On the current VM:

```bash
tar -czf /root/opencode-proxy-bundle.tar.gz -C /root proxy
sha256sum /root/opencode-proxy-bundle.tar.gz
```

On your PC:

```bash
scp root@CURRENT_VM_IP:/root/opencode-proxy-bundle.tar.gz .
scp opencode-proxy-bundle.tar.gz root@NEW_VM_IP:/root/
```

### 3.2 Install on the new VM

```bash
ssh root@NEW_VM_IP \
  'tar -xzf /root/opencode-proxy-bundle.tar.gz -C /root && bash /root/proxy/setup.sh'
```

`setup.sh` does this serially:

1. apt packages (`curl`, `jq`, `openssl`, `iproute2`, `util-linux`, `cron`)
2. Node.js 22
3. `opencode` service user
4. opencode **1.19.4** (pinned, 1.x)
5. proxy + `start.sh` + `stop.sh` + `watchdog.sh` + `status.sh`
6. `/etc/opencode-stack.env` with a random serve password
7. starts `opencode serve` and both proxies with `setsid`
8. installs `/etc/cron.d/opencode-proxy` (`@reboot` + 1-minute watchdog)
9. verifies health, dynamic model count, and a real chat

It is idempotent: re-running updates files, keeps the existing env file, and
restarts the stack.

### 3.3 Verify

```bash
bash /opt/opencode-proxy/status.sh

curl -s http://127.0.0.1:7015/health
curl -s http://127.0.0.1:7015/open/v1/ | jq .
curl -s -H 'Authorization: Bearer affaz' \
  http://127.0.0.1:7015/v1/models | jq '.data | length'
```

Chat:

```bash
curl -s -H 'Authorization: Bearer affaz' \
  -H 'content-type: application/json' \
  http://127.0.0.1:7015/v1/chat/completions \
  -d '{"model":"glm-5.3-flash-free","messages":[{"role":"user","content":"Reply exactly: SETUP_OK"}]}' \
  | jq -r '.choices[0].message.content'
```

`7015` has `TRUST_LOOPBACK=1`, so a local request works even without the key.
`4097` has `TRUST_LOOPBACK=0`; it needs the key when `PROXY_API_KEY` is set.

---

## 4. Manual serial setup (no systemd, no systemctl)

Run as root on Ubuntu 22.04/24.04. These are the exact commands `setup.sh`
automates.

### 4.1 OS packages

```bash
apt-get update -y
apt-get install -y curl ca-certificates gnupg git jq openssl iproute2 util-linux procps cron
```

### 4.2 Node.js 22

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs
node --version   # v22.x
```

### 4.3 Service user

```bash
useradd --create-home --shell /bin/bash opencode
install -d -o opencode -g opencode -m 755 /home/opencode
```

### 4.4 opencode 1.19.4 (pinned)

Do **not** use opencode 2.x for this proxy. The 1.x installer explicitly
refuses 2.x. On the current VM, root's `PATH` opencode is 2.1.4, so always use
the absolute 1.19.4 binary.

```bash
runuser -u opencode -- env HOME=/home/opencode bash -c \
  'curl -fsSL https://opencode.ai/install | bash -s -- --version 1.19.4 --no-modify-path'

/home/opencode/.opencode/bin/opencode --version
# 1.19.4
```

A clean install with an empty `$HOME` exposes the built-in `opencode` provider
and the free models without login. Verified with 9 free models.

### 4.5 Install proxy and helper scripts

Assumes the bundle is at `/root/proxy`.

```bash
install -d -m 755 /opt/opencode-proxy
install -m 644 /root/proxy/proxy.mjs /opt/opencode-proxy/proxy.mjs
install -m 755 \
  /root/proxy/start.sh \
  /root/proxy/stop.sh \
  /root/proxy/watchdog.sh \
  /root/proxy/status.sh \
  /opt/opencode-proxy/

install -d -o opencode -g opencode -m 750 \
  /var/lib/opencode-proxy \
  /var/lib/opencode-proxy/sandbox \
  /var/log/opencode-proxy
```

### 4.6 Env file

```bash
SERVE_PASS="$(openssl rand -hex 18)"

cat >/etc/opencode-stack.env <<EOF
OPENCODE_URL=http://127.0.0.1:4096
OPENCODE_USERNAME=opencode
OPENCODE_SERVER_PASSWORD=$SERVE_PASS
OPENCODE_PASSWORD=$SERVE_PASS
PROXY_API_KEY=affaz
MODEL=opencode/deepseek-v4.1-flash-free
VARIANT=max
TOOLS_MODE=safe
SANDBOX_DIR=/var/lib/opencode-proxy/sandbox
HOST=127.0.0.1
REQUEST_TIMEOUT_MS=120000
START_TIMEOUT_MS=30000
POLL_MS=350
EOF

chmod 600 /etc/opencode-stack.env
```

Passwordless mode (optional): set `PROXY_API_KEY=` blank. The proxy then
accepts every request. The router-facing `7015` already trusts loopback, so
external requests still work; `/v1/models` also works without a key.

### 4.7 Start opencode serve with setsid

opencode uses the **current directory** as its project directory. Run it from
`/home/opencode`, never from `/root`; otherwise the `opencode` user cannot read
`/root/opencode.jsonc` and `/config/providers` returns 500 / 0 models.

```bash
set -a
. /etc/opencode-stack.env
set +a

cd /home/opencode

setsid -f runuser -u opencode -- env \
  HOME=/home/opencode \
  XDG_CONFIG_HOME=/home/opencode/.config \
  XDG_DATA_HOME=/home/opencode/.local/share \
  OPENCODE_SERVER_PASSWORD="$OPENCODE_PASSWORD" \
  /home/opencode/.opencode/bin/opencode serve --port 4096 --hostname 127.0.0.1 \
  >>/var/log/opencode-proxy/serve.log 2>&1 </dev/null
```

Wait until it is healthy:

```bash
for i in $(seq 1 60); do
  curl -fsS -u "opencode:$OPENCODE_PASSWORD" \
    http://127.0.0.1:4096/global/health && break
  sleep 1
done
```

### 4.8 Start both proxies with setsid

```bash
NODE_BIN="$(command -v node)"

# 7015 — router-facing, loopback trusted
setsid -f runuser -u opencode -- env \
  HOME=/home/opencode \
  OPENCODE_URL=http://127.0.0.1:4096 \
  OPENCODE_USERNAME=opencode \
  OPENCODE_PASSWORD="$OPENCODE_PASSWORD" \
  PROXY_API_KEY="$PROXY_API_KEY" \
  MODEL="$MODEL" \
  VARIANT="$VARIANT" \
  TOOLS_MODE="$TOOLS_MODE" \
  SANDBOX_DIR="$SANDBOX_DIR" \
  HOST=127.0.0.1 \
  PORT=7015 \
  TRUST_LOOPBACK=1 \
  "$NODE_BIN" /opt/opencode-proxy/proxy.mjs \
  >>/var/log/opencode-proxy/proxy-7015.log 2>&1 </dev/null

# 4097 — key enforced
setsid -f runuser -u opencode -- env \
  HOME=/home/opencode \
  OPENCODE_URL=http://127.0.0.1:4096 \
  OPENCODE_USERNAME=opencode \
  OPENCODE_PASSWORD="$OPENCODE_PASSWORD" \
  PROXY_API_KEY="$PROXY_API_KEY" \
  MODEL="$MODEL" \
  VARIANT="$VARIANT" \
  TOOLS_MODE="$TOOLS_MODE" \
  SANDBOX_DIR="$SANDBOX_DIR" \
  HOST=127.0.0.1 \
  PORT=4097 \
  TRUST_LOOPBACK=0 \
  "$NODE_BIN" /opt/opencode-proxy/proxy.mjs \
  >>/var/log/opencode-proxy/proxy-4097.log 2>&1 </dev/null
```

Wait for both:

```bash
for p in 7015 4097; do
  for i in $(seq 1 60); do
    curl -fsS "http://127.0.0.1:$p/health" && break
    sleep 0.5
  done
done
```

Or use the helper, which performs the same `setsid` starts and waits:

```bash
bash /opt/opencode-proxy/start.sh
```

### 4.9 Cron: @reboot + 1-minute watchdog

No systemd. Cron starts the stack at boot and restarts any dead port.

```bash
cat >/etc/cron.d/opencode-proxy <<'EOF'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
@reboot root bash /opt/opencode-proxy/start.sh >> /var/log/opencode-proxy/cron.log 2>&1
* * * * * root bash /opt/opencode-proxy/watchdog.sh >> /var/log/opencode-proxy/cron.log 2>&1
EOF

chmod 644 /etc/cron.d/opencode-proxy

# Make sure the cron daemon is running (no systemctl).
pgrep -x cron >/dev/null || /usr/sbin/cron
```

`watchdog.sh` checks 4096/7015/4097 every minute. If any is down, it calls
`start.sh`. `start.sh` is idempotent and uses a lock, so overlapping cron runs
cannot start duplicate proxies.

### 4.10 Verify

```bash
curl -s http://127.0.0.1:7015/health | jq .
curl -s http://127.0.0.1:7015/open/v1/ | jq .
curl -s -H 'Authorization: Bearer affaz' \
  http://127.0.0.1:7015/v1/models | jq '.data | length'

curl -s -H 'Authorization: Bearer affaz' \
  -H 'content-type: application/json' \
  http://127.0.0.1:7015/v1/chat/completions \
  -d '{"model":"glm-5.3-flash-free","messages":[{"role":"user","content":"Reply exactly: MANUAL_OK"}]}' \
  | jq -r '.choices[0].message.content'
```

Expected: `MANUAL_OK`.

---

## 5. Operations (no systemd)

```bash
# start / restart (idempotent)
bash /opt/opencode-proxy/start.sh

# stop proxies and opencode serve
bash /opt/opencode-proxy/stop.sh

# status + dynamic model list
bash /opt/opencode-proxy/status.sh

# logs
tail -f /var/log/opencode-proxy/serve.log
tail -f /var/log/opencode-proxy/proxy-7015.log
tail -f /var/log/opencode-proxy/proxy-4097.log
tail -f /var/log/opencode-proxy/cron.log

# cron jobs
cat /etc/cron.d/opencode-proxy
```

`start.sh`:
- starts `opencode serve` if 4096 is not healthy
- starts 7015/4097 if their health check fails
- leaves healthy processes alone
- uses `flock` so two starts cannot race
- closes the lock fd in children so long-lived processes do not hold it

### Change the proxy key

```bash
vi /etc/opencode-stack.env     # PROXY_API_KEY=...
bash /opt/opencode-proxy/start.sh
```

`start.sh` restarts only unhealthy ports; after changing env, run `stop.sh`
then `start.sh` to force both proxies to reload:

```bash
bash /opt/opencode-proxy/stop.sh
bash /opt/opencode-proxy/start.sh
```

### Change the serve password

Edit both `OPENCODE_SERVER_PASSWORD` and `OPENCODE_PASSWORD` in
`/etc/opencode-stack.env`, then:

```bash
bash /opt/opencode-proxy/stop.sh
bash /opt/opencode-proxy/start.sh
```

---

## 6. Dynamic behavior and reliability

### Dynamic model list

- `/v1/models` is fetched from opencode `/config/providers` and cached 60s.
- New/removed free models appear automatically. No single model is hardcoded.
- Any ID from `/v1/models` is accepted. Both `glm-5.3-flash-free` and
  `opencode/glm-5.3-flash-free` work.
- If the configured default model disappears, a request without `model` falls
  back to the first available free model.
- If the upstream provider call fails briefly, the last known model list is
  served instead of returning an empty list.

### What can break (no TTL)

There is **no 2-day expiry** in the proxy. It runs until stopped. Real risks:

1. **VM reboot** — `@reboot` cron + watchdog restore the stack. If cron is not
   running at boot, nothing starts; keep the `cron` package enabled.
2. **opencode version drift** — pinned to 1.19.4 because the proxy is tested
   against its HTTP API. Do not auto-upgrade to 2.x.
3. **Upstream free model availability / rate limit** — a specific model can
   stop working. The list refreshes; the default fallback picks another.
4. **Password/port drift** — if serve restarts with a different password or
   port, the proxy returns 401/502 until the env file is fixed.
5. **Wrong serve cwd** — `opencode serve` must run from `/home/opencode`;
   running it from `/root` makes the `opencode` user unable to read
   `/root/opencode.jsonc`, so providers/models become empty. `start.sh`
   handles this by `cd "$SERVICE_HOME"` before starting serve.
6. **Hidden router changes** — external `/open/v1/*` behavior is router-side.
7. **Cron daemon not running** — watchdog cannot restart anything.

---

## 7. External route and `/open/v1/`

Routes:

- `GET /health`, `GET /v1/health` — health
- `GET /v1/models`, `GET /models` — dynamic model list
- `POST /v1/chat/completions`, `POST /v1/chat` — chat
- `GET /`, `GET /v1/`, `GET /open/v1/` — info JSON
- `/open/...` is stripped, so `/open/v1/models` = `/v1/models`

Current external results:

| Path | Result |
|---|---|
| `GET /open/v1/` | 200, proxy info |
| `GET /open/v1/models` | 200, dynamic list (8 via router) |
| `POST /open/v1/chat` | 200, works |
| `POST /open/v1/chat/completions` | intercepted by hidden router; hangs |
| `GET /open/v1/chat/completions` | 404 from router |

The standard external `POST /open/v1/chat/completions` is blocked by the
hidden router before it reaches the proxy. The proxy cannot fix that. Use
`/open/v1/chat` for manual tests, or fix the router so it forwards the
standard path.

External mapping to the new VM (`https://router1...` → new VM `127.0.0.1:7015`)
is router/Cloudflare configuration, outside this bundle. `setup.sh` only
prepares the local VM stack.

### Security reality

The public endpoint is currently open: the router calls `127.0.0.1:7015`
without the client `Authorization` header, and `7015` uses
`TRUST_LOOPBACK=1`. A request with no key or a wrong key still returns 200.

Fix at the edge (Cloudflare Worker / router), not in the proxy:

```js
const auth = request.headers.get("authorization") || ""
if (auth !== `Bearer ${env.PROXY_API_KEY}`) {
  return new Response(JSON.stringify({
    error: { message: "unauthorized" }
  }), {
    status: 401,
    headers: { "content-type": "application/json" },
  })
}
// ... existing forward to 7015 ...
```

Alternative: make the router forward `Authorization: Bearer affaz` (or
`x-api-key: affaz`) and set `TRUST_LOOPBACK=0` for 7015, then restart. The
router must forward the header on both `/v1/models` and chat.

If password is not important to you, leaving `PROXY_API_KEY=affaz` is fine for
local use, but the public URL remains usable by anyone who knows it until the
edge enforces auth.

---

## 8. Troubleshooting

| Symptom | Check | Fix |
|---|---|---|
| `invalid proxy api key` | Is the request loopback? Is `TRUST_LOOPBACK` right? | Send `Authorization: Bearer affaz`; or use 7015 for loopback/router |
| `upstream unreachable` / 502 | `curl -u opencode:PASS http://127.0.0.1:4096/global/health` | `bash /opt/opencode-proxy/start.sh`; check serve log |
| Models list empty / `/config/providers` 500 | serve cwd must be `/home/opencode`, not `/root` | `cd /home/opencode` before `opencode serve`; `start.sh` already does this |
| Chat hangs on `/open/v1/chat/completions` | Proxy log has no POST entry | Router intercepts that path; use `/open/v1/chat` |
| First chat after boot is slow | `START_TIMEOUT_MS=30000` | Increase to 60000 and restart |
| Tool request hangs | `TOOLS_MODE=full` and a permission prompt | Keep `TOOLS_MODE=safe` |
| `EADDRINUSE` | `ss -tlnp \| grep -E ':(4096\|4097\|7015)'` | `bash /opt/opencode-proxy/stop.sh`, then start |
| Watchdog not restarting | `pgrep -x cron` | `pgrep -x cron >/dev/null \|\| /usr/sbin/cron` |
| `invalid JSON body` from Windows cmd | cmd does not support single-quoted JSON | Use a JSON file or PowerShell `curl.exe` |

Windows `cmd` example with a body file:

```cmd
curl -X POST "https://router1.affaz101.eu.cc/open/v1/chat" ^
  -H "Content-Type: application/json" ^
  -H "Authorization: Bearer affaz" ^
  --data-binary "@body.json"
```

`body.json`:

```json
{
  "model": "glm-5.3-flash-free",
  "messages": [
    { "role": "user", "content": "Reply exactly: WIN_OK" }
  ]
}
```

PowerShell: use `curl.exe`, not the `curl` alias.

---

## 9. Files

| File | Purpose |
|---|---|
| `proxy.mjs` | The proxy |
| `setup.sh` | Fresh VM installer, **no systemd** (setsid + cron) |
| `start.sh` | Start/restart the whole stack, idempotent |
| `stop.sh` | Stop proxies and serve |
| `watchdog.sh` | Cron watchdog, restarts down ports |
| `status.sh` | Health + model list |
| `env.example` | Env template |
| `README.md` | This document |

