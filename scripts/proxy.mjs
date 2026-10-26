#!/usr/bin/env node
import http from "node:http"
import { mkdirSync } from "node:fs"
import { randomUUID } from "node:crypto"

const UPSTREAM = process.env.OPENCODE_URL || "http://127.0.0.1:4096"
const USERNAME = process.env.OPENCODE_USERNAME || "opencode"
const PASSWORD = process.env.OPENCODE_PASSWORD || process.env.OPENCODE_SERVER_PASSWORD || ""
const PORT = Number(process.env.PORT || 4097)
const HOST = process.env.HOST || "127.0.0.1"
const DEFAULT_MODEL = process.env.MODEL || "opencode/deepseek-v4.1-flash-free"
const DEFAULT_VARIANT = process.env.VARIANT || "max"
const AGENT = process.env.OPENCODE_AGENT || process.env.PROXY_AGENT || ""
const TOOLS_MODE = process.env.TOOLS_MODE || "safe"
const SANDBOX_DIR = process.env.SANDBOX_DIR || "/tmp/opencode-chat"
const TIMEOUT_MS = Number(process.env.REQUEST_TIMEOUT_MS || 120000)
const START_TIMEOUT_MS = Number(process.env.START_TIMEOUT_MS || 15000)
const POLL_MS = Number(process.env.POLL_MS || 350)
const KEEP_SESSIONS = process.env.KEEP_SESSIONS === "1"
const PROXY_API_KEY = process.env.PROXY_API_KEY || ""

const AUTH = PASSWORD
  ? "Basic " + Buffer.from(`${USERNAME}:${PASSWORD}`).toString("base64")
  : ""

const DISABLED_TOOLS = ["bash", "edit", "write", "apply_patch", "task", "webfetch", "websearch", "question"]

mkdirSync(SANDBOX_DIR, { recursive: true })

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

class ProxyError extends Error {
  constructor(status, message, type = "proxy_error", code = null, extra = null) {
    super(message)
    this.status = status
    this.type = type
    this.code = code
    this.extra = extra
  }
}

async function upstream(method, path, body = undefined, timeout = 30000) {
  const headers = { "content-type": "application/json" }
  if (AUTH) headers.authorization = AUTH
  let response
  try {
    response = await fetch(UPSTREAM + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(timeout),
    })
  } catch (error) {
    if (error.name === "TimeoutError" || error.name === "AbortError") {
      throw new ProxyError(504, "opencode upstream timeout", "upstream_timeout", "timeout")
    }
    throw new ProxyError(502, `opencode upstream unreachable: ${error.message}`, "upstream_error", "unreachable")
  }
  const raw = await response.text()
  let data = null
  if (raw) {
    try {
      data = JSON.parse(raw)
    } catch {
      data = raw
    }
  }
  if (!response.ok) {
    const detail =
      (data && data.message) ||
      (data && data.error && data.error.message) ||
      (typeof data === "string" ? data.slice(0, 500) : null) ||
      `HTTP ${response.status}`
    throw new ProxyError(502, `opencode upstream error: ${detail}`, "upstream_error", String(response.status))
  }
  return data
}

function normalizeModel(name) {
  const value = String(name || DEFAULT_MODEL).trim()
  const slash = value.indexOf("/")
  if (slash === -1) return { providerID: "opencode", modelID: value }
  return { providerID: value.slice(0, slash), modelID: value.slice(slash + 1) }
}

function mapEffort(effort) {
  switch (String(effort || "").toLowerCase()) {
    case "minimal":
    case "low":
      return "low"
    case "medium":
      return "high"
    case "high":
      return "high"
    case "xhigh":
    case "max":
      return "max"
    default:
      return ""
  }
}

function contentText(content) {
  if (typeof content === "string") return content
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (!part) return ""
        if (part.type === "text") return part.text || ""
        if (part.type === "input_text") return part.text || ""
        if (part.type === "image_url") return "[image omitted]"
        return ""
      })
      .filter(Boolean)
      .join("\n")
  }
  return ""
}

