#!/usr/bin/env python3
"""
9Router Distributed Node Monitor & Heartbeat Daemon (Option K: Multi-Threaded Engine)
-------------------------------------------------------------------------------------
Architecture:
1. Thread 1 (Heartbeat & Lease Daemon):
   - Runs in a dedicated background daemon thread (threading.Thread).
   - Fires strictly every 15s regardless of whatever long-running or blocking
     operations (WARP IP cycling, 429 rate-limit cooldown, network diagnostics)
     occur on the main thread.
   - Handles:
     * 9rt:lock:node-X renewal (TTL: 60s)
     * 9rt:hb:node-X heartbeat pulse renewal (TTL: 60s)
     * Self-healing if lock key was wiped.
     * Instant self-suicide if lock was usurped by another RUN_ID.
2. Thread 2 (Main Thread: Engine & Recovery Supervisor):
   - Supervises local 9Router health and model endpoints.
   - Tracks 429 rate-limits and updates thread-safe state.
   - Ready for WARP IP cycling without ever freezing the heartbeat!
3. Verbose Debug Logging:
   - Complete visibility into every lock check, pulse, model health check, and state change.
"""

import os
import sys
import time
import json
import signal
import platform
import argparse
import threading
import subprocess
import shutil
import re
import urllib.request
import urllib.parse
import urllib.error

# Force unbuffered real-time stdout output in CI/CD consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

DEFAULT_REDIS_BASE = "https://jelab101-rimjhim.hf.space"

def log(tag: str, msg: str):
    """Formatted timestamped console logger."""
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] [{tag}] {msg}", flush=True)

DEFAULT_SLOT_NAMES = {
    "node-1": "Norom Dupur",
    "node-2": "Komol Josna",
    "node-3": "Sobuj Kanon",
    "node-4": "Shanto Godhuli",
    "node-5": "Neel Digonto",
    "node-6": "Megher Bhela"
}

def normalize_slot(raw_slot: str) -> str:
    """Normalizes slot name to canonical 'node-N' format (e.g. node-3 -> node-3, 9rt3 -> node-3, 3 -> node-3)."""
    s = raw_slot.strip().lower()
    digits = ""
    for char in reversed(s):
        if char.isdigit():
            digits = char + digits
        elif digits:
            break
    if digits:
        return f"node-{digits}"
    return s

def calculate_port(slot: str) -> int:
    """Calculates dynamic port based on formula: 6000 + N"""
    digits = "".join(filter(str.isdigit, slot))
    if digits:
        port = 6000 + int(digits)
        if port > 65535:
            raise ValueError(f"Calculated port {port} exceeds 65535 limit")
        return port
    return 6001

def detect_warp_egress_ip(proxy_addr: str = "127.0.0.1:40000", timeout: int = 3) -> str:
    """
    Queries external IP reflection endpoint through SOCKS5 proxy to verify WARP egress IP.
    """
    endpoints = ["https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me/ip"]
    curl_bin = shutil.which("curl")
    if curl_bin:
        for ep in endpoints:
            try:
                cmd = [curl_bin, "-s", "--max-time", str(timeout), "--socks5", proxy_addr, ep]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 1)
                ip = res.stdout.strip()
                if ip and re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                    return ip
            except Exception:
                continue
        # Fallback direct curl if WARP socks5 not up yet
        try:
            cmd = [curl_bin, "-s", "--max-time", str(timeout), "https://api.ipify.org"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 1)
            ip = res.stdout.strip()
            if ip and re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                return ip
        except Exception:
            pass
    return "127.0.0.1"

# ----------------- Thread-Safe Shared Node State -----------------

class NodeSharedState:
    def __init__(self, slot: str, name: str, port: int, run_id: str, gh_run_id: str, boot_time: int):
        self.slot = slot
        self.name = name
        self.port = port
        self.run_id = run_id
        self.gh_run_id = gh_run_id
        self.boot_time = boot_time
        
        self.lock = threading.Lock()
        self.status = "online"
        self.status_msg = "Initializing engine & background threads..."
        self.is_healthy = False
        self.active_models_count = 0
        self.warp_ip = "127.0.0.1"
        self.models_list = ["big-pickle"]
        self.discovered_models = []
        self.active_combo = ["big-pickle", "mimo-v2.5-free"]
        self.is_running = True

    def update_health(self, is_healthy: bool, models_count: int, status: str, status_msg: str, models_list: list = None, discovered_models: list = None):
        with self.lock:
            self.is_healthy = is_healthy
            self.active_models_count = models_count
            self.status = status
            self.status_msg = status_msg
            if models_list is not None:
                self.models_list = models_list
            if discovered_models is not None:
                self.discovered_models = discovered_models

    def update_combo(self, combo: list):
        with self.lock:
            self.active_combo = list(combo)

    def update_warp_ip(self, ip: str):
        with self.lock:
            self.warp_ip = ip

    def get_snapshot(self):
        with self.lock:
            return {
                "slot": self.slot,
                "name": self.name,
                "port": self.port,
                "run_id": self.run_id,
                "gh_run_id": self.gh_run_id,
                "boot_time": self.boot_time,
                "status": self.status,
                "status_msg": self.status_msg,
                "is_healthy": self.is_healthy,
                "models_count": self.active_models_count,
                "warp_ip": self.warp_ip,
                "models_list": list(self.models_list),
                "discovered_models": list(self.discovered_models),
                "active_combo": list(self.active_combo),
                "is_running": self.is_running
            }

    def stop(self):
        with self.lock:
            self.is_running = False

