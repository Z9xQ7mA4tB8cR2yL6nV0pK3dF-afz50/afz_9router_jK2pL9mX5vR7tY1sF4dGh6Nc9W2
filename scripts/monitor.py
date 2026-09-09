#!/usr/bin/env python3
"""
9Router Distributed Node Monitor & Telemetry Daemon (Option K: Multi-Threaded Engine)
-------------------------------------------------------------------------------------
Architecture:
1. Thread 1 (Heartbeat & Lease Daemon):
   - Runs in a dedicated background daemon thread (threading.Thread).
   - Fires strictly every 15s regardless of whatever long-running or blocking
     operations (WARP IP cycling, 429 rate-limit cooldown, network diagnostics)
     occur on the main thread.
   - Handles:
     * 9rt:lock:node-X renewal (TTL: 60s)
     * 9rt:hb:node-X telemetry renewal (TTL: 60s)
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

def normalize_slot(raw_slot: str) -> str:
    """Normalizes slot name to canonical 'node-N' format."""
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

# ----------------- Thread-Safe Shared Node State -----------------

class NodeSharedState:
    def __init__(self, slot: str, port: int, run_id: str, gh_run_id: str, boot_time: int):
        self.slot = slot
        self.port = port
        self.run_id = run_id
        self.gh_run_id = gh_run_id
        self.boot_time = boot_time
        
        self.lock = threading.Lock()
        self.status = "online"
        self.status_msg = "Initializing engine & background threads..."
        self.is_healthy = False
        self.active_models_count = 0
        self.is_running = True

    def update_health(self, is_healthy: bool, models_count: int, status: str, status_msg: str):
        with self.lock:
            self.is_healthy = is_healthy
            self.active_models_count = models_count
            self.status = status
            self.status_msg = status_msg

    def get_snapshot(self):
        with self.lock:
            return {
                "slot": self.slot,
                "port": self.port,
                "run_id": self.run_id,
                "gh_run_id": self.gh_run_id,
                "boot_time": self.boot_time,
                "status": self.status,
                "status_msg": self.status_msg,
                "is_healthy": self.is_healthy,
                "models_count": self.active_models_count,
                "is_running": self.is_running
            }

    def stop(self):
        with self.lock:
            self.is_running = False

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
        if e.code == 404:
            return None
        return None
    except Exception as e:
        log("REDIS_ERR", f"HTTP Error on {url}: {e}")
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
    url = f"{redis_base.rstrip('/')}/SETEX/{urllib.parse.quote(key)}/{seconds}/{urllib.parse.quote(string_val)}"
    return redis_http_call(url)

def redis_setex_json(redis_base: str, key: str, seconds: int, value_dict: dict):
    val_json = json.dumps(value_dict)
    url = f"{redis_base.rstrip('/')}/SETEX/{urllib.parse.quote(key)}/{seconds}/{urllib.parse.quote(val_json)}"
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

def heartbeat_worker(state: NodeSharedState, redis_base: str, lock_key: str, hb_key: str):
    """
    Dedicated Background Heartbeat Thread:
    Runs strictly every 15 seconds. Unaffected by any blocking engine cooldowns.
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
                # Hard exit immediately to stop duplicate runner
                os._exit(0)
            else:
                # Renew lock lease (TTL 60s)
                redis_setex_str(redis_base, lock_key, 60, snap["run_id"])
        else:
            # Lock was deleted or expired: Self-Heal and re-claim lock!
            log("SELF_HEAL", f"Lock key was missing. Re-claiming lock lease for {snap['run_id']}...")
            redis_setex_str(redis_base, lock_key, 60, snap["run_id"])

        # 2. Renew All-in-One Heartbeat (TTL 60s)
        hb_payload = {
            "slot": snap["slot"],
            "port": snap["port"],
            "run_id": snap["run_id"],
            "gh_run_id": snap["gh_run_id"],
            "boot_time": snap["boot_time"],
            "pulse": now,
            "uptime": uptime,
            "status": snap["status"],
            "status_msg": snap["status_msg"]
        }
        redis_setex_json(redis_base, hb_key, 60, hb_payload)

        # 3. Rich Formatted Console Output
        badge = "🟢 ONLINE" if snap["is_healthy"] else f"🟡 {snap['status'].upper()}"
        log("PULSE", f"#{pulse_count:04d} | Uptime: {uptime}s | {badge} | Msg: {snap['status_msg']}")

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
    run_id = f"{slot}_{boot_timestamp}"

    lock_key = f"9rt:lock:{slot}"
    hb_key = f"9rt:hb:{slot}"

    print("=" * 70, flush=True)
    log("INIT", f"9ROUTER MULTI-THREADED NODE MONITOR STARTING")
    log("INIT", f"Canonical Slot : {slot}")
    log("INIT", f"Dynamic Port   : {port}")
    log("INIT", f"Session RUN_ID : {run_id}")
    log("INIT", f"GitHub Run ID  : {gh_run_id}")
    log("INIT", f"Lock Key       : {lock_key}")
    log("INIT", f"Heartbeat Key  : {hb_key}")
    log("INIT", f"Redis API Base : {redis_base}")
    print("=" * 70, flush=True)

    # Initialize Thread-Safe Shared State
    state = NodeSharedState(slot, port, run_id, gh_run_id, boot_timestamp)

    # Initial Lock Claim and Registration
    log("INIT", f"Claiming initial lock lease at {lock_key}...")
    redis_setex_str(redis_base, lock_key, 60, run_id)

    initial_hb = {
        "slot": slot,
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

    # Spawn Thread 1: Dedicated Heartbeat Worker
    hb_thread = threading.Thread(
        target=heartbeat_worker,
        args=(state, redis_base, lock_key, hb_key),
        daemon=True,
        name="HeartbeatDaemon"
    )
    hb_thread.start()
    log("INIT", "Spawned independent HeartbeatDaemon thread.")

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

        try:
            req = urllib.request.Request(
                models_url,
                headers={"Authorization": f"Bearer {api_key}"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    data = resp.read().decode("utf-8")
                    try:
                        parsed = json.loads(data)
                        if isinstance(parsed, dict) and "data" in parsed:
                            models_count = len(parsed["data"])
                        elif isinstance(parsed, list):
                            models_count = len(parsed)
                    except Exception:
                        pass
                    
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
                status = "cooldown"
                status_msg = "429 Rate Limited (Waiting / Cycling IP cooldown...)"
                log("HEALTH", "429 Rate Limit detected! Heartbeat continues on thread.")
            else:
                is_healthy = False
                status = "recovering"
                status_msg = f"Engine HTTP {e.code} error"
        except Exception:
            is_healthy = False
            status = "recovering"
            status_msg = f"Engine port {port} unresponsive"

        # Update Thread-Safe Shared State for the Heartbeat Worker
        state.update_health(is_healthy, models_count, status, status_msg)

        # Health supervisor ticks every 5 seconds (independent of 15s heartbeat pulse)
        time.sleep(5)

def main():
    parser = argparse.ArgumentParser(description="9Router Node Monitor & Lease Daemon (Option K)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: check-lock
    p_check = subparsers.add_parser("check-lock", help="Pre-flight check if slot is actively locked")
    p_check.add_argument("--slot", required=True, help="Target slot name (e.g. 9rt3 or node-3)")
    p_check.add_argument("--force", action="store_true", help="Force run: bypass lock check and take over slot")
    p_check.add_argument("--redis-base", default=DEFAULT_REDIS_BASE, help="Redis HTTP API base URL")

    # Subcommand: run
    p_run = subparsers.add_parser("run", help="Start background telemetry and heartbeat loop")
    p_run.add_argument("--slot", required=True, help="Target slot name (e.g. 9rt3 or node-3)")
    p_run.add_argument("--port", type=int, help="Dynamic port (defaults to 6000+N)")
    p_run.add_argument("--gh-run-id", help="GitHub Run ID for cancellation tracking")
    p_run.add_argument("--api-key", help="9Router master API key")
    p_run.add_argument("--redis-base", default=DEFAULT_REDIS_BASE, help="Redis HTTP API base URL")

    args = parser.parse_args()

    if args.command == "check-lock":
        cmd_check_lock(args)
    elif args.command == "run":
        cmd_run_daemon(args)

if __name__ == "__main__":
    main()
