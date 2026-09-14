/**
 * 9Router Local Node Gateway & Log Stream Server
 * ===============================================
 * Runs inside GitHub Actions runner VM on public node port (e.g. 6001, 6002).
 * - Routes GET /logs & GET /stream -> directly streams local runner log ring-buffer & file
 * - Routes POST /logs/clear & DELETE /logs -> resets local log buffer without restarting runner
 * - Routes all other requests (/v1/*, /models, etc.) -> transparently reverse-proxies to 9Router
 */

const http = require("http");
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

// In-memory ring buffer for low latency
let buffer = [];
let fileOffset = 0;

function syncFile() {
  try {
    if (!fs.existsSync(LOG_FILE)) return;
    const stat = fs.statSync(LOG_FILE);
    if (stat.size < fileOffset) {
      // File was truncated / cleared
      fileOffset = 0;
      buffer = [];
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
    }
  } catch (e) {
    // ignore read errors
  }
}

// Keep synced from disk every 400ms
setInterval(syncFile, 400);

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

  // 1. GET /logs or /stream
  if ((pathname === "/logs" || pathname === "/stream" || pathname === "/logs/") && req.method === "GET") {
    syncFile();
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

  // 4. Reverse-Proxy ALL other requests to 9Router on TARGET_PORT
  const headers = { ...req.headers };
  headers.host = `127.0.0.1:${TARGET_PORT}`;

  const proxyReq = http.request(
    {
      hostname: "127.0.0.1",
      port: TARGET_PORT,
      path: req.url,
      method: req.method,
      headers: headers,
    },
    (proxyRes) => {
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

server.listen(PORT, "0.0.0.0", () => {
  console.log(
    `[GATEWAY] Active on port ${PORT} for slot '${SLOT}'. Upstream 9Router: ${TARGET_PORT}. Log file: ${LOG_FILE}`
  );
});
