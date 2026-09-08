#!/usr/bin/env python3
"""
Seed ~/.9router/db/data.sqlite headlessly so that OpenCode Free models and API key
are pre-configured before 9Router boots up.
"""
import sqlite3
import os
import sys
import json

def get_target_db_path():
    # Allow override via CLI or env
    if len(sys.argv) > 1:
        return sys.argv[1]
    data_dir = os.environ.get("DATA_DIR")
    if not data_dir:
        home = os.path.expanduser("~")
        data_dir = os.path.join(home, ".9router")
    return os.path.join(data_dir, "db", "data.sqlite")

def seed():
    db_path = get_target_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    print(f"[SEED] Target Database: {db_path}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # 1. Create Tables
    cur.execute("""
        CREATE TABLE IF NOT EXISTS _meta (
            key TEXT PRIMARY KEY, 
            value TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1), 
            data TEXT NOT NULL
        )
    """)
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

    # 2. Insert Settings
    cur.execute("""
        INSERT OR REPLACE INTO settings (id, data) 
        VALUES (1, '{"providerStrategies":{},"quotaVisibility":{}}')
    """)

    # 3. Insert Master API Key
    api_key = os.environ.get("ROUTER_API_KEY", "sk-361ddf48ad95487f-l1vj9z-499b11a6")
    cur.execute("""
        INSERT OR REPLACE INTO apiKeys (id, key, name, machineId, isActive, createdAt)
        VALUES ('0243e23f-696e-4b54-a690-1ed6ed4dbb25', ?, 'Default Key', 'cloud-node', 1, datetime('now'))
    """, (api_key,))

    # 4. Insert OpenCode Free Models into KV (scope: customModels)
    models = [
        ("mimo-v2.5-free", "MiMo V2.5 Free"),
        ("nemotron-3-ultra-free", "nemotron-3-ultra-free"),
        ("nemotron-3.5-lightning-free", "nemotron-3.5-lightning-free"),
        ("muse-spark-1.2-contributor-free", "Muse Spark 1.2 Contributor Free"),
        ("muse-spark-1.3-contributor-free", "Muse Spark 1.3 Contributor Free"),
        ("big-pickle", "Big Pickle Free"),
        ("ling-3.0-flash-fin-free", "Ling 3.0 Flash Free")
    ]

    for model_id, model_name in models:
        kv_key = f"oc|{model_id}|llm"
        kv_val = json.dumps({
            "providerAlias": "oc",
            "id": model_id,
            "type": "llm",
            "name": model_name
        })
        cur.execute("""
            INSERT OR REPLACE INTO kv (scope, key, value)
            VALUES ('customModels', ?, ?)
        """, (kv_key, kv_val))
        print(f"  [+] Added Model: oc/{model_id}")

    conn.commit()
    conn.close()
    print("[SEED] Database successfully seeded with OpenCode Free models and API key!")

if __name__ == "__main__":
    seed()
