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
    ("big-pickle", "Big Pickle Free"),
    ("mimo-v2.5-free", "MiMo V2.5 Free"),
    ("nemotron-3-ultra-free", "Nemotron 3 Ultra Free"),
    ("nemotron-3.5-lightning-free", "Nemotron 3.5 Lightning Free"),
    ("muse-spark-1.3-contributor-free", "Muse Spark 1.3 Contributor Free"),
    ("ling-3.0-flash-fin-free", "Ling 3.0 Flash Free"),
]

def get_target_db_path():
    if len(sys.argv) > 1:
        return sys.argv[1]
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
        "https://opencode.ai/api/v1/models",
        "https://opencode.ai/v1/models",
        "https://api.opencode.ai/v1/models"
    ]
    discovered = []
    headers = {"User-Agent": "9Router-Dynamic-Discovery/2.0"}

    for ep in endpoints:
        try:
            req = urllib.request.Request(ep, headers=headers)
            with urllib.request.urlopen(req, timeout=4) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    raw_list = data.get("data") or data.get("models") or []
                    for m in raw_list:
                        m_id = m.get("id") or m.get("name") or ""
                        m_name = m.get("name") or m_id
                        # Match 'free' (case-insensitive) or 'big-pickle' / 'big pickle'
                        if re.search(r"free|big[-_\s]?pickle", m_id, re.IGNORECASE) or \
                           re.search(r"free|big[-_\s]?pickle", m_name, re.IGNORECASE):
                            discovered.append((m_id, m_name))
                    if discovered:
                        print(f"[SEED] Successfully discovered {len(discovered)} live models from {ep}")
                        break
        except Exception:
            continue

    # Ensure 'big-pickle' is always in the list
    has_pickle = any("big-pickle" in m[0].lower() or "big pickle" in m[0].lower() for m in discovered)
    if not has_pickle:
        discovered.insert(0, ("big-pickle", "Big Pickle Free"))

    return discovered if discovered else BASELINE_MODELS

def seed(custom_combo=None):
    db_path = get_target_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    print(f"[SEED] Target Database: {db_path}")

    conn = sqlite3.connect(db_path)
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

    # 2. Insert Settings & Master API Key
    cur.execute("INSERT OR REPLACE INTO settings (id, data) VALUES (1, '{\"providerStrategies\":{},\"quotaVisibility\":{}}')")
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
            model_id, model_name = item
        else:
            model_id = str(item)
            model_name = str(item).replace("-", " ").title()

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
    seed()