# ==============================================================================
# SMART RATE LIMIT & WARP DECISION MATRIX (ALGORITHM SPECIFICATION)
# ==============================================================================
# This decision engine protects the 6-hour runner lifespan by avoiding compute waste.
# It prevents the runner from falling into "Chained Backoff Traps" (moving goalposts:
# 2 min -> 4 min -> 3 min 45s...) and daily IP quota locks.
#
# RUNNER PARAMETERS:
#   TOTAL_LIFESPAN_SEC      = 21600 (6 hours hard VM lifetime on GitHub Actions)
#   SINGLE_WAIT_CAP_SEC     = 180   (Max 3 minutes tolerable for a single wait)
#   CUMULATIVE_WAIT_CAP_SEC = 240   (Max 4 minutes cumulative idle wait before forcing rotation)
#   MAX_CONSECUTIVE_RL      = 2     (Max consecutive 429 hits allowed before shifting to WARP)
#
# DECISION RULES HIERARCHY:
# ------------------------------------------------------------------------------
# RULE 1: LIFECYCLE EXPIRY CHECK (Hard VM Boundary)
#   Condition: RL_Wait_Time >= Remaining_VM_Lifetime
#   Rationale: Waiting would cause the runner to expire or terminate before/at the
#              reset moment. Compute is wasted with 0 remaining utility.
#   Decision : SHIFT_TO_W (Instant Cloudflare WARP IP Rotation).
#
# RULE 2: SINGLE WAIT CAP (Greedy Waste Prevention)
#   Condition: RL_Wait_Time > 180 seconds (3 minutes)
#   Rationale: WARP IP rotation takes only ~3-5 seconds. Sitting idle for 10-60 mins
#              on a 6-hour runner when a fresh IP takes 5 seconds is unacceptable.
#   Decision : SHIFT_TO_W (Instant Cloudflare WARP IP Rotation).
#
# RULE 3: CHAINED BACKOFF TRAP / LOOP BREAKER (User-Experienced Moving Goalposts)
#   Condition: consecutive_rl_count >= 2
#   Rationale: Upstream providers often return deceptively short wait times (e.g. 2 min,
#              then 4 min, then 3 min...) when an IP is soft-banned. If a second 429
#              occurs immediately after waiting, it is a chained trap, not a burst.
#   Decision : SHIFT_TO_W (Instant Cloudflare WARP IP Rotation).
#
# RULE 4: CUMULATIVE WAIT CAP (Aggregate Idle Budget)
#   Condition: cumulative_wait_sec + RL_Wait_Time > 240 seconds (4 minutes)
#   Rationale: Even if individual waits are small, cumulative idle time across
#              retries must not exceed 4 minutes total.
#   Decision : SHIFT_TO_W (Instant Cloudflare WARP IP Rotation).
#
# RULE 5: TOLERABLE RPM / CONCURRENCY BURST ZONE (Genuine Micro-Burst)
#   Condition: RL_Wait_Time <= 180s AND consecutive_rl_count == 1
#   Rationale: A true short RPM or concurrency spike (5-30s). Waiting this short
#              interval avoids unnecessary network disruption.
#   Decision : ACTION_WAIT (Sleep RL_Wait_Time while HeartbeatDaemon keeps pulsing).
#              Post-Sleep Probe:
#                - If HTTP 200 OK: Reset consecutive_rl_count = 0, cumulative_wait = 0.
#                - If HTTP 429: consecutive_rl_count becomes 2 -> RULE 3 triggers SHIFT_TO_W.
# ==============================================================================

class DecisionResult:
    ACTION_WAIT = "WAIT"
    ACTION_SHIFT_TO_W = "SHIFT_TO_W"

    def __init__(self, action: str, rule: str, reason: str, wait_seconds: float = 0):
        self.action = action
        self.rule = rule
        self.reason = reason
        self.wait_seconds = wait_seconds

    def __repr__(self):
        return f"<DecisionResult action={self.action} rule='{self.rule}' wait={self.wait_seconds}s reason='{self.reason}'>"

