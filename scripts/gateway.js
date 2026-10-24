/**
 * 9Router Local Node Gateway - Smart Egress Strategy Router
 * ========================================================
 * Runs inside GitHub Actions runner VM on public node port (e.g. 6001, 6002).
 * - Routes GET /logs & GET /stream -> directly streams local runner log ring-buffer & file
 * - Routes POST /logs/clear & DELETE /logs -> resets local log buffer without restarting runner
 * - Smart Egress Strategy Router with automatic fallback:
 *   Strategies: direct, warp (multi-instance), cf_tunnel, warp_ipv6
 *   Automatic fallback on 429, 5xx, network errors
 * - Routes /kilo/v1/* -> forwards to Kilo AI Gateway using selected strategy
 * - Routes all other requests (/v1/*, /models, etc.) -> reverse-proxies to 9Router via selected strategy
 */

const http = require("http");
const https = require("https");
const fs = require("fs");

// Parse CLI args
const args = process.argv.slice(2);
function getArg(flag, def) {
  const idx = args.indexOf(flag);
  return idx !== -1 && args[idx + 1] ? args[idx + 1] : def;
}

const PORT = parseInt(getArg("--port", process.env.PORT || 6001), 10);
const TARGET_PORT = parseInt(getArg("--target-port", process.env.TARGET_PORT || (PORT + 500)), 10);
const SLOT = getArg("--slot", process.env.NODE || "node-1");
const LOG_FILE = getArg("--log-file", "/tmp/runner.log");
const REDIS_BASE = getArg("--redis-base", process.env.REDIS_BASE || "https://jelab101-rimjhim.hf.space").replace(/\/+$/, "");

// Kilo AI Gateway channel
const KILO_UPSTREAM = (
  getArg("--kilo-upstream", process.env.KILO_UPSTREAM || "https://api.kilo.ai/api/gateway")
).replace(/\/+$/, "");
const KILO_API_KEY = getArg("--kilo-api-key", process.env.KILO_API_KEY || "anonymous");
const KILO_FREE_ONLY_DEFAULT = String(
  getArg("--kilo-free-only", process.env.KILO_FREE_ONLY || "true")
).toLowerCase() === "true";

// WARP SOCKS5 ports (multi-instance)
const WARP_PORTS = [40000, 40001, 40002, 40003];
let currentWarpPortIdx = 0;

// CF Tunnel HTTP proxy port (cloudflared runs here)
const CF_TUNNEL_PORT = 8080;

// Strategy state (refreshed from Redis periodically)
let strategyState = {
  warpEnabled: true,
  cfTunnelEnabled: false,
  currentStrategy: "warp",
  warpPortIdx: 0,
  stats: {
    direct: { total: 0, success: 0, latencySum: 0, recent429: 0, last429: 0 },
    warp: { total: 0, success: 0, latencySum: 0, recent429: 0, last429: 0 },
    warp_cf_tunnel: { total: 0, success: 0, latencySum: 0, recent429: 0, last429: 0 },
    warp_ipv6: { total: 0, success: 0, latencySum: 0, recent429: 0, last429: 0 },
  },
  lastStrategyRefresh: 0,
};

// In-memory ring buffer for logs
let buffer = [];
let fileOffset = 0;
const sseClients = new Set();

function broadcastSse(payload) {
  const data = `data: ${JSON.stringify(payload)}\n\n`;
  for (const client of sseClients) {
    try { client.write(data); } catch { sseClients.delete(client); }
  }
}

function syncFile() {
  try {
    if (!fs.existsSync(LOG_FILE)) return;
    const stat = fs.statSync(LOG_FILE);
    if (stat.size < fileOffset) {
      fileOffset = 0; buffer = [];
      broadcastSse({ type: "clear", message: "Runner log file cleared." });
    }
    if (stat.size > fileOffset) {
      const fd = fs.openSync(LOG_FILE, "r");
      const len = stat.size - fileOffset;
      const buf = Buffer.alloc(len);
      fs.readSync(fd, buf, 0, len, fileOffset);
      fs.closeSync(fd);
      fileOffset = stat.size;
      const newLines = buf.toString("utf-8").split("\n").map(l => l.trimEnd()).filter(Boolean);
      buffer.push(...newLines);
      if (buffer.length > 1000) buffer = buffer.slice(-1000);
      for (const line of newLines) broadcastSse({ type: "line", text: line });
    }
  } catch (e) {}
}
setInterval(syncFile, 250);

