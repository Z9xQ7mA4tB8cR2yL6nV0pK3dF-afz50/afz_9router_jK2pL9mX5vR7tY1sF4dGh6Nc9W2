/**
 * 9Router Local Node Gateway & Log Stream Server
 * ===============================================
 * Runs inside GitHub Actions runner VM on public node port (e.g. 6001, 6002).
 * - Routes GET /logs & GET /stream -> directly streams local runner log ring-buffer & file
 * - Routes POST /logs/clear & DELETE /logs -> resets local log buffer without restarting runner
 * - Routes /kilo/v1/* -> forwards to Kilo AI Gateway (OpenAI-compatible) using
 *   remote VM egress IP, so local PC gets per-node quota via subdomain.
 *   Local 9Router Base URL: https://<node-subdomain>/kilo/v1
 * - Routes all other requests (/v1/*, /models, etc.) -> transparently reverse-proxies to 9Router
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

// 9Router API Key for local engine auth (injected by workflow from seeded DB)
const ROUTER_API_KEY = getArg("--router-api-key", process.env.ROUTER_API_KEY || "");
console.log(`[GATEWAY] ROUTER_API_KEY loaded: ${ROUTER_API_KEY ? '[REDACTED]' : 'EMPTY'}`);

// Kilo AI Gateway channel (remote-egress quota multiplier).
// Local 9Router Base URL shape: https://<node-subdomain>/kilo/v1
// 9Router blindly appends /models and /chat/completions, so both must work.
const KILO_UPSTREAM = (
  getArg("--kilo-upstream", process.env.KILO_UPSTREAM || "https://api.kilo.ai/api/gateway")
).replace(/\/+$/, "");
// Free models answer without a key. Local 9Router sends its own Bearer key for
// the remote 9Router, which Kilo would reject, so replace it here.
// Set KILO_API_KEY env later for paid/BYOK usage.
const KILO_API_KEY = getArg("--kilo-api-key", process.env.KILO_API_KEY || "anonymous");
// Default free-only filtering for Import from /models. ?all=true bypasses it.
const KILO_FREE_ONLY_DEFAULT = String(
  getArg("--kilo-free-only", process.env.KILO_FREE_ONLY || "true")
).toLowerCase() === "true";

// OpenCode Proxy configuration
// Proxy port formula: 7005 + (slot_number * 10), e.g., node-1=7015, node-2=7025
const slotMatch = String(SLOT).match(/(\d+)$/);
const slotNum = slotMatch ? parseInt(slotMatch[1], 10) : 1;
const OPENCODE_PROXY_PORT = 7005 + (slotNum * 10);
const OPENCODE_PROXY_BASE = `http://127.0.0.1:${OPENCODE_PROXY_PORT}`;

// In-memory ring buffer for low latency

let buffer = [];
let fileOffset = 0;
const sseClients = new Set();

function broadcastSse(payload) {
  const data = `data: ${JSON.stringify(payload)}\n\n`;
  for (const client of sseClients) {
    try {
      client.write(data);
    } catch {
      sseClients.delete(client);
    }
  }
}

function syncFile() {
  try {
    if (!fs.existsSync(LOG_FILE)) return;
    const stat = fs.statSync(LOG_FILE);
    if (stat.size < fileOffset) {
      // File was truncated / cleared
      fileOffset = 0;
      buffer = [];
      broadcastSse({ type: "clear", message: "Runner log file cleared." });
    }
    if (stat.size > fileOffset) {
      const fd = fs.openSync(LOG_FILE, "r");
      const len = stat.size - fileOffset;
      const buf = Buffer.alloc(len);
      fs.readSync(fd, buf, 0, len, fileOffset);
      fs.closeSync(fd);
      fileOffset = stat.size;

      const newLines = buf
        .toString("utf-8")
        .split("\n")
        .map((l) => l.trimEnd())
        .filter(Boolean);

      buffer.push(...newLines);
      if (buffer.length > 1000) {
        buffer = buffer.slice(-1000);
      }

      for (const line of newLines) {
        broadcastSse({ type: "line", text: line });
      }
    }
  } catch (e) {
    // ignore read errors
  }
}

// Keep synced from disk every 250ms for low latency
setInterval(syncFile, 250);

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization, x-admin-key, Accept, Origin",
};

const server = http.createServer((req, res) => {
  const parsed = new URL(req.url, `http://${req.headers.host || "localhost"}`);
  const pathname = parsed.pathname || "/";

  // Handle CORS preflight
  if (req.method === "OPTIONS") {
    res.writeHead(204, CORS_HEADERS);
    res.end();
    return;
  }

  // 1. GET /logs -> Supports both True SSE Stream and Standard JSON snapshot
  if ((pathname === "/logs" || pathname === "/logs/") && req.method === "GET") {
    syncFile();
    const isSse =
      (req.headers.accept && req.headers.accept.includes("text/event-stream")) ||
      parsed.searchParams.get("stream") === "true";

    if (isSse) {
      res.writeHead(200, {
        ...CORS_HEADERS,
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
      });

      // Send buffered history immediately to new subscriber
      const limit = parseInt(parsed.searchParams.get("limit") || "120", 10) || 120;
      const history = buffer.slice(-limit);
      for (const line of history) {
        res.write(`data: ${JSON.stringify({ type: "line", text: line })}\n\n`);
      }

      sseClients.add(res);

      // Heartbeat ping every 15s to keep tunnel connection active
      const pingTimer = setInterval(() => {
        try {
          res.write(": keepalive\n\n");
        } catch {
          clearInterval(pingTimer);
          sseClients.delete(res);
        }
      }, 15000);

      req.on("close", () => {
        clearInterval(pingTimer);
        sseClients.delete(res);
      });
      return;
    }

    // Standard JSON snapshot (for browser address bar / curl)
    const limit = parseInt(parsed.searchParams.get("limit") || "120", 10) || 120;
    const lines = buffer.slice(-limit);

    res.writeHead(200, {
      ...CORS_HEADERS,
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-cache, no-store, must-revalidate",
    });
    res.end(
      JSON.stringify({
        success: true,
        slot: SLOT,
        total_buffered: buffer.length,
        lines,
        timestamp: Math.floor(Date.now() / 1000),
      })
    );
    return;
  }

  // 2. Clear logs: POST /logs/clear or DELETE /logs
  if (
    (pathname === "/logs/clear" || pathname === "/logs" || pathname === "/clear") &&
    (req.method === "POST" || req.method === "DELETE")
  ) {
    buffer = [];
    fileOffset = 0;
    try {
      if (fs.existsSync(LOG_FILE)) {
        fs.writeFileSync(LOG_FILE, "");
      }
    } catch {}

    broadcastSse({
      type: "clear",
      message: "Runner log stream buffer cleared successfully.",
    });

    res.writeHead(200, {
      ...CORS_HEADERS,
      "Content-Type": "application/json; charset=utf-8",
    });
    res.end(
      JSON.stringify({
        success: true,
        slot: SLOT,
        message: "Runner log stream buffer cleared successfully.",
        timestamp: Math.floor(Date.now() / 1000),
      })
    );
    return;
  }

  // 3. Gateway Health Probe
  if (pathname === "/gateway-health" && req.method === "GET") {
    res.writeHead(200, { ...CORS_HEADERS, "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        status: "ok",
        slot: SLOT,
        port: PORT,
        target_port: TARGET_PORT,
      })
    );
    return;
  }

  // 3.5 Kilo channel health (diagnostics, no upstream call)
  if (pathname === "/kilo/health" && req.method === "GET") {
    res.writeHead(200, { ...CORS_HEADERS, "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        status: "ok",
        slot: SLOT,
        port: PORT,
        kilo_upstream: KILO_UPSTREAM,
        kilo_base_url_shape: "/kilo/v1",
        free_only_default: KILO_FREE_ONLY_DEFAULT,
        endpoints: {
          models: "GET /kilo/v1/models (?all=true to bypass free filter)",
          chat: "POST /kilo/v1/chat/completions",
        },
        timestamp: Math.floor(Date.now() / 1000),
      })
    );
    return;
  }

  // 3.6 Kilo AI Gateway channel: /kilo/v1/* -> https://api.kilo.ai/api/gateway/*
  // Result-focused: remote VM egress IP is used, so each node gets its own
  // anonymous quota (n x 200/hr) instead of sharing the single local PC IP.
  if (pathname === "/kilo/v1" || pathname === "/kilo/v1/" || pathname.startsWith("/kilo/v1/")) {
    let suffix = pathname.replace(/^\/kilo\/v1/, "") || "/";
    if (!suffix.startsWith("/")) suffix = `/${suffix}`;
    const upstreamUrl = `${KILO_UPSTREAM}${suffix}${parsed.search || ""}`;

    // Small helper: GET /kilo/v1/models with free-only filtering for Import.
    if (suffix.replace(/\/+$/, "") === "/models" && req.method === "GET") {
      const wantAll = parsed.searchParams.get("all") === "true";
      const filterFree = wantAll ? false : KILO_FREE_ONLY_DEFAULT;
      let upstreamParsed;
      try {
        upstreamParsed = new URL(upstreamUrl);
      } catch (e) {
        res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "Bad Gateway", message: `Invalid Kilo upstream URL: ${e.message}` }));
        return;
      }
      const upstreamReq = https.request(
        {
          hostname: upstreamParsed.hostname,
          port: upstreamParsed.port || 443,
          path: `${upstreamParsed.pathname}${upstreamParsed.search || ""}`,
          method: "GET",
          headers: {
            Accept: "application/json",
            Authorization: `Bearer ${KILO_API_KEY}`,
            "User-Agent": "9Router-KiloChannel/1.0",
          },
        },
        (upstreamRes) => {
          const chunks = [];
          upstreamRes.on("data", (c) => chunks.push(c));
          upstreamRes.on("end", () => {
            const raw = Buffer.concat(chunks).toString("utf-8");
            let payload = null;
            try {
              payload = raw ? JSON.parse(raw) : null;
            } catch {
              // Upstream returned non-JSON; pass through as-is with 502 shape
              res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" });
              res.end(JSON.stringify({ error: "Bad Gateway", message: "Kilo upstream returned non-JSON for /models." }));
              return;
            }
            // Normalize shapes to OpenAI { data: [{id,...}] } for 9Router Import.
            let items = [];
            if (Array.isArray(payload)) items = payload;
            else if (payload && Array.isArray(payload.data)) items = payload.data;
            else if (payload && Array.isArray(payload.models)) items = payload.models;
            const total = items.length;
            let filtered = items;
            if (filterFree) {
              filtered = items.filter((m) => {
                const id = typeof m === "string" ? m : m && (m.id || m.name || "");
                return /free/i.test(String(id || ""));
              });
            }
            const data = filtered.map((m) => {
              if (typeof m === "string") return { id: m, object: "model" };
              if (m && typeof m === "object") {
                if (!m.id && m.name) m.id = String(m.name);
                if (!m.object) m.object = "model";
                return m;
              }
              return { id: String(m), object: "model" };
            });
            res.writeHead(upstreamRes.statusCode || 200, {
              ...CORS_HEADERS,
              "Content-Type": "application/json; charset=utf-8",
              "X-Kilo-Filtered": filterFree ? "free-only" : "all",
              "X-Kilo-Total": String(total),
              "X-Kilo-Returned": String(data.length),
            });
            res.end(JSON.stringify({ object: "list", data }));
          });
        }
      );
      upstreamReq.on("error", (err) => {
        res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "Bad Gateway", message: `Kilo /models unreachable: ${err.message}` }));
      });
      upstreamReq.end();
      return;
    }

    // Generic Kilo passthrough (chat/completions with SSE streaming, etc.)
    // Auth replaced: local 9Router key would be rejected by Kilo, use KILO_API_KEY.
    let upstreamParsed;
    try {
      upstreamParsed = new URL(upstreamUrl);
    } catch (e) {
      res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "Bad Gateway", message: `Invalid Kilo upstream URL: ${e.message}` }));
      return;
    }
    const fwdHeaders = { ...req.headers };
    delete fwdHeaders.host;
    delete fwdHeaders["content-length"];
    fwdHeaders.authorization = `Bearer ${KILO_API_KEY}`;
    fwdHeaders["user-agent"] = "9Router-KiloChannel/1.0";
    fwdHeaders.accept = fwdHeaders.accept || "application/json";

    const kiloReq = https.request(
      {
        hostname: upstreamParsed.hostname,
        port: upstreamParsed.port || 443,
        path: `${upstreamParsed.pathname}${upstreamParsed.search || ""}`,
        method: req.method,
        headers: fwdHeaders,
      },
      (kiloRes) => {
        const resHeaders = { ...kiloRes.headers };
        resHeaders["Access-Control-Allow-Origin"] = "*";
        res.writeHead(kiloRes.statusCode, resHeaders);
        kiloRes.pipe(res);
      }
    );
    kiloReq.on("error", (err) => {
      res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "Bad Gateway", message: `Kilo upstream unreachable: ${err.message}` }));
    });
    req.pipe(kiloReq);
    return;
  }

  // 3.7 OpenCode Proxy channel: /open/v1/* -> Local OpenCode Proxy (Dynamic Port)
  // Result-focused: Local OpenCode Proxy on dynamic port per slot.
  // OpenCode Proxy Base URL shape: http://localhost:<proxy_port>/v1/*
  // Proxy port = 7005 + (slot_number * 10), e.g., node-1 = 7015, node-2 = 7025
  if (pathname === "/open/v1" || pathname === "/open/v1/" || pathname.startsWith("/open/v1/")) {
    // Calculate proxy port based on SLOT: 7005 + (slot_number * 10)
    // node-1 -> 7015, node-2 -> 7025, node-3 -> 7035, etc.
    const slotMatch = String(SLOT).match(/(\d+)$/);
    const slotNum = slotMatch ? parseInt(slotMatch[1], 10) : 1;
    const proxyPort = 7005 + (slotNum * 10);
    
    let suffix = pathname.replace(/^\/open\/v1/, "") || "/";
    if (!suffix.startsWith("/")) suffix = `/${suffix}`;
    
    // Ensure path starts with /v1/ for OpenCode Proxy
    if (!suffix.startsWith("/v1/")) {
      suffix = `/v1${suffix}`;
    }
    
    const upstreamUrl = `http://127.0.0.1:${proxyPort}${suffix}${parsed.search || ""}`;
    
    // Small helper: GET /open/v1/models with free-only filtering for Import.
    if (suffix.replace(/\/+$/, "") === "/v1/models" && req.method === "GET") {
      const wantAll = parsed.searchParams.get("all") === "true";
      const filterFree = wantAll ? false : true; // Default to free-only for OpenCode
      let upstreamParsed;
      try {
        upstreamParsed = new URL(upstreamUrl);
      } catch (e) {
        res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "Bad Gateway", message: `Invalid OpenCode Proxy URL: ${e.message}` }));
        return;
      }
      const upstreamReq = http.request(
        {
          hostname: upstreamParsed.hostname,
          port: upstreamParsed.port || proxyPort,
          path: `${upstreamParsed.pathname}${upstreamParsed.search || ""}`,
          method: "GET",
          headers: {
            Accept: "application/json",
            "User-Agent": "9Router-OpenCodeChannel/1.0",
          },
        },
        (upstreamRes) => {
          const chunks = [];
          upstreamRes.on("data", (c) => chunks.push(c));
          upstreamRes.on("end", () => {
            const raw = Buffer.concat(chunks).toString("utf-8");
            let payload = null;
            try {
              payload = raw ? JSON.parse(raw) : null;
            } catch {
              res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" });
              res.end(JSON.stringify({ error: "Bad Gateway", message: "OpenCode Proxy upstream returned non-JSON for /models." }));
              return;
            }
            // Normalize shapes to OpenAI { data: [{id,...}] } for 9Router Import.
            let items = [];
            if (Array.isArray(payload)) items = payload;
            else if (payload && Array.isArray(payload.data)) items = payload.data;
            else if (payload && Array.isArray(payload.models)) items = payload.models;
            const total = items.length;
            let filtered = items;
            if (filterFree) {
              filtered = items.filter((m) => {
                const id = typeof m === "string" ? m : m && (m.id || m.name || "");
                return /free/i.test(String(id || ""));
              });
            }
            const data = filtered.map((m) => {
              if (typeof m === "string") return { id: m, object: "model" };
              if (m && typeof m === "object") {
                if (!m.id && m.name) m.id = String(m.name);
                if (!m.object) m.object = "model";
                return m;
              }
              return { id: String(m), object: "model" };
            });
            res.writeHead(upstreamRes.statusCode || 200, {
              ...CORS_HEADERS,
              "Content-Type": "application/json; charset=utf-8",
              "X-OpenCode-Filtered": filterFree ? "free-only" : "all",
              "X-OpenCode-Total": String(total),
              "X-OpenCode-Returned": String(data.length),
            });
            res.end(JSON.stringify({ object: "list", data }));
          });
        });
        upstreamReq.on("error", (err) => {
          res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" });
          res.end(JSON.stringify({ error: "Bad Gateway", message: `OpenCode Proxy /models unreachable: ${err.message}` }));
        });
        upstreamReq.end();
return;
      }

    // 3.7.1 OpenCode Chat Completions via Proxy: POST /open/v1/chat/completions
    // Uses the proxy's chat/completions endpoint (same as direct curl to 7015)
    if (suffix.replace(/\/+$/, "") === "/v1/chat/completions" && req.method === "POST") {
      // Read body first, then handle asynchronously
      let body = "";
      req.on("data", (chunk) => { body += chunk; });
      req.on("end", () => {
        (async () => {
          let payload;
          try {
            payload = JSON.parse(body);
          } catch {
            res.writeHead(400, { ...CORS_HEADERS, "Content-Type": "application/json" });
            return res.end(JSON.stringify({ error: "Invalid JSON body" }));
          }

          const model = payload.model || "glm-5.3-flash-free";
          const messages = payload.messages || [];
          if (!messages.length) {
            res.writeHead(400, { ...CORS_HEADERS, "Content-Type": "application/json" });
            return res.end(JSON.stringify({ error: "messages array required" }));
          }

          // Forward to OpenCode Proxy's chat/completions endpoint
          try {
            const proxyRes = await fetch(`${OPENCODE_PROXY_BASE}/v1/chat/completions`, {
              method: "POST",
              headers: {
                "content-type": "application/json",
                ...(OPENCODE_PROXY_AUTH ? { authorization: OPENCODE_PROXY_AUTH } : {})
              },
              body: JSON.stringify({ model, messages })
            });

            const data = await proxyRes.json();
            
            res.writeHead(proxyRes.status, {
              ...CORS_HEADERS,
              "Content-Type": "application/json; charset=utf-8",
            });
            res.end(JSON.stringify(data));

          } catch (err) {
            console.error("[GATEWAY] OpenCode chat error:", err.message);
            res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" });
            res.end(JSON.stringify({ error: "OpenCode execution failed", details: err.message }));
          }
        })();
      });
      return;
    }

    // For all other /open/v1/* routes, use generic passthrough
// For all other /open/v1/* routes, use generic passthrough
      // No auth replacement needed - OpenCode Proxy handles its own auth (none needed)
      let upstreamParsed;
      try {
        upstreamParsed = new URL(upstreamUrl);
      } catch (e) {
        res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: "Bad Gateway", message: `Invalid OpenCode Proxy upstream URL: ${e.message}` }));
        return;
      }
      const fwdHeaders = { ...req.headers };
      delete fwdHeaders.host;
      delete fwdHeaders["content-length"];
      fwdHeaders["user-agent"] = "9Router-OpenCodeChannel/1.0";
      fwdHeaders.accept = fwdHeaders.accept || "application/json";

    const openCodeReq = http.request(
      {
        hostname: upstreamParsed.hostname,
        port: upstreamParsed.port || proxyPort,
        path: `${upstreamParsed.pathname}${upstreamParsed.search || ""}`,
        method: req.method,
        headers: fwdHeaders,
      },
      (openCodeRes) => {
        const resHeaders = { ...openCodeRes.headers };
        resHeaders["Access-Control-Allow-Origin"] = "*";
        res.writeHead(openCodeRes.statusCode, resHeaders);
        openCodeRes.pipe(res);
      }
    );
    openCodeReq.on("error", (err) => {
      res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: "Bad Gateway", message: `OpenCode Proxy upstream unreachable: ${err.message}` }));
    });
    req.pipe(openCodeReq);
    return;
  }

  // 4. Reverse-Proxy ALL other requests to 9Router on TARGET_PORT
  const headers = { ...req.headers };
  headers.host = `127.0.0.1:${TARGET_PORT}`;

  // Inject 9Router API key if available and no Authorization header present
  if (ROUTER_API_KEY && !headers.authorization) {
    headers.authorization = `Bearer ${ROUTER_API_KEY}`;
    console.log(`[GATEWAY] Injected Authorization header for ${req.url}`);
  } else if (!ROUTER_API_KEY) {
    console.log(`[GATEWAY] WARNING: ROUTER_API_KEY is empty, cannot inject auth for ${req.url}`);
  }

  const proxyReq = http.request(
    {
      hostname: "127.0.0.1",
      port: TARGET_PORT,
      path: req.url,
      method: req.method,
      headers: headers,
    },
    (proxyRes) => {
      console.log(`[GATEWAY] Proxied ${req.method} ${req.url} -> 9Router:${TARGET_PORT} (status: ${proxyRes.statusCode})`);
      const resHeaders = { ...proxyRes.headers };
      resHeaders["Access-Control-Allow-Origin"] = "*";
      res.writeHead(proxyRes.statusCode, resHeaders);
      proxyRes.pipe(res);
    }
  );

  proxyReq.on("error", (err) => {
    res.writeHead(502, { ...CORS_HEADERS, "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "Bad Gateway",
        message: `Gateway failed to reach internal 9Router on port ${TARGET_PORT}: ${err.message}`,
      })
    );
  });

  req.pipe(proxyReq);
});

server.listen(PORT, "0.0.0.0", async () => {
  console.log(
    `[GATEWAY] Active on port ${PORT} for slot '${SLOT}'. Upstream 9Router: ${TARGET_PORT}. Log file: ${LOG_FILE}`
  );
  

});
