#!/usr/bin/env python3
"""
9Router Distributed Node Monitor & Telemetry Daemon
---------------------------------------------------
Implements:
1. Fast-Abort on Boot: Checks 9rt:hb:node-X for existing active lease.
2. Deterministic Lock Lease: Claims slot using RUN_ID = f"{slot}_{boot_time}".
3. 2-Key Architecture:
   - 9rt:meta:node-X (Static, written once at boot)
   - 9rt:hb:node-X   (Living heartbeat & lock, renewed every 15s with 60s TTL)
4. Self-Suicide: Gracefully terminates if another runner claims the slot lock.
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
    """
    Normalizes slot name to canonical 'node-N' format.
    Examples:
      '9rt3'   -> 'node-3'
      'node-3' -> 'node-3'
      '3'      -> 'node-3'
    """
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
    """Calculates port based on formula: 6000 + N"""
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

def redis_set(redis_base: str, key: str, value_dict: dict):
    val_json = json.dumps(value_dict)
    url = f"{redis_base.rstrip('/')}/SET/{urllib.parse.quote(key)}/{urllib.parse.quote(val_json)}"
    return redis_http_call(url)

def redis_setex(redis_base: str, key: str, seconds: int, value_dict: dict):
    val_json = json.dumps(value_dict)
    url = f"{redis_base.rstrip('/')}/SETEX/{urllib.parse.quote(key)}/{seconds}/{urllib.parse.quote(val_json)}"
    return redis_http_call(url)

def redis_del(redis_base: str, key: str):
    url = f"{redis_base.rstrip('/')}/DEL/{urllib.parse.quote(key)}"
    return redis_http_call(url)

# ----------------- Core Operational Modes -----------------

def cmd_check_lock(args):
    """
    Pre-flight Lock Check:
    Verifies if slot is already occupied.
    Returns:
      Exit 0: Slot is free, safe to proceed.
      Exit 42: Slot is actively locked, abort duplicate run immediately.
    """
    slot = normalize_slot(args.slot)
    redis_base = args.redis_base or DEFAULT_REDIS_BASE
    hb_key = f"9rt:hb:{slot}"

    print(f"[PREFLIGHT] Checking active lock for slot '{slot}' ({hb_key})...")
    existing_hb = redis_get(redis_base, hb_key)

    if existing_hb and isinstance(existing_hb, dict):
        owner = existing_hb.get("owner_run_id", "unknown_owner")
        pulse = existing_hb.get("pulse", 0)
        status = existing_hb.get("status", "unknown")
        
        print("=" * 65)
        print(f"  [ABORT] SLOT IS ACTIVELY OCCUPIED!")
        print(f"  Slot       : {slot}")
        print(f"  Owner Run  : {owner}")
        print(f"  Status     : {status}")
        print(f"  Last Pulse : {pulse}")
        print(f"  Action     : Terminating duplicate workflow run without interference.")
        print("=" * 65)
        
        # Set GitHub Action output if in GITHUB_OUTPUT environment
        gh_output = os.environ.get("GITHUB_OUTPUT")
        if gh_output and os.path.exists(gh_output):
            with open(gh_output, "a") as f:
                f.write("is_locked=true\n")
        
        sys.exit(42) # Special exit code for duplicate lock abort
    
    print(f"[PREFLIGHT] Slot '{slot}' is completely FREE. No active lease found.")
    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output and os.path.exists(gh_output):
        with open(gh_output, "a") as f:
            f.write("is_locked=false\n")
            f.write(f"canonical_slot={slot}\n")
    sys.exit(0)

def cmd_run_daemon(args):
    """
    Main Telemetry & Lock Daemon:
    1. Registers 9rt:meta:{slot} (once).
    2. Continually updates 9rt:hb:{slot} with 60s TTL every 15s.
    3. Handles self-suicide if owner_run_id is superseded.
    """
    slot = normalize_slot(args.slot)
    port = args.port or calculate_port(slot)
    redis_base = args.redis_base or DEFAULT_REDIS_BASE
    gh_run_id = args.gh_run_id or os.environ.get("GITHUB_RUN_ID", "local")
    api_key = args.api_key or os.environ.get("ROUTER_API_KEY", "sk-361ddf48ad95487f-l1vj9z-499b11a6")

    boot_timestamp = int(time.time())
    run_id = f"{slot}_{boot_timestamp}"

    meta_key = f"9rt:meta:{slot}"
    hb_key = f"9rt:hb:{slot}"

    print("==================================================")
    print(f"  9ROUTER NODE MONITOR STARTING")
    print(f"  Canonical Slot : {slot}")
    print(f"  Dynamic Port   : {port}")
    print(f"  Session RUN_ID : {run_id}")
    print(f"  GitHub Run ID  : {gh_run_id}")
    print(f"  Redis API Base : {redis_base}")
    print("==================================================")

    # 1. Write Static Metadata (Once)
    meta_payload = {
        "slot": slot,
        "port": port,
        "run_id": run_id,
        "gh_run_id": gh_run_id,
        "boot_time": boot_timestamp,
        "os": f"{platform.system()} {platform.release()}"
    }
    print(f"[STEP] Registering static metadata in Redis at {meta_key}...")
    redis_set(redis_base, meta_key, meta_payload)

    # Initial Lock Acquisition (TTL: 60s)
    hb_payload = {
        "owner_run_id": run_id,
        "status": "online",
        "status_msg": "Initializing 9Router engine...",
        "pulse": boot_timestamp,
        "uptime": 0
    }
    redis_setex(redis_base, hb_key, 60, hb_payload)
    print(f"[SUCCESS] Slot lock acquired! Initial heartbeat registered in {hb_key} (TTL: 60s).")

    # Graceful Shutdown Handler
    is_running = True
    def handle_signal(sig, frame):
        nonlocal is_running
        print(f"\n[SIGNAL] Received termination signal ({sig}). Cleaning up...")
        is_running = False
        # Remove living heartbeat lock on clean shutdown so slot becomes immediately available
        redis_del(redis_base, hb_key)
        print(f"[CLEANUP] Slot {slot} unlocked cleanly.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    pulse_count = 0

    # 2. Main Pulse Loop (Every 15s)
    while is_running:
        pulse_count += 1
        now = int(time.time())
        uptime = now - boot_timestamp

        # Verify Lock Ownership (Check if superseded by newer run)
        remote_hb = redis_get(redis_base, hb_key)
        if remote_hb and isinstance(remote_hb, dict):
            remote_owner = remote_hb.get("owner_run_id")
            if remote_owner and remote_owner != run_id:
                print("=" * 65)
                print(f"  [SUPERSEDED] Lock stolen or taken over by newer run!")
                print(f"  Current Run : {run_id}")
                print(f"  New Ruler   : {remote_owner}")
                print(f"  Action      : Gracefully committing self-suicide to yield slot.")
                print("=" * 65)
                sys.exit(0)

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

        # Renew Heartbeat and Extend Lock TTL to 60s
        current_hb = {
            "owner_run_id": run_id,
            "status": status,
            "status_msg": status_msg,
            "pulse": now,
            "uptime": uptime
        }
        redis_setex(redis_base, hb_key, 60, current_hb)

        # Formatted Console Output
        badge = "🟢 ONLINE" if is_healthy else "🟡 RECOVERING"
        time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        print(f"[{time_str}] [{slot}] Pulse #{pulse_count:04d} | Uptime: {uptime}s | {badge} | Msg: {status_msg}")

        # Sleep in 1-second chunks for responsive signal handling
        for _ in range(15):
            if not is_running:
                break
            time.sleep(1)

def main():
    parser = argparse.ArgumentParser(description="9Router Node Monitor & Lease Daemon")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: check-lock
    p_check = subparsers.add_parser("check-lock", help="Pre-flight check if slot is actively locked")
    p_check.add_argument("--slot", required=True, help="Target slot name (e.g. 9rt3 or node-3)")
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