// Redis HTTP helpers
async function redisGet(key) {
  try {
    const url = `${REDIS_BASE}/GET/${encodeURIComponent(key)}`;
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    if (!res.ok) return null;
    const data = await res.json();
    let val = data.GET ?? data.value;
    if (typeof val === "string") {
      try { val = JSON.parse(val); } catch {}
    }
    return val;
  } catch { return null; }
}

async function redisSet(key, value, ttl = 3600) {
  try {
    const valStr = typeof value === "object" ? JSON.stringify(value) : String(value);
    const encoded = encodeURIComponent(valStr).replace(/%2F/g, "%252F");
    const url = `${REDIS_BASE}/SETEX/${encodeURIComponent(key)}/${ttl}/${encoded}`;
    const res = await fetch(url);
    return res.ok;
  } catch { return false; }
}

// Strategy detection functions
async function detectDirectIP() {
  try {
    const res = await fetch("https://api.ipify.org", { headers: { "User-Agent": "9Router-Gateway" } });
    if (res.ok) return (await res.text()).trim();
  } catch {}
  return "unknown";
}

async function detectWarpIP(port) {
  try {
    const socksAgent = require("socks-proxy-agent");
    const agent = new socksAgent(`socks5://127.0.0.1:${port}`);
    const res = await fetch("https://api.ipify.org", { agent, headers: { "User-Agent": "9Router-Gateway" } });
    if (res.ok) return (await res.text()).trim();
  } catch (e) {
    return detectDirectIP();
  }
  return "unknown";
}

async function detectCFProxyIP() {
  // Detect IP via cloudflared HTTP proxy (port 8080)
  try {
    const HttpProxyAgent = require("http-proxy-agent");
    const agent = new HttpProxyAgent(`http://127.0.0.1:${CF_TUNNEL_PORT}`);
    const res = await fetch("https://api.ipify.org", { agent, headers: { "User-Agent": "9Router-Gateway" } });
    if (res.ok) return (await res.text()).trim();
  } catch (e) {
    return detectDirectIP();
  }
  return "unknown";
}

function getCurrentWarpPort() {
  return WARP_PORTS[currentWarpPortIdx];
}

function rotateWarpPort() {
  currentWarpPortIdx = (currentWarpPortIdx + 1) % WARP_PORTS.length;
  console.log(`[STRATEGY] Rotated WARP to port ${getCurrentWarpPort()}`);
}

function getStrategyStats(name) {
  return strategyState.stats[name] || { total: 0, success: 0, latencySum: 0, recent429: 0, last429: 0 };
}

function recordStrategyResult(name, success, latency, is429) {
  const s = getStrategyStats(name);
  s.total++;
  if (success) s.success++;
  s.latencySum += latency;
  if (is429) {
    s.recent429++;
    s.last429 = Date.now();
  }
  // Decay 429 after 5 min
  if (s.last429 && Date.now() - s.last429 > 300000) {
    s.recent429 = Math.max(0, s.recent429 - 1);
  }
}

function calculateStrategyScore(name) {
  const s = getStrategyStats(name);
  if (!s) return 0;
  
  const priority = { direct: 100, warp: 80, warp_cf_tunnel: 70, warp_ipv6: 60 }[name] || 50;
  let score = priority;
  
  const successRate = s.total > 0 ? (s.success / s.total) * 100 : 100;
  score += (successRate / 100) * 30;
  
  const avgLatency = s.total > 0 ? s.latencySum / s.total : 100;
  score -= Math.min(avgLatency / 20, 20);
  
  score -= s.recent429 * 15;
  
  // IP freshness bonus (if we have recent IP data from monitor.py)
  // This would need Redis integration; for now skip
  
  return score;
}

function getAvailableStrategies() {
  const strategies = ["direct"]; // Always available
  
  if (strategyState.warpEnabled) {
    if (strategyState.cfTunnelEnabled) {
      // CHAINED: WARP SOCKS5 → CF Tunnel HTTP proxy
      strategies.push("warp_cf_tunnel");
    } else {
      // WARP only (no CF Tunnel)
      strategies.push("warp");
    }
    strategies.push("warp_ipv6");
  }
  
  return strategies;
}

function selectBestStrategy() {
  const available = getAvailableStrategies();
  let best = "direct";
  let bestScore = -Infinity;
  
  for (const name of available) {
    const score = calculateStrategyScore(name);
    if (score > bestScore) {
      bestScore = score;
      best = name;
    }
  }
  
  return best;
}

