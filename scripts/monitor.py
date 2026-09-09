#!/usr/bin/env python3
"""
9Router Distributed Node Monitor & Telemetry Daemon (Option K: 2-Key Model)
-------------------------------------------------------------------------
Implements:
1. 2-Key Architecture:
   - 9rt:lock:node-X : Pure string lock lease (value: RUN_ID, TTL: 60s)
   - 9rt:hb:node-X   : All-in-one telemetry + metadata (JSON, TTL: 60s)
   (No separate meta key - metadata is merged directly into heartbeat!)
2. Pre-flight Fast-Abort: Checks 9rt:lock:node-X on boot; exits immediately if locked.
3. Self-Healing vs Self-Suicide:
   - If 9rt:lock:node-X is deleted: Running node re-claims it (Self-Healing).
   - If 9rt:lock:node-X is owned by another RUN_ID: Node self-terminates (Self-Suicide).
4. Local Independence: Node stores its identity in local memory (decoupled from Redis).
5. Zero dependencies: Standard library Python only.
"""

import os
import sys
import time
import json
import signal
import platform
import argparse
import urllib.request
import urllib.parse
import urllib.error

DEFAULT_REDIS_BASE = "https://jelab101-rimjhim.hf.space"

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

# ----------------- Redis HTTP REST Client -----------------