function buildPrompt(messages) {
  const systems = []
  const conversation = []
  for (const message of messages) {
    const text = contentText(message?.content)
    if (!text) continue
    if (message.role === "system" || message.role === "developer") systems.push(text)
    else conversation.push({ role: message.role || "user", text })
  }
  let prompt
  if (conversation.length === 1 && conversation[0].role === "user") prompt = conversation[0].text
  else
    prompt = conversation
      .map((message) => `${message.role.toUpperCase()}: ${message.text}`)
      .join("\n\n")
  return { system: systems.join("\n\n"), prompt }
}

function textOf(message) {
  return (message.parts || [])
    .filter((part) => part && part.type === "text" && !part.ignored && !part.synthetic)
    .map((part) => part.text || "")
    .join("")
}

function usageOf(info) {
  const tokens = info?.tokens || {}
  const cached = tokens.cache?.read || 0
  const input = tokens.input || 0
  const output = (tokens.output || 0) + (tokens.reasoning || 0)
  return {
    prompt_tokens: input + cached,
    completion_tokens: output,
    total_tokens: tokens.total || input + cached + output,
  }
}

function finishOf(info) {
  if (info?.finish === "length") return "length"
  if (info?.finish === "content_filter") return "content_filter"
  return "stop"
}

async function sessionStatus(sessionID) {
  const all = await upstream("GET", "/session/status")
  if (!all || typeof all !== "object") return null
  return all[sessionID] || null
}

function sendJSON(res, status, payload) {
  if (res.headersSent) return
  const body = JSON.stringify(payload)
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "access-control-allow-origin": "*",
  })
  res.end(body)
}

function sendError(res, error) {
  const status = error instanceof ProxyError ? error.status : 500
  const type = error instanceof ProxyError ? error.type : "internal_error"
  const payload = {
    error: {
      message: error.message || "internal error",
      type,
      code: (error instanceof ProxyError && error.code) || null,
    },
  }
  if (error instanceof ProxyError && error.extra) payload.error.action = error.extra
  if (res.headersSent) {
    res.write(`data: ${JSON.stringify(payload)}\n\n`)
    res.write("data: [DONE]\n\n")
    res.end()
    return
  }
  sendJSON(res, status, payload)
}

function writeSSE(res, payload) {
  res.write(`data: ${JSON.stringify(payload)}\n\n`)
}

async function deleteSession(sessionID) {
  if (!sessionID || KEEP_SESSIONS) return
  try {
    await upstream("DELETE", `/session/${sessionID}`, undefined, 10000)
  } catch {}
}

async function abortSession(sessionID) {
  if (!sessionID) return
  try {
    await upstream("POST", `/session/${sessionID}/abort`, {}, 10000)
  } catch {}
}

let modelCache = { at: 0, list: [] }

async function modelList() {
  if (Date.now() - modelCache.at < 60000 && modelCache.list.length) return modelCache.list
  try {
    const providers = await upstream("GET", "/config/providers")
    const list = []
    for (const provider of providers?.providers || []) {
      for (const [modelID, model] of Object.entries(provider.models || {})) {
        const released = model?.release_date ? Date.parse(model.release_date) : 0
        list.push({
          id: modelID,
          object: "model",
          created: Number.isFinite(released) ? Math.floor(released / 1000) : 0,
          owned_by: provider.id,
          provider: provider.id,
        })
      }
    }
    if (list.length) modelCache = { at: Date.now(), list }
    return list.length ? list : modelCache.list
  } catch (error) {
    // Keep serving the last known list during a transient upstream failure.
    if (modelCache.list.length) return modelCache.list
    throw error
  }
}