async function refreshStrategyState() {
  try {
    // Get warp_enabled from Redis
    const warpState = await redisGet(`9rt:warp:${SLOT}`);
    if (warpState && typeof warpState === "object") {
      strategyState.warpEnabled = warpState.enabled !== false;
    }
    
    // Get CF Tunnel status from Redis (supports both named and temporary)
    const tunnelState = await redisGet(`9rt:tunnel:${SLOT}`);
    if (tunnelState && typeof tunnelState === "object") {
      strategyState.cfTunnelEnabled = tunnelState.active === true;
      strategyState.tunnelType = tunnelState.type || "named"; // "named" or "temporary"
    }
    
    // Get current strategy from heartbeat
    const hb = await redisGet(`9rt:hb:${SLOT}`);
    if (hb && typeof hb === "object") {
      strategyState.currentStrategy = hb.egress_strategy || "warp";
    }
  } catch (e) {
    console.warn("[STRATEGY] Failed to refresh state:", e.message);
  }
  strategyState.lastStrategyRefresh = Date.now();
}

// Refresh strategy state every 30 seconds
setInterval(refreshStrategyState, 30000);
refreshStrategyState(); // Initial

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, x-admin-key, Accept, Origin",
};

// Create agent for strategy
function createStrategyAgent(strategy) {
  if (strategy === "direct") {
    return new http.Agent({ keepAlive: true });
  }
  
  if (strategy === "warp" || strategy === "warp_ipv6") {
    const port = getCurrentWarpPort();
    try {
      const SocksProxyAgent = require("socks-proxy-agent");
      return new SocksProxyAgent(`socks5://127.0.0.1:${port}`);
    } catch (e) {
      console.warn("[STRATEGY] socks-proxy-agent not available, falling back to direct");
      return new http.Agent({ keepAlive: true });
    }
  }
  
  if (strategy === "warp_cf_tunnel") {
    // CHAINED: WARP SOCKS5 → CF Tunnel HTTP proxy
    // cloudflared runs HTTP proxy on localhost:8080, configured to use WARP upstream
    // This requires cloudflared to be started with: cloudflared tunnel --url http://localhost:8080 --proxy-destination socks5://127.0.0.1:40000
    // For now, connect to cloudflared HTTP proxy which should route through WARP
    try {
      const HttpProxyAgent = require("http-proxy-agent");
      return new HttpProxyAgent(`http://127.0.0.1:${CF_TUNNEL_PORT}`);
    } catch (e) {
      console.warn("[STRATEGY] http-proxy-agent not available, falling back to WARP only");
      const port = getCurrentWarpPort();
      const SocksProxyAgent = require("socks-proxy-agent");
      return new SocksProxyAgent(`socks5://127.0.0.1:${port}`);
    }
  }
  
  return new http.Agent({ keepAlive: true });
}

// Forward request via strategy
async function forwardViaStrategy(req, res, strategy, targetHost, targetPort, targetPath) {
  const startTime = Date.now();
  const agent = createStrategyAgent(strategy);
  
  const options = {
    hostname: targetHost,
    port: targetPort,
    path: targetPath,
    method: req.method,
    headers: { ...req.headers },
    agent,
  };
  
  delete options.headers.host;
  delete options.headers["content-length"];
  
  return new Promise((resolve, reject) => {
    const proxyReq = http.request(options, (proxyRes) => {
      const latency = Date.now() - startTime;
      const is429 = proxyRes.statusCode === 429;
      const isError = proxyRes.statusCode >= 500;
      const success = !is429 && !isError;
      
      recordStrategyResult(strategy, success, latency, is429);
      
      // On 429 or 5xx, try next strategy
      if (is429 || isError) {
        proxyRes.on("data", () => {});
        proxyRes.on("end", () => {
          tryNextStrategy(req, res, strategy, targetHost, targetPort, targetPath);
        });
        return;
      }
      
      const resHeaders = { ...proxyRes.headers };
      resHeaders["Access-Control-Allow-Origin"] = "*";
      resHeaders["X-Egress-Strategy"] = strategy;
      res.writeHead(proxyRes.statusCode, resHeaders);
      proxyRes.pipe(res);
      resolve();
    });
    
    proxyReq.on("error", (err) => {
      const latency = Date.now() - startTime;
      recordStrategyResult(strategy, false, latency, false);
      tryNextStrategy(req, res, strategy, targetHost, targetPort, targetPath);
    });
    
    req.pipe(proxyReq);
  });
}

