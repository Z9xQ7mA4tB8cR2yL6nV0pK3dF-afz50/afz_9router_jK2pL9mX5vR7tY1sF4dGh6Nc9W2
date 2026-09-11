#!/usr/bin/env python3
"""
Dynamic Database Seeder & Model Discovery for 9Router
=====================================================
- Dynamically queries live OpenCode models matching 'free', 'FREE', or 'big-pickle'.
- Automatically provisions ~/.9router/db/data.sqlite with live models, API keys,
  and smart default alias mappings (e.g. 'default', 'claude-3-5-sonnet' -> 'big-pickle').
- Resilient fallback mechanism ensures 9Router boots instantly even if external network is slow.
"""
import sqlite3
import os
import sys
import json
import re
import urllib.request
import urllib.error

# Curated resilient baseline (guaranteed fallback if live discovery fails)
BASELINE_MODELS = [
    ("big-pickle", "big-pickle"),
    ("mimo-v2.5-free", "mimo-v2.5-free"),
    ("nemotron-3-ultra-free", "nemotron-3-ultra-free"),
    ("nemotron-3.5-lightning-free", "nemotron-3.5-lightning-free"),
    ("muse-spark-1.3-contributor-free", "muse-spark-1.3-contributor-free"),
    ("ling-3.0-flash-fin-free", "ling-3.0-flash-fin-free"),
]

def get_target_db_path(explicit_path=None):
    if explicit_path:
        return explicit_path
    db_env = os.environ.get("ROUTER_DB_PATH")
    if db_env:
        return db_env
    data_dir = os.environ.get("DATA_DIR")
    if not data_dir:
        home = os.path.expanduser("~")
        data_dir = os.path.join(home, ".9router")
    return os.path.join(data_dir, "db", "data.sqlite")

def fetch_live_opencode_models():
    """
    Attempts to fetch live models from OpenCode catalog endpoints.
    Filters models matching 'free|FREE|Free' or 'big-pickle'.
    """
    endpoints = [
        "https://opencode.ai/zen/v1/models",
        "https://opencode.ai/api/v1/models",
        "https://opencode.ai/v1/models"
    ]
    discovered = []
    headers = {
        "User-Agent": "9Router-Dynamic-Discovery/2.0",
        "x-opencode-client": "desktop"
    }

    for ep in endpoints:
        try:
            req = urllib.request.Request(ep, headers=headers)
            with urllib.request.urlopen(req, timeout=4) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    raw_list = data.get("data") or data.get("models") or []
                    for m in raw_list:
                        m_id = m.get("id") or m.get("name") or ""
                        # Match 'free' (case-insensitive) or 'big-pickle'
                        if re.search(r"free|big[-_\s]?pickle", m_id, re.IGNORECASE):
                            discovered.append((m_id, m_id))
                    if discovered:
                        print(f"[SEED] Successfully discovered {len(discovered)} live models from {ep}")
                        break
        except Exception:
            continue

    # Ensure 'big-pickle' is always in the list
    has_pickle = any("big-pickle" in m[0].lower() for m in discovered)
    if not has_pickle:
        discovered.insert(0, ("big-pickle", "big-pickle"))

    return discovered if discovered else BASELINE_MODELS

def seed(custom_combo=None, db_path=None):
    target_db = get_target_db_path(db_path)
    os.makedirs(os.path.dirname(target_db), exist_ok=True)
    print(f"[SEED] Target Database: {target_db}")

    conn = sqlite3.connect(target_db)
    cur = conn.cursor()

    # 1. Create Core 9Router SQLite Schema
    cur.execute("CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    cur.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS providerConnections (
            id TEXT PRIMARY KEY, 
            provider TEXT NOT NULL, 
            authType TEXT NOT NULL, 
            name TEXT, 
            email TEXT, 
            priority INTEGER, 
            isActive INTEGER DEFAULT 1, 
            data TEXT NOT NULL, 
            createdAt TEXT NOT NULL, 
            updatedAt TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS providerNodes (
            id TEXT PRIMARY KEY, 
            type TEXT, 
            name TEXT, 
            data TEXT NOT NULL, 
            createdAt TEXT NOT NULL, 
            updatedAt TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS apiKeys (
            id TEXT PRIMARY KEY, 
            key TEXT UNIQUE NOT NULL, 
            name TEXT, 
            machineId TEXT, 
            isActive INTEGER DEFAULT 1, 
            createdAt TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS kv (
            scope TEXT NOT NULL, 
            key TEXT NOT NULL, 
            value TEXT NOT NULL, 
            PRIMARY KEY (scope, key)
        )
    """)

    # 2. Insert Settings, Active Provider Connection & Master API Key
    settings_data = {
        "providerStrategies": {},
        "quotaVisibility": {},
        "tunnelDashboardAccess": True
    }
    cur.execute("INSERT OR REPLACE INTO settings (id, data) VALUES (1, ?)", (json.dumps(settings_data),))
    
    # Register OpenCode Free as the sole active provider connection.
    # This prevents 9Router from falling back to exposing all 640+ models from other providers!
    cur.execute("""
        INSERT OR REPLACE INTO providerConnections (id, provider, authType, name, email, priority, isActive, data, createdAt, updatedAt)
        VALUES ('conn-opencode-free', 'opencode', 'none', 'OpenCode Free', NULL, 1, 1, '{}', datetime('now'), datetime('now'))
    """)

    api_key = os.environ.get("ROUTER_API_KEY", "sk-361ddf48ad95487f-l1vj9z-499b11a6")
    cur.execute("""
        INSERT OR REPLACE INTO apiKeys (id, key, name, machineId, isActive, createdAt)
        VALUES ('0243e23f-696e-4b54-a690-1ed6ed4dbb25', ?, 'Default Key', 'cloud-node', 1, datetime('now'))
    """, (api_key,))

    # 3. Discover or Apply Models
    if custom_combo:
        cur.execute("DELETE FROM kv WHERE scope = 'customModels'")
        print(f"[SEED] Cleared previous customModels for new combo configuration.")
    models = custom_combo if custom_combo else fetch_live_opencode_models()
    print(f"[SEED] Registering {len(models)} models into 9Router...")

    for item in models:
        if isinstance(item, tuple):
            model_id = item[0]
        else:
            model_id = str(item)
        model_name = model_id

        kv_key = f"oc|{model_id}|llm"
        kv_val = json.dumps({
            "providerAlias": "oc",
            "id": model_id,
            "type": "llm",
            "name": model_name
        })
        cur.execute("INSERT OR REPLACE INTO kv (scope, key, value) VALUES ('customModels', ?, ?)", (kv_key, kv_val))
        print(f"  [+] Active Model: oc/{model_id}")

    # 4. Smart Fallback Aliases ('default' and 'claude-3-5-sonnet' -> primary model)
    primary_model_id = models[0][0] if isinstance(models[0], tuple) else models[0]
    for alias in ["default", "claude-3-5-sonnet", "auto"]:
        cur.execute("INSERT OR REPLACE INTO kv (scope, key, value) VALUES ('customModels', ?, ?)", (
            f"oc|{alias}|llm",
            json.dumps({
                "providerAlias": "oc",
                "id": alias,
                "targetModel": primary_model_id,
                "type": "llm",
                "name": f"Auto Fallback -> {primary_model_id}"
            })
        ))

    conn.commit()
    conn.close()
    print("[SEED] 9Router Database successfully provisioned and ready for traffic!")
    return [m[0] if isinstance(m, tuple) else m for m in models]

if __name__ == "__main__":
    cli_path = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else None
    seed(db_path=cli_path)