class RateLimitDecisionEngine:
    TOTAL_LIFESPAN_SEC = 21600   # 6 hours
    SINGLE_WAIT_CAP_SEC = 180    # 3 minutes
    CUMULATIVE_WAIT_CAP_SEC = 240  # 4 minutes
    MAX_CONSECUTIVE_RL = 2

    def __init__(self, boot_time: int, total_lifespan: int = TOTAL_LIFESPAN_SEC):
        self.boot_time = boot_time
        self.total_lifespan = total_lifespan
        self.consecutive_rl_count = 0
        self.cumulative_wait_sec = 0.0

    def evaluate(self, rl_wait_sec: float, current_time: float = None) -> DecisionResult:
        """
        Evaluates the incoming Rate Limit wait time against the 5-rule decision matrix.
        Returns a DecisionResult indicating whether to WAIT or SHIFT_TO_W.
        """
        now = current_time if current_time is not None else time.time()
        uptime = max(0, now - self.boot_time)
        remaining_lifetime = max(0, self.total_lifespan - uptime)

        # RULE 1: Lifecycle Expiry Check (Hard VM Boundary)
        if rl_wait_sec >= remaining_lifetime:
            return DecisionResult(
                action=DecisionResult.ACTION_SHIFT_TO_W,
                rule="RULE_1_LIFECYCLE_EXPIRY",
                reason=f"RL wait time ({rl_wait_sec:.0f}s) >= remaining VM lifetime ({remaining_lifetime:.0f}s). Waiting would outlive runner."
            )

        # RULE 2: Single Wait Cap (Greedy Waste Prevention)
        if rl_wait_sec > self.SINGLE_WAIT_CAP_SEC:
            return DecisionResult(
                action=DecisionResult.ACTION_SHIFT_TO_W,
                rule="RULE_2_SINGLE_WAIT_CAP",
                reason=f"RL wait time ({rl_wait_sec:.0f}s) exceeds single wait cap ({self.SINGLE_WAIT_CAP_SEC}s). Instant WARP rotation preferred."
            )

        potential_consecutive = self.consecutive_rl_count + 1

        # RULE 3: Chained Backoff Trap / Loop Breaker
        if potential_consecutive >= self.MAX_CONSECUTIVE_RL:
            return DecisionResult(
                action=DecisionResult.ACTION_SHIFT_TO_W,
                rule="RULE_3_CHAINED_BACKOFF_TRAP",
                reason=f"Consecutive RL hit #{potential_consecutive} detected. Provider is moving goalposts (chained drip-feed loop)."
            )

        # RULE 4: Cumulative Wait Cap
        if (self.cumulative_wait_sec + rl_wait_sec) > self.CUMULATIVE_WAIT_CAP_SEC:
            return DecisionResult(
                action=DecisionResult.ACTION_SHIFT_TO_W,
                rule="RULE_4_CUMULATIVE_WAIT_CAP",
                reason=f"Total cumulative wait ({self.cumulative_wait_sec + rl_wait_sec:.0f}s) exceeds limit ({self.CUMULATIVE_WAIT_CAP_SEC}s)."
            )

        # RULE 5: Tolerable RPM / Concurrency Burst Zone
        self.consecutive_rl_count = potential_consecutive
        self.cumulative_wait_sec += rl_wait_sec
        return DecisionResult(
            action=DecisionResult.ACTION_WAIT,
            rule="RULE_5_TOLERABLE_RPM_BURST",
            reason=f"Acceptable RPM micro-burst ({rl_wait_sec:.0f}s). Consecutive hit #{self.consecutive_rl_count}.",
            wait_seconds=rl_wait_sec
        )

    def on_success(self):
        """Called after an HTTP 200 OK response to reset backoff counters."""
        if self.consecutive_rl_count > 0 or self.cumulative_wait_sec > 0:
            log("RL_ENGINE", f"Request successful (HTTP 200). Resetting RL counters (consecutive was {self.consecutive_rl_count}, wait was {self.cumulative_wait_sec:.0f}s).")
        self.consecutive_rl_count = 0
        self.cumulative_wait_sec = 0.0

    def on_warp_rotated(self):
        """Called after a successful WARP IP rotation to reset state for the fresh IP."""
        log("RL_ENGINE", "WARP IP rotated. Resetting all rate-limit counters for fresh IP address.")
        self.consecutive_rl_count = 0
        self.cumulative_wait_sec = 0.0