def redis_http_call(url: str, timeout: int = 10):
    try:
        req = urllib.request.Request(
            url, 
            headers={"User-Agent": "9Router-Monitor/1.0", "Accept": "application/json"}
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
        print(f"[WARN] Redis HTTP request error ({url}): {e}", file=sys.stderr)
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

# ----------------- Core Operational Modes -----------------

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
        print(f"[PREFLIGHT] Force flag active! Bypassing lock check for slot '{slot}'...")
        gh_output = os.environ.get("GITHUB_OUTPUT")
        if gh_output and os.path.exists(gh_output):
            with open(gh_output, "a") as f:
                f.write("is_locked=false\n")
                f.write(f"canonical_slot={slot}\n")
        sys.exit(0)

    print(f"[PREFLIGHT] Checking lock for slot '{slot}' ({lock_key})...")
    existing_lock = redis_get(redis_base, lock_key)

    if existing_lock:
        owner = str(existing_lock)
        print("=" * 65)
        print(f"  [ABORT] SLOT IS ACTIVELY LOCKED!")
        print(f"  Slot       : {slot}")
        print(f"  Lock Owner : {owner}")
        print(f"  Action     : Terminating duplicate workflow run without interference.")
        print("=" * 65)
        
        gh_output = os.environ.get("GITHUB_OUTPUT")
        if gh_output and os.path.exists(gh_output):
            with open(gh_output, "a") as f:
                f.write("is_locked=true\n")
        
        sys.exit(42) # Exit code indicating duplicate lock
    
    print(f"[PREFLIGHT] Slot '{slot}' is completely FREE. Ready to proceed.")
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output and os.path.exists(gh_output):
        with open(gh_output, "a") as f:
            f.write("is_locked=false\n")
            f.write(f"canonical_slot={slot}\n")
    sys.exit(0)

def cmd_run_daemon(args):
    """
    Main Telemetry & Lock Lease Daemon (Option K: 2-Key Model):
    - Key 1: 9rt:lock:node-X (Pure string lock, TTL: 60s)
    - Key 2: 9rt:hb:node-X   (All-in-one Telemetry + Meta JSON, TTL: 60s)
    """
    slot = normalize_slot(args.slot)
    port = args.port or calculate_port(slot)
    redis_base = args.redis_base or DEFAULT_REDIS_BASE
    gh_run_id = args.gh_run_id or os.environ.get("GITHUB_RUN_ID", "local")
    api_key = args.api_key or os.environ.get("ROUTER_API_KEY", "sk-361ddf48ad95487f-l1vj9z-499b11a6")

    # Local birth certificate (Decoupled from Redis)
    boot_timestamp = int(time.time())
    run_id = f"{slot}_{boot_timestamp}"

    lock_key = f"9rt:lock:{slot}"
    hb_key = f"9rt:hb:{slot}"

    print("==================================================")
    print(f"  9ROUTER NODE MONITOR STARTING (OPTION K)")
    print(f"  Canonical Slot : {slot}")
    print(f"  Dynamic Port   : {port}")
    print(f"  Session RUN_ID : {run_id}")
    print(f"  GitHub Run ID  : {gh_run_id}")
    print(f"  Lock Key       : {lock_key}")
    print(f"  Heartbeat Key  : {hb_key}")
    print(f"  Redis API Base : {redis_base}")
    print("==================================================")

    # 1. Initial Lock Claim (TTL: 60s)
    print(f"[STEP] Claiming lock lease in Redis at {lock_key}...")
    redis_setex_str(redis_base, lock_key, 60, run_id)

    # 2. Initial All-in-One Heartbeat Registration (TTL: 60s)
    hb_payload = {
        "slot": slot,
        "port": port,
        "run_id": run_id,
        "gh_run_id": gh_run_id,
        "boot_time": boot_timestamp,
        "pulse": boot_timestamp,
        "uptime": 0,
        "status": "online",
        "status_msg": "Initializing 9Router engine..."
    }
    redis_setex_json(redis_base, hb_key, 60, hb_payload)
    print(f"[SUCCESS] Slot locked and initial heartbeat registered!")

    # Graceful Shutdown Handler
    is_running = True
    def handle_signal(sig, frame):
        nonlocal is_running
        print(f"\n[SIGNAL] Received termination signal ({sig}). Cleaning up...")
        is_running = False
        redis_del(redis_base, lock_key)
        redis_del(redis_base, hb_key)
        print(f"[CLEANUP] Slot {slot} lock and heartbeat cleared cleanly.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    pulse_count = 0

    # 3. Main Pulse Loop (Every 15s)
    while is_running:
        pulse_count += 1
        now = int(time.time())
        uptime = now - boot_timestamp

        # --- Self-Healing vs Self-Suicide Lock Check ---
        current_lock = redis_get(redis_base, lock_key)
        if current_lock is not None:
            current_owner = str(current_lock)
            if current_owner != run_id:
                # Lock taken over by a newer runner!
                print("=" * 65)
                print(f"  [SUPERSEDED] Lock usurped by newer runner!")
                print(f"  Current Run : {run_id}")
                print(f"  New Ruler   : {current_owner}")
                print(f"  Action      : Committing graceful self-suicide.")
                print("=" * 65)
                sys.exit(0)
            else:
                # Still owner: renew lock TTL to 60s
                redis_setex_str(redis_base, lock_key, 60, run_id)
        else:
            # Lock was deleted externally: Self-Heal and re-claim lock!
            print(f"[SELF-HEALING] Lock key was missing. Re-claiming lock lease for {run_id}...")
            redis_setex_str(redis_base, lock_key, 60, run_id)

        # Check Local 9Router Engine Health
        models_url = f"http://127.0.0.1:{port}/v1/models"
        is_healthy = False
        try:
            req = urllib.request.Request(
                models_url,
                headers={"Authorization": f"Bearer {api_key}"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    data = resp.read().decode("utf-8")
                    if "mimo-v2.5-free" in data:
                        is_healthy = True
        except Exception:
            is_healthy = False

        status = "online" if is_healthy else "recovering"
        status_msg = "Healthy (OpenCode models active)" if is_healthy else f"Engine port {port} unresponsive"

        # Renew All-in-One Heartbeat (Preserving local specs + live telemetry)
        current_hb = {
            "slot": slot,
            "port": port,
            "run_id": run_id,
            "gh_run_id": gh_run_id,
            "boot_time": boot_timestamp,
            "pulse": now,
            "uptime": uptime,
            "status": status,
            "status_msg": status_msg
        }
        redis_setex_json(redis_base, hb_key, 60, current_hb)

        # Console Output
        badge = "🟢 ONLINE" if is_healthy else "🟡 RECOVERING"
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        print(f"[{time_str}] [{slot}] Pulse #{pulse_count:04d} | Uptime: {uptime}s | {badge} | Msg: {status_msg}")

        # Sleep in 1-second chunks for responsive signal termination
        for _ in range(15):
            if not is_running:
                break
            time.sleep(1)

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