async function defaultModelId() {
  try {
    const list = await modelList()
    if (!list.length) return DEFAULT_MODEL
    const bare = String(DEFAULT_MODEL).includes("/")
      ? String(DEFAULT_MODEL).slice(String(DEFAULT_MODEL).indexOf("/") + 1)
      : String(DEFAULT_MODEL)
    const exact = list.find((model) => model.id === bare)
    const chosen = exact || list.find((model) => /-free$/.test(model.id)) || list[0]
    return `${chosen.provider}/${chosen.id}`
  } catch {
    return DEFAULT_MODEL
  }
}

class SilentStartError extends Error {}

async function handleChat(res, body) {
  const messages = body?.messages
  if (!Array.isArray(messages) || messages.length === 0) {
    throw new ProxyError(400, "messages must be a non-empty array", "invalid_request_error")
  }
  const { system, prompt } = buildPrompt(messages)
  if (!prompt.trim()) {
    throw new ProxyError(400, "no text content in messages", "invalid_request_error")
  }

  const requestedModel = String(body.model || (await defaultModelId()))
  const { providerID, modelID } = normalizeModel(requestedModel)
  if (!providerID || !modelID) {
    throw new ProxyError(400, "invalid model", "invalid_request_error")
  }

  let variant = ""
  if (body.variant) variant = String(body.variant)
  else if (body.reasoning_effort) variant = mapEffort(body.reasoning_effort)
  else if (providerID === "opencode" && /-free$/.test(modelID)) variant = DEFAULT_VARIANT

  const streaming = body.stream === true
  const includeUsage = streaming && body.stream_options?.include_usage === true
  const firstUser = messages.find((message) => message?.role === "user")
  const title = "api: " + (contentText(firstUser?.content) || "chat").replace(/\s+/g, " ").slice(0, 60)

  const payload = {
    model: { providerID, modelID },
    parts: [{ type: "text", text: prompt }],
  }
  if (variant) payload.variant = variant
  if (system) payload.system = system
  if (AGENT) payload.agent = AGENT
  if (TOOLS_MODE !== "full") {
    payload.tools = Object.fromEntries(DISABLED_TOOLS.map((tool) => [tool, false]))
  }

  let closed = false
  let finished = false
  res.on("close", () => {
    if (!finished) closed = true
  })

  const created = Math.floor(Date.now() / 1000)
  const completionID = "chatcmpl-" + randomUUID().replace(/-/g, "").slice(0, 24)
  const deadline = Date.now() + TIMEOUT_MS
  let attempt = 0

  while (attempt < 2 && !closed) {
    attempt++
    const query = new URLSearchParams({ directory: SANDBOX_DIR })
    const session = await upstream("POST", `/session?${query.toString()}`, { title })
    const sessionID = session?.id
    if (!sessionID) throw new ProxyError(502, "opencode did not return a session id", "upstream_error")
    if (process.env.DEBUG_PROXY === "1") {
      console.log("DEBUG create", JSON.stringify(session))
      console.log("DEBUG prompt", JSON.stringify(payload))
    }
    const sessionStarted = Date.now()
    let sentText = ""
    let headersStarted = false
    let silentStart = false

    const startStream = () => {
      res.writeHead(200, {
        "content-type": "text/event-stream; charset=utf-8",
        "cache-control": "no-cache, no-transform",
        connection: "keep-alive",
        "x-accel-buffering": "no",
        "access-control-allow-origin": "*",
      })
      headersStarted = true
      writeSSE(res, {
        id: completionID,
        object: "chat.completion.chunk",
        created,
        model: requestedModel,
        choices: [{ index: 0, delta: { role: "assistant", content: "" }, finish_reason: null }],
      })
    }

    try {
      await upstream("POST", `/session/${sessionID}/prompt_async`, payload)

      while (true) {
        if (closed) return
        if (Date.now() > deadline) {
          throw new ProxyError(504, "timed out waiting for model response", "upstream_timeout", "timeout")
        }

        const status = await sessionStatus(sessionID)
        if (status && status.type === "retry") {
          throw new ProxyError(
            502,
            status.message || "provider retry in progress",
            "upstream_error",
            status.action?.reason || "retry",
            status.action || null,
          )
        }

        const messagesOut = await upstream("GET", `/session/${sessionID}/message`, undefined, 30000)
        const assistants = (messagesOut || []).filter((message) => message?.info?.role === "assistant")

        if (assistants.length === 0) {
          if (!status && Date.now() - sessionStarted > START_TIMEOUT_MS) {
            throw new SilentStartError("session did not start")
          }
          await sleep(POLL_MS)
          continue
        }

        const fullText = assistants.map(textOf).join("")
        const lastInfo = assistants[assistants.length - 1].info
        if (lastInfo?.error) {
          const message =
            lastInfo.error.message || lastInfo.error.error || JSON.stringify(lastInfo.error).slice(0, 500)
          throw new ProxyError(502, message, "upstream_error", "model_error")
        }

        if (streaming && !headersStarted) startStream()

        if (streaming && fullText.length > sentText.length) {
          const delta = fullText.slice(sentText.length)
          sentText = fullText
          writeSSE(res, {
            id: completionID,
            object: "chat.completion.chunk",
            created,
            model: requestedModel,
            choices: [{ index: 0, delta: { content: delta }, finish_reason: null }],
          })
        }

        if (lastInfo.time?.completed && !status) {
          const usage = usageOf(lastInfo)
          const finishReason = finishOf(lastInfo)
          if (streaming) {
            if (!headersStarted) startStream()
            writeSSE(res, {
              id: completionID,
              object: "chat.completion.chunk",
              created,
              model: requestedModel,
              choices: [{ index: 0, delta: {}, finish_reason: finishReason }],
            })
            if (includeUsage) {
              writeSSE(res, {
                id: completionID,
                object: "chat.completion.chunk",
                created,
                model: requestedModel,
                choices: [],
                usage,
              })
            }
            res.write("data: [DONE]\n\n")
            res.end()
          } else {
            sendJSON(res, 200, {
              id: completionID,
              object: "chat.completion",
              created,
              model: requestedModel,
              choices: [
                {
                  index: 0,
                  message: { role: "assistant", content: fullText },
                  finish_reason: finishReason,
                },
              ],
              usage,
            })
          }
          finished = true
          return
        }

        await sleep(POLL_MS)
      }
    } catch (error) {
      if (error instanceof SilentStartError && attempt < 2 && !headersStarted && !closed) {
        silentStart = true
      } else {
        throw error
      }
    } finally {
      if (!finished) await abortSession(sessionID)
      await deleteSession(sessionID)
    }

    if (silentStart) continue
  }

  if (closed) return
  throw new ProxyError(502, "opencode session did not start", "upstream_error", "silent_start")
}