function tryNextStrategy(req, res, failedStrategy, targetHost, targetPort, targetPath) {
  const available = getAvailableStrategies().filter(s => s !== failedStrategy);
  
  if (available.length === 0) {
    res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "Bad Gateway", message: "All egress strategies exhausted" }));
    return;
  }
  
  // Pick next best
  let best = available[0];
  let bestScore = -Infinity;
  for (const name of available) {
    const score = calculateStrategyScore(name);
    if (score > bestScore) { bestScore = score; best = name; }
  }
  
  console.log(`[STRATEGY] Fallback from ${failedStrategy} to ${best}`);
  forwardViaStrategy(req, res, best, targetHost, targetPort, targetPath);
}

const server = http.createServer(async (req, res) => {
  const parsed = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  const pathname = parsed.pathname || "/";

  // Handle CORS preflight
  if (req.method === "OPTIONS") {
    res.writeHead(204, CORS_HEADERS);
    res.end();
    return;
  }

  // 1. GET /logs -> SSE Stream or JSON snapshot
  if ((pathname === "/logs" || pathname === "/logs/") && req.method === "GET") {
    syncFile();
    const isSse = (req.headers.accept && req.headers.accept.includes("text/event-stream")) || parsed.searchParams.get("stream") === "true";

    if (isSse) {
      res.writeHead(200, { ...CORS_HEADERS, "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no" });
      const limit = parseInt(parsed.searchParams.get("limit") || "120", 10) || 120;
      for (const line of buffer.slice(-limit)) res.write(`data: ${JSON.stringify({ type: "line", text: line })}\n\n`);
      sseClients.add(res);
      const pingTimer = setInterval(() => { try { res.write(": keepalive\n\n"); } catch { clearInterval(pingTimer); sseClients.delete(res); } }, 15000);
      req.on("close", () => { clearInterval(pingTimer); sseClients.delete(res); });
      return;
    }

    const limit = parseInt(parsed.searchParams.get("limit") || "120", 10) || 120;
    res.writeHead(200, { ...CORS_HEADERS, "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache, no-store, must-revalidate" });
    res.end(JSON.stringify({ success: true, slot: SLOT, total_buffered: buffer.length, lines: buffer.slice(-limit), timestamp: Math.floor(Date.now() / 1000) }));
    return;
  }

  // 2. Clear logs
  if ((pathname === "/logs/clear" || pathname === "/logs" || pathname === "/clear") && (req.method === "POST" || req.method === "DELETE")) {
    buffer = []; fileOffset = 0;
    try { if (fs.existsSync(LOG_FILE)) fs.writeFileSync(LOG_FILE, ""); } catch {}
    broadcastSse({ type: "clear", message: "Runner log stream buffer cleared successfully." });
    res.writeHead(200, { ...CORS_HEADERS, "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify({ success: true, slot: SLOT, message: "Runner log stream buffer cleared successfully.", timestamp: Math.floor(Date.now() / 1000) }));
    return;
  }

  // 3. Gateway Health Probe
  if (pathname === "/gateway-health" && req.method === "GET") {
    res.writeHead(200, { ...CORS_HEADERS, "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok", slot: SLOT, port: PORT, target_port: TARGET_PORT, warp_enabled: strategyState.warpEnabled, current_strategy: strategyState.currentStrategy }));
    return;
  }

  // 3.5 Kilo channel health
  if (pathname === "/kilo/health" && req.method === "GET") {
    res.writeHead(200, { ...CORS_HEADERS, "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok", slot: SLOT, port: PORT, kilo_upstream: KILO_UPSTREAM, kilo_base_url_shape: "/kilo/v1", free_only_default: KILO_FREE_ONLY_DEFAULT, endpoints: { models: "GET /kilo/v1/models (?all=true to bypass free filter)", chat: "POST /kilo/v1/chat/completions" }, timestamp: Math.floor(Date.now() / 1000) }));
    return;
  }

  // 3.6 Kilo AI Gateway channel: /kilo/v1/* -> upstream via current strategy
  if (pathname === "/kilo/v1" || pathname === "/kilo/v1/" || pathname.startsWith("/kilo/v1/")) {
    let suffix = pathname.replace(/^\/kilo\/v1/, "") || "/";
    if (!suffix.startsWith("/")) suffix = `/${suffix}`;
    const upstreamUrl = `${KILO_UPSTREAM}${suffix}${parsed.search || ""}`;

    if (suffix.replace(/\/+$/, "") === "/models" && req.method === "GET") {
      const wantAll = parsed.searchParams.get("all") === "true";
      const filterFree = wantAll ? false : KILO_FREE_ONLY_DEFAULT;
      let upstreamParsed;
      try { upstreamParsed = new URL(upstreamUrl); } catch (e) { res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" }); res.end(JSON.stringify({ error: "Bad Gateway", message: `Invalid Kilo upstream URL: ${e.message}` })); return; }

      const strategy = selectBestStrategy();
      const agent = createStrategyAgent(strategy);
      
      const upstreamReq = https.request({
        hostname: upstreamParsed.hostname,
        port: upstreamParsed.port || 443,
        path: `${upstreamParsed.pathname}${upstreamParsed.search || ""}`,
        method: "GET",
        headers: { Accept: "application/json", Authorization: `Bearer ${KILO_API_KEY}`, "User-Agent": "9Router-KiloChannel/1.0" },
        agent,
      }, (upstreamRes) => {
        const chunks = [];
        upstreamRes.on("data", c => chunks.push(c));
        upstreamRes.on("end", () => {
          const raw = Buffer.concat(chunks).toString("utf-8");
          let payload = null;
          try { payload = raw ? JSON.parse(raw) : null; } catch { res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" }); res.end(JSON.stringify({ error: "Bad Gateway", message: "Kilo upstream returned non-JSON for /models." })); return; }
          let items = [];
          if (Array.isArray(payload)) items = payload;
          else if (payload && Array.isArray(payload.data)) items = payload.data;
          else if (payload && Array.isArray(payload.models)) items = payload.models;
          const total = items.length;
          let filtered = items;
          if (filterFree) filtered = items.filter(m => { const id = typeof m === "string" ? m : m && (m.id || m.name || ""); return /free/i.test(String(id || "")); });
          const data = filtered.map(m => { if (typeof m === "string") return { id: m, object: "model" }; if (m && typeof m === "object") { if (!m.id && m.name) m.id = String(m.name); if (!m.object) m.object = "model"; return m; } return { id: String(m), object: "model" }; });
          res.writeHead(upstreamRes.statusCode || 200, { ...CORS_HEADERS, "Content-Type": "application/json; charset=utf-8", "X-Kilo-Filtered": filterFree ? "free-only" : "all", "X-Kilo-Total": String(total), "X-Kilo-Returned": String(data.length), "X-Egress-Strategy": strategy });
          res.end(JSON.stringify({ object: "list", data }));
        });
      });
      upstreamReq.on("error", err => { res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" }); res.end(JSON.stringify({ error: "Bad Gateway", message: `Kilo /models unreachable: ${err.message}` })); });
      upstreamReq.end();
      return;
    }

    // Generic Kilo passthrough
    let upstreamParsed;
    try { upstreamParsed = new URL(upstreamUrl); } catch (e) { res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" }); res.end(JSON.stringify({ error: "Bad Gateway", message: `Invalid Kilo upstream URL: ${e.message}` })); return; }
    
    const strategy = selectBestStrategy();
    const agent = createStrategyAgent(strategy);
    
    const fwdHeaders = { ...req.headers };
    delete fwdHeaders.host; delete fwdHeaders["content-length"];
    fwdHeaders.authorization = `Bearer ${KILO_API_KEY}`;
    fwdHeaders["user-agent"] = "9Router-KiloChannel/1.0";
    fwdHeaders.accept = fwdHeaders.accept || "application/json";

    const kiloReq = https.request({
      hostname: upstreamParsed.hostname,
      port: upstreamParsed.port || 443,
      path: `${upstreamParsed.pathname}${upstreamParsed.search || ""}`,
      method: req.method,
      headers: fwdHeaders,
      agent,
    }, (kiloRes) => {
      const resHeaders = { ...kiloRes.headers };
      resHeaders["Access-Control-Allow-Origin"] = "*";
      resHeaders["X-Egress-Strategy"] = strategy;
      res.writeHead(kiloRes.statusCode, resHeaders);
      kiloRes.pipe(res);
    });
    kiloReq.on("error", err => { res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" }); res.end(JSON.stringify({ error: "Bad Gateway", message: `Kilo upstream unreachable: ${err.message}` })); });
    req.pipe(kiloReq);
    return;
  }

  // 4. Reverse-Proxy ALL other requests to 9Router via current strategy
  const strategy = selectBestStrategy();
  await forwardViaStrategy(req, res, strategy, "127.0.0.1", TARGET_PORT, req.url);
});

server.listen(PORT, "0.0.0.0", () => {
  console.log(`[GATEWAY] Active on port ${PORT} for slot '${SLOT}'. Upstream 9Router: ${TARGET_PORT}. Log file: ${LOG_FILE}`);
  console.log(`[GATEWAY] Strategies: direct, warp (${WARP_PORTS.length} instances), warp_ipv6. WARP enabled: ${strategyState.warpEnabled}`);
});