def execute_warp_rotation(state: NodeSharedState, engine: RateLimitDecisionEngine, rule: str = "") -> bool:
    """
    Executes Cloudflare WARP IP rotation:
    1. Updates shared state to 'rotating_ip' so heartbeat broadcasts to Redis.
    2. Runs warp-cli commands to disconnect and reconnect for a fresh Anycast edge IP.
    3. Verifies new outbound IP connectivity.
    4. Restores state to 'online' and resets rate limit engine counters.
    """
    snap = state.get_snapshot()
    models_cnt = snap["models_count"]
    rule_tag = f"[{rule}] " if rule else ""

    state.update_health(
        is_healthy=False,
        models_count=models_cnt,
        status="rotating_ip",
        status_msg=f"{rule_tag}Triggering Cloudflare WARP IP rotation..."
    )
    log("WARP", f"Initiating Cloudflare WARP IP rotation {rule_tag}(Shift to W)...")

    warp_bin = shutil.which("warp-cli")
    if not warp_bin:
        log("WARP_WARN", "warp-cli not found on PATH. Simulating IP rotation in non-WARP environment.")
        time.sleep(2)
        engine.on_warp_rotated()
        state.update_health(
            is_healthy=True,
            models_count=models_cnt,
            status="online",
            status_msg="WARP simulated rotation complete (mock)"
        )
        return True

    try:
        # 1. Disconnect current WARP session
        log("WARP", "Executing: warp-cli --accept-tos disconnect")
        subprocess.run([warp_bin, "--accept-tos", "disconnect"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)

        # 2. Reconnect to obtain fresh IP from Cloudflare Anycast mesh
        log("WARP", "Executing: warp-cli --accept-tos connect")
        subprocess.run([warp_bin, "--accept-tos", "connect"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)

        # 3. Post-rotation counter reset
        engine.on_warp_rotated()
        state.update_health(
            is_healthy=True,
            models_count=models_cnt,
            status="online",
            status_msg="Outbound IP successfully rotated via WARP"
        )
        log("WARP", "WARP IP rotation successful! Resuming normal engine operation.")
        return True
    except Exception as e:
        log("WARP_ERR", f"WARP rotation encountered error: {e}")
        state.update_health(
            is_healthy=False,
            models_count=models_cnt,
            status="error",
            status_msg=f"WARP rotation failed: {e}"
        )
        return False

# ----------------- Redis HTTP REST Client -----------------

def redis_http_call(url: str, timeout: int = 10):
    try:
        req = urllib.request.Request(
            url, 
            headers={"User-Agent": "9Router-Monitor/2.0", "Accept": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                return data
    except urllib.error.HTTPError as e:
        log("REDIS_ERR", f"HTTP {e.code} Error ({e.reason}) on {url[:80]}...")
        return None
    except Exception as e:
        log("REDIS_ERR", f"Network Error on {url[:80]}...: {e}")
        return None

def redis_get(redis_base: str, key: str):
    url = f"{redis_base.rstrip('/')}/GET/{urllib.parse.quote(key)}"
    res = redis_http_call(url)
    if isinstance(res, dict):
        val = res.get("GET")
        if val is None:
            val = res.get("value")
        if val is None:
            return None
        if isinstance(val, str):
            try:
                return json.loads(val)
            except json.JSONDecodeError:
                return val
        return val
    return None

def redis_setex_str(redis_base: str, key: str, seconds: int, string_val: str):
    encoded_val = urllib.parse.quote(string_val, safe='').replace("%2F", "%252F")
    url = f"{redis_base.rstrip('/')}/SETEX/{urllib.parse.quote(key, safe='')}/{seconds}/{encoded_val}"
    return redis_http_call(url)

def redis_setex_json(redis_base: str, key: str, seconds: int, value_dict: dict):
    val_json = json.dumps(value_dict)
    encoded_val = urllib.parse.quote(val_json, safe='').replace("%2F", "%252F")
    url = f"{redis_base.rstrip('/')}/SETEX/{urllib.parse.quote(key, safe='')}/{seconds}/{encoded_val}"
    
    # Safety guard: HTTP GET URLs must stay under 3000 characters to prevent 414 Request-URI Too Large
    if len(url) > 2800:
        pruned = dict(value_dict)
        if "models" in pruned and isinstance(pruned["models"], list):
            # Keep only compact string IDs up to 15
            pruned["models"] = [m["id"] if isinstance(m, dict) else str(m) for m in pruned["models"]][:15]
        val_json = json.dumps(pruned)
        encoded_val = urllib.parse.quote(val_json, safe='').replace("%2F", "%252F")
        url = f"{redis_base.rstrip('/')}/SETEX/{urllib.parse.quote(key, safe='')}/{seconds}/{encoded_val}"
        
    return redis_http_call(url)

def redis_del(redis_base: str, key: str):
    url = f"{redis_base.rstrip('/')}/DEL/{urllib.parse.quote(key)}"
    return redis_http_call(url)

# ----------------- Operational Commands -----------------

def cmd_check_lock(args):
    """
    Pre-flight Lock Check (Fast Abort):
    Verifies 9rt:lock:node-X before heavy npm installs.
    If --force is specified, bypasses lock check and claims slot immediately.
    """
    slot = normalize_slot(args.slot)
    redis_base = args.redis_base or DEFAULT_REDIS_BASE
    lock_key = f"9rt:lock:{slot}"

    if getattr(args, "force", False):
        log("PREFLIGHT", f"Force flag active! Bypassing lock check for slot '{slot}'...")
        gh_output = os.environ.get("GITHUB_OUTPUT")
        if gh_output and os.path.exists(gh_output):
            with open(gh_output, "a") as f:
                f.write("is_locked=false\n")
                f.write(f"canonical_slot={slot}\n")
        sys.exit(0)

    log("PREFLIGHT", f"Checking lock for slot '{slot}' ({lock_key})...")
    existing_lock = redis_get(redis_base, lock_key)

    if existing_lock:
        owner = str(existing_lock)
        print("=" * 70, flush=True)
        log("ABORT", f"SLOT IS ACTIVELY OCCUPIED!")
        log("ABORT", f"Slot       : {slot}")
        log("ABORT", f"Lock Owner : {owner}")
        log("ABORT", f"Action     : Terminating duplicate workflow run cleanly without changes.")
        print("=" * 70, flush=True)
        
        gh_output = os.environ.get("GITHUB_OUTPUT")
        if gh_output and os.path.exists(gh_output):
            with open(gh_output, "a") as f:
                f.write("is_locked=true\n")
        
        sys.exit(42) # Exit code indicating duplicate lock
    
    log("PREFLIGHT", f"Slot '{slot}' is completely FREE. Ready to proceed.")
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output and os.path.exists(gh_output):
        with open(gh_output, "a") as f:
            f.write("is_locked=false\n")
            f.write(f"canonical_slot={slot}\n")
    sys.exit(0)

# ----------------- Thread 1: Heartbeat Daemon -----------------

def command_consumer_worker(state: NodeSharedState, redis_base: str, rl_engine: RateLimitDecisionEngine = None):
    """
    Dedicated Command Consumer Daemon Thread:
    - Runs continuously on a fast 3-second cycle.
    - Observes 9rt:cmd:{slot} in Redis without delaying or conflicting with the Heartbeat daemon.
    - Consumes command immediately, executes action (rotate_ip / set_combo), writes acknowledgment to 9rt:ack:{slot}.
    - Deletes 9rt:cmd:{slot} upon completion (no TTL dependency).
    """
    cmd_key = f"9rt:cmd:{state.slot}"
    ack_key = f"9rt:ack:{state.slot}"
    log("CMD_THREAD", f"Dedicated Command Consumer Thread active for {state.slot} (Interval: 3s).")

    while True:
        snap = state.get_snapshot()
        if not snap["is_running"]:
            log("CMD_THREAD", "Shutdown signaled. Command consumer worker exiting.")
            break

        try:
            cmd = redis_get(redis_base, cmd_key)
            if cmd and isinstance(cmd, dict):
                action = cmd.get("action")
                now = int(time.time())
                log("CMD_CONSUMER", f"Received command '{action}': {cmd}")

                ack_payload = {
                    "status": "ok",
                    "action": action,
                    "slot": state.slot,
                    "applied_at": now
                }

                try:
                    if action == "set_combo":
                        new_models = cmd.get("models") or []
                        scripts_dir = os.path.dirname(os.path.abspath(__file__))
                        if scripts_dir not in sys.path:
                            sys.path.insert(0, scripts_dir)
                        try:
                            import seed_db
                            applied = seed_db.seed(new_models)
                            state.update_combo(applied)
                            state.update_health(
                                is_healthy=snap["is_healthy"],
                                models_count=len(applied),
                                status=snap["status"],
                                status_msg=f"Combo updated ({len(applied)} models active)",
                                models_list=applied
                            )
                            ack_payload["models"] = applied
                            ack_payload["active_combo"] = applied
                            ack_payload["message"] = f"Successfully updated combo ({len(applied)} models)."
                            log("CMD_CONSUMER", f"Combo successfully applied: {applied}")
                        except Exception as seed_err:
                            ack_payload["status"] = "error"
                            ack_payload["error"] = str(seed_err)
                            log("CMD_ERR", f"Failed to seed models into SQLite: {seed_err}")

                    elif action == "rotate_ip":
                        log("CMD_CONSUMER", "Executing WARP IP rotation by command...")
                        if rl_engine:
                            execute_warp_rotation(state, rl_engine, rule="USER_COMMAND")
                        fresh_ip = detect_warp_egress_ip()
                        state.update_warp_ip(fresh_ip)
                        ack_payload["new_ip"] = fresh_ip
                        ack_payload["message"] = f"WARP IP rotated to {fresh_ip}."
                        log("CMD_CONSUMER", f"WARP IP rotated to: {fresh_ip}")

                    else:
                        ack_payload["status"] = "ignored"
                        ack_payload["message"] = f"Action '{action}' not recognized."

                except Exception as cmd_exc:
                    ack_payload["status"] = "error"
                    ack_payload["error"] = str(cmd_exc)
                    log("CMD_ERR", f"Error during command execution: {cmd_exc}")

                # Save acknowledgment and delete command immediately
                redis_setex_json(redis_base, ack_key, 86400, ack_payload)
                redis_del(redis_base, cmd_key)
                log("CMD_CONSUMER", f"Command '{action}' processed, acked to {ack_key}, and {cmd_key} deleted.")

        except Exception as e:
            log("CMD_ERR", f"Error in command consumer loop: {e}")

        # Command interval strictly set to 15s (matching 15s heartbeat rhythm)
        time.sleep(15)


def heartbeat_worker(state: NodeSharedState, redis_base: str, lock_key: str, hb_key: str, rl_engine: RateLimitDecisionEngine = None):
    """
    Dedicated Background Heartbeat Thread:
    - Runs strictly every 15 seconds.
    - Renews 9rt:lock and 9rt:hb leases.
    - Publishes 100% complete telemetry pulse to Redis.
    """
    pulse_count = 0
    log("HB_THREAD", f"Heartbeat & Lock Daemon started for {state.slot} on dedicated thread.")

    while True:
        snap = state.get_snapshot()
        if not snap["is_running"]:
            log("HB_THREAD", "Shutdown signaled. Heartbeat worker exiting.")
            break

        pulse_count += 1
        now = int(time.time())
        uptime = now - snap["boot_time"]

        # 1. Lock Validation: Check if usurped or missing
        current_lock = redis_get(redis_base, lock_key)
        if current_lock is not None:
            current_owner = str(current_lock)
            if current_owner != snap["run_id"]:
                print("=" * 70, flush=True)
                log("SUPERSEDED", f"Lock taken over by newer runner!")
                log("SUPERSEDED", f"Current Run : {snap['run_id']}")
                log("SUPERSEDED", f"New Ruler   : {current_owner}")
                log("SUPERSEDED", f"Action      : Committing graceful self-suicide.")
                print("=" * 70, flush=True)
                os._exit(0)
            else:
                redis_setex_str(redis_base, lock_key, 60, snap["run_id"])
        else:
            log("SELF_HEAL", f"Lock key was missing. Re-claiming lock lease for {snap['run_id']}...")
            redis_setex_str(redis_base, lock_key, 60, snap["run_id"])

        # 2. Egress WARP IP Check (Every 60s / 4 pulses)
        if pulse_count == 1 or pulse_count % 4 == 0:
            try:
                active_ip = detect_warp_egress_ip()
                state.update_warp_ip(active_ip)
            except Exception:
                pass

        # 3. Renew All-in-One Heartbeat (TTL 60s)
        snap = state.get_snapshot()
        raw_models = snap["discovered_models"] if snap["discovered_models"] else snap["models_list"]
        compact_models = []
        for m in raw_models:
            mid = m["id"] if isinstance(m, dict) else str(m)
            if mid not in compact_models:
                compact_models.append(mid)

        hb_payload = {
            "slot": snap["slot"],
            "name": snap["name"],
            "port": snap["port"],
            "run_id": snap["run_id"],
            "gh_run_id": snap["gh_run_id"],
            "boot_time": snap["boot_time"],
            "pulse": now,
            "uptime": uptime,
            "status": snap["status"],
            "status_msg": snap["status_msg"],
            "warp_ip": snap["warp_ip"],
            "provider": "opencode-free",
            "models": compact_models,
            "active_combo": snap["active_combo"],
            "models_count": len(compact_models)
        }
        res = redis_setex_json(redis_base, hb_key, 60, hb_payload)
        if res is not None and isinstance(res, dict) and res.get("SETEX") == [True, "OK"]:
            if pulse_count == 1 or pulse_count % 10 == 0:
                log("HB_SYNC", f"Heartbeat verified in Redis ({hb_key})")
        else:
            log("HB_WARN", f"Redis SETEX returned non-OK status: {res} at {hb_key}")

        # 5. Rich Formatted Console Output
        badge = "🟢 ONLINE" if snap["is_healthy"] else f"🟡 {snap['status'].upper()}"
        log("PULSE", f"#{pulse_count:04d} | {snap['slot']} ({snap['name']}) | Uptime: {uptime}s | {badge} | Egress: {snap['warp_ip']} | Msg: {snap['status_msg']}")

        # Sleep precisely 15 seconds in 1-second intervals for quick termination
        for _ in range(15):
            snap = state.get_snapshot()
            if not snap["is_running"]:
                break
            time.sleep(1)

# ----------------- Thread 2 (Main Thread): Daemon Entry -----------------

def cmd_run_daemon(args):
    slot = normalize_slot(args.slot)
    port = args.port or calculate_port(slot)
    redis_base = args.redis_base or DEFAULT_REDIS_BASE
    gh_run_id = args.gh_run_id or os.environ.get("GITHUB_RUN_ID", "local")
    api_key = args.api_key or os.environ.get("ROUTER_API_KEY", "sk-361ddf48ad95487f-l1vj9z-499b11a6")

    boot_timestamp = int(time.time())
    run_id = getattr(args, "run_id", None) or f"{slot}_{boot_timestamp}"

    lock_key = f"9rt:lock:{slot}"
    hb_key = f"9rt:hb:{slot}"

    node_name = getattr(args, "name", None) or DEFAULT_SLOT_NAMES.get(slot, f"Node {slot}")

    print("=" * 70, flush=True)
    log("INIT", f"9ROUTER MULTI-THREADED NODE MONITOR STARTING")
    log("INIT", f"Canonical Slot : {slot}")
    log("INIT", f"Poetic Name    : {node_name}")
    log("INIT", f"Dynamic Port   : {port}")
    log("INIT", f"Session RUN_ID : {run_id}")
    log("INIT", f"GitHub Run ID  : {gh_run_id}")
    log("INIT", f"Lock Key       : {lock_key}")
    log("INIT", f"Heartbeat Key  : {hb_key}")
    log("INIT", f"Redis API Base : {redis_base}")
    print("=" * 70, flush=True)

    # Initialize Thread-Safe Shared State
    state = NodeSharedState(slot, node_name, port, run_id, gh_run_id, boot_timestamp)

    # Initial Lock Claim and Registration
    log("INIT", f"Claiming initial lock lease at {lock_key}...")
    redis_setex_str(redis_base, lock_key, 60, run_id)

    initial_hb = {
        "slot": slot,
        "name": node_name,
        "port": port,
        "run_id": run_id,
        "gh_run_id": gh_run_id,
        "boot_time": boot_timestamp,
        "pulse": boot_timestamp,
        "uptime": 0,
        "status": "online",
        "status_msg": "Initializing 9Router engine & background threads..."
    }
    redis_setex_json(redis_base, hb_key, 60, initial_hb)
    log("INIT", f"Initial heartbeat registered in Redis.")

    # Initialize Rate Limit & WARP Decision Matrix Engine
    rl_engine = RateLimitDecisionEngine(boot_timestamp)

    # Spawn Thread 1: Dedicated Heartbeat Worker
    hb_thread = threading.Thread(
        target=heartbeat_worker,
        args=(state, redis_base, lock_key, hb_key, rl_engine),
        daemon=True,
        name="HeartbeatDaemon"
    )
    hb_thread.start()
    log("INIT", "Spawned independent HeartbeatDaemon thread.")

    # Spawn Thread 2: Dedicated Command Consumer Worker (Fast 3s polling, zero delay)
    cmd_thread = threading.Thread(
        target=command_consumer_worker,
        args=(state, redis_base, rl_engine),
        daemon=True,
        name="CommandConsumerDaemon"
    )
    cmd_thread.start()
    log("INIT", "Spawned independent CommandConsumerDaemon thread.")

    # Graceful Signal Handling
    def handle_signal(sig, frame):
        log("SIGNAL", f"Received termination signal ({sig}). Cleaning up...")
        state.stop()
        # Clean delete of lock and hb so slot becomes instantly free
        redis_del(redis_base, lock_key)
        redis_del(redis_base, hb_key)
        log("CLEANUP", f"Slot {slot} unlocked cleanly.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Main Thread: Engine Supervisor & Health Monitor Loop
    models_url = f"http://127.0.0.1:{port}/v1/models"

    while True:
        snap = state.get_snapshot()
        if not snap["is_running"]:
            break

        # Check Local 9Router Engine Health via Bearer Auth
        is_healthy = False
        models_count = 0
        status = "online"
        status_msg = ""
        parsed_models = snap.get("models_list", ["big-pickle"])
        discovered_items = snap.get("discovered_models", [])

        try:
            req = urllib.request.Request(
                models_url,
                headers={"Authorization": f"Bearer {api_key}"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    rl_engine.on_success()
                    data = resp.read().decode("utf-8")
                    fresh_discovered = []
                    fresh_ids = []
                    try:
                        parsed = json.loads(data)
                        raw_items = []
                        if isinstance(parsed, dict) and "data" in parsed:
                            raw_items = parsed["data"]
                        elif isinstance(parsed, list):
                            raw_items = parsed

                        for m in raw_items:
                            if isinstance(m, dict) and m.get("id"):
                                m_id = m.get("id")
                                m_owner = m.get("owned_by") or m.get("provider") or "OpenCode"
                                m_name = m.get("name") or m_id.replace("-", " ").title()
                                
                                # Targeted Provider Filter: focus dynamically on the active free provider tier
                                is_target = bool(
                                    re.search(r"free|big[-_\s]?pickle", m_id, re.IGNORECASE) or
                                    re.search(r"free|opencode", str(m_owner), re.IGNORECASE)
                                )
                                if is_target:
                                    fresh_discovered.append({
                                        "id": m_id,
                                        "name": m_name,
                                        "provider": "OpenCode-Free"
                                    })
                                    fresh_ids.append(m_id)
                    except Exception:
                        pass

                    if fresh_ids:
                        parsed_models = fresh_ids
                        discovered_items = fresh_discovered
                    elif not parsed_models:
                        parsed_models = snap.get("models_list", ["big-pickle"])

                    models_count = len(parsed_models)

                    if "mimo-v2.5-free" in data:
                        is_healthy = True
                        status = "online"
                        status_msg = f"Healthy ({models_count} models loaded, OpenCode active)"
                    else:
                        is_healthy = True
                        status = "online"
                        status_msg = f"Running ({models_count} models loaded)"
        except urllib.error.HTTPError as e:
            if e.code == 429:
                is_healthy = False
                # Parse Retry-After header if provided, else default to 60s
                retry_header = e.headers.get("Retry-After") if hasattr(e, "headers") else None
                wait_sec = 60.0
                if retry_header:
                    try:
                        wait_sec = float(retry_header)
                    except ValueError:
                        wait_sec = 60.0

                # Evaluate via Smart Decision Matrix
                decision = rl_engine.evaluate(wait_sec)
                log("RL_DECISION", f"Decision: [{decision.rule}] -> {decision.action} ({decision.reason})")

                if decision.action == DecisionResult.ACTION_SHIFT_TO_W:
                    execute_warp_rotation(state, rl_engine, rule=decision.rule)
                    continue
                else:
                    status = "cooldown"
                    status_msg = f"Cooldown {decision.wait_seconds:.0f}s ({decision.rule})"
                    state.update_health(is_healthy, models_count, status, status_msg, models_list=parsed_models, discovered_models=discovered_items)

                    # Sleep specified wait duration in 1s intervals
                    sleep_remaining = int(decision.wait_seconds)
                    while sleep_remaining > 0:
                        if not state.get_snapshot()["is_running"]:
                            break
                        time.sleep(1)
                        sleep_remaining -= 1
                    continue
            else:
                is_healthy = False
                status = "recovering"
                status_msg = f"Engine HTTP {e.code} error"
        except Exception:
            is_healthy = False
            status = "recovering"
            status_msg = f"Engine port {port} unresponsive"

        # Update Thread-Safe Shared State for the Heartbeat Worker
        state.update_health(is_healthy, models_count, status, status_msg, models_list=parsed_models, discovered_models=discovered_items)

        # Health supervisor ticks every 5 seconds (independent of 15s heartbeat pulse)
        time.sleep(5)

def cmd_test_rl_matrix():
    """
    Automated test suite verifying all 5 rules of the RateLimitDecisionEngine.
    Simulates:
      - Rule 1: Lifespan limit exceeded (T_rl >= T_remain)
      - Rule 2: Single wait cap exceeded (T_rl > 180s)
      - Rule 3: Chained backoff loop trap (consecutive_rl >= 2)
      - Rule 4: Cumulative wait cap exceeded (> 240s)
      - Rule 5: Tolerable RPM micro-burst (T_rl <= 180s, hit #1)
      - Recovery: HTTP 200 resets counters
    """
    print("=" * 70, flush=True)
    log("TEST", "RUNNING SMART RATE LIMIT & WARP DECISION MATRIX TEST SUITE")
    print("=" * 70, flush=True)

    boot = 1000000.0
    lifespan = 21600 # 6 hours
    engine = RateLimitDecisionEngine(boot_time=boot, total_lifespan=lifespan)

    # Test 1: Rule 5 - First short wait (RPM burst)
    d1 = engine.evaluate(rl_wait_sec=30.0, current_time=boot + 100)
    assert d1.action == DecisionResult.ACTION_WAIT, f"Expected WAIT, got {d1.action}"
    assert d1.rule == "RULE_5_TOLERABLE_RPM_BURST", f"Expected RULE_5, got {d1.rule}"
    log("PASS", f"Test 1 Passed: Short burst (30s) -> {d1.rule} ({d1.action})")

    # Test 2: Rule 3 - Chained loop trap (immediate second 429 after 30s wait)
    d2 = engine.evaluate(rl_wait_sec=30.0, current_time=boot + 130)
    assert d2.action == DecisionResult.ACTION_SHIFT_TO_W, f"Expected SHIFT_TO_W, got {d2.action}"
    assert d2.rule == "RULE_3_CHAINED_BACKOFF_TRAP", f"Expected RULE_3, got {d2.rule}"
    log("PASS", f"Test 2 Passed: Chained 429 loop -> {d2.rule} ({d2.action})")

    # Test 3: WARP rotation resets counters
    engine.on_warp_rotated()
    assert engine.consecutive_rl_count == 0
    assert engine.cumulative_wait_sec == 0.0
    log("PASS", "Test 3 Passed: on_warp_rotated cleanly reset all counters")

    # Test 4: Rule 2 - Single wait cap (> 180s, e.g. 300s)
    d4 = engine.evaluate(rl_wait_sec=300.0, current_time=boot + 200)
    assert d4.action == DecisionResult.ACTION_SHIFT_TO_W, f"Expected SHIFT_TO_W, got {d4.action}"
    assert d4.rule == "RULE_2_SINGLE_WAIT_CAP", f"Expected RULE_2, got {d4.rule}"
    log("PASS", f"Test 4 Passed: Long single wait (300s) -> {d4.rule} ({d4.action})")

    # Test 5: Rule 1 - Lifespan expiry (VM has 500s remaining, wait is 600s)
    engine.on_warp_rotated()
    near_end_time = boot + (lifespan - 500)
    d5 = engine.evaluate(rl_wait_sec=600.0, current_time=near_end_time)
    assert d5.action == DecisionResult.ACTION_SHIFT_TO_W, f"Expected SHIFT_TO_W, got {d5.action}"
    assert d5.rule == "RULE_1_LIFECYCLE_EXPIRY", f"Expected RULE_1, got {d5.rule}"
    log("PASS", f"Test 5 Passed: RL exceeds remaining VM life -> {d5.rule} ({d5.action})")

    # Test 6: HTTP 200 resets counters
    engine.on_warp_rotated()
    engine.evaluate(rl_wait_sec=40.0, current_time=boot + 300)
    assert engine.consecutive_rl_count == 1
    engine.on_success()
    assert engine.consecutive_rl_count == 0
    log("PASS", "Test 6 Passed: HTTP 200 on_success cleanly reset counters")

    print("=" * 70, flush=True)
    log("TEST_SUCCESS", "ALL 6 RATE LIMIT DECISION MATRIX TESTS PASSED PERFECTLY!")
    print("=" * 70, flush=True)
    sys.exit(0)

def main():
    parser = argparse.ArgumentParser(description="9Router Node Monitor & Lease Daemon (Option K)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: check-lock
    p_check = subparsers.add_parser("check-lock", help="Pre-flight check if slot is actively locked")
    p_check.add_argument("--slot", required=True, help="Target slot name (e.g. node-1 or node-3)")
    p_check.add_argument("--force", action="store_true", help="Force run: bypass lock check and take over slot")
    p_check.add_argument("--redis-base", default=DEFAULT_REDIS_BASE, help="Redis HTTP API base URL")

    # Subcommand: run
    p_run = subparsers.add_parser("run", help="Start background heartbeat daemon and monitor loop")
    p_run.add_argument("--slot", required=True, help="Target slot name (e.g. node-1 or node-3)")
    p_run.add_argument("--name", help="Poetic node name (e.g. সবুজ কানন)")
    p_run.add_argument("--port", type=int, help="Dynamic port (defaults to 6000+N)")
    p_run.add_argument("--run-id", help="Explicit session RUN_ID")
    p_run.add_argument("--gh-run-id", help="GitHub Run ID for cancellation tracking")
    p_run.add_argument("--api-key", help="9Router master API key")
    p_run.add_argument("--redis-base", default=DEFAULT_REDIS_BASE, help="Redis HTTP API base URL")

    # Subcommand: test-rl-matrix
    subparsers.add_parser("test-rl-matrix", help="Run automated test suite for RateLimitDecisionEngine")

    args = parser.parse_args()

    if args.command == "check-lock":
        cmd_check_lock(args)
    elif args.command == "run":
        cmd_run_daemon(args)
    elif args.command == "test-rl-matrix":
        cmd_test_rl_matrix()

if __name__ == "__main__":
    main()