async function readBody(req) {
  let size = 0
  const chunks = []
  for await (const chunk of req) {
    size += chunk.length
    if (size > 2_000_000) throw new ProxyError(413, "request body too large", "invalid_request_error")
    chunks.push(chunk)
  }
  if (size === 0) return {}
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"))
  } catch {
    throw new ProxyError(400, "invalid JSON body", "invalid_request_error")
  }
}

const TRUST_LOOPBACK = process.env.TRUST_LOOPBACK !== "0"

function isLoopback(req) {
  const address = req.socket.remoteAddress || ""
  return address === "127.0.0.1" || address === "::1" || address === "::ffff:127.0.0.1"
}

function authorized(req) {
  if (!PROXY_API_KEY) return true
  if (TRUST_LOOPBACK && isLoopback(req)) return true
  return (
    req.headers.authorization === `Bearer ${PROXY_API_KEY}` ||
    req.headers["x-api-key"] === PROXY_API_KEY
  )
}

const server = http.createServer(async (req, res) => {
  const started = Date.now()
  const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`)
  const path = url.pathname
  const route = path === "/open" ? "/" : path.startsWith("/open/") ? path.slice("/open".length) : path
  if (process.env.DEBUG_HEADERS === "1") {
    console.log("DEBUG_HEADERS", req.method, path, req.socket.remoteAddress, JSON.stringify(req.headers))
  }
  try {
    if (req.method === "OPTIONS") {
      res.writeHead(204, {
        "access-control-allow-origin": "*",
        "access-control-allow-headers": "authorization, content-type",
        "access-control-allow-methods": "GET, POST, OPTIONS",
      })
      res.end()
      return
    }

    const isHealth = req.method === "GET" && (route === "/health" || route === "/v1/health")
    const isRoot = req.method === "GET" && (route === "/" || route === "/v1" || route === "/v1/")

    if (!authorized(req) && !isHealth && !isRoot) {
      throw new ProxyError(401, "invalid proxy api key", "authentication_error")
    }

    if (isHealth) {
      let upstreamHealth = null
      try {
        upstreamHealth = await upstream("GET", "/global/health", undefined, 5000)
      } catch (error) {
        upstreamHealth = { ok: false, error: error.message }
      }
      sendJSON(res, 200, {
        ok: true,
        upstream: UPSTREAM,
        model: DEFAULT_MODEL,
        variant: DEFAULT_VARIANT,
        tools_mode: TOOLS_MODE,
        upstream_health: upstreamHealth,
      })
      return
    }

    if (req.method === "GET" && (route === "/v1/models" || route === "/models")) {
      const data = await modelList()
      sendJSON(res, 200, { object: "list", data })
      return
    }

    if (
      req.method === "POST" &&
      (route === "/v1/chat/completions" ||
        route === "/chat/completions" ||
        route === "/v1/chat" ||
        route === "/chat")
    ) {
      const body = await readBody(req)
      await handleChat(res, body)
      return
    }

    if (req.method === "GET" && (route === "/" || route === "/v1" || route === "/v1/")) {
      sendJSON(res, 200, {
        name: "opencode-openai-proxy",
        endpoints: [
          "GET /health",
          "GET /v1/models",
          "POST /v1/chat/completions",
          "POST /v1/chat",
        ],
        models: "GET /v1/models",
        default_model: DEFAULT_MODEL,
        port: PORT,
      })
      return
    }

    throw new ProxyError(404, "not found", "invalid_request_error")
  } catch (error) {
    console.error(`${new Date().toISOString()} ERROR ${req.method} ${path}: ${error.message}`)
    sendError(res, error)
  } finally {
    const status = res.statusCode
    console.log(`${new Date().toISOString()} ${req.method} ${path} ${status} ${Date.now() - started}ms`)
  }
})

async function warmup() {
  try {
    const query = new URLSearchParams({ directory: SANDBOX_DIR })
    const session = await upstream("POST", `/session?${query.toString()}`, { title: "proxy-warmup" }, 30000)
    await sleep(1000)
    if (session?.id) await upstream("DELETE", `/session/${session.id}`, undefined, 10000)
    console.log("upstream sandbox warmed")
  } catch (error) {
    console.error("warmup failed:", error.message)
  }
}

warmup().finally(() => {
  server.listen(PORT, HOST, () => {
    console.log(`opencode proxy listening on http://${HOST}:${PORT}`)
    console.log(`upstream=${UPSTREAM} model=${DEFAULT_MODEL} variant=${DEFAULT_VARIANT} tools=${TOOLS_MODE}`)
    if (PROXY_API_KEY && TRUST_LOOPBACK) {
      console.log("WARN: TRUST_LOOPBACK=1 — loopback requests bypass PROXY_API_KEY")
    }
    if (HOST !== "127.0.0.1" && HOST !== "::1" && HOST !== "localhost") {
      console.log(`WARN: HOST=${HOST} is not loopback — firewall/auth required`)
    }
  })
})
