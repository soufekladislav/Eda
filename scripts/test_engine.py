#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

base = (os.getenv("AI_ENGINE_URL") or "https://aiengine.freshcrackers.cz").rstrip("/")
key = os.getenv("AI_ENGINE_API_KEY") or os.getenv("FRESH_AI_ENGINE_API_KEY") or ""
if not key:
    print("ERROR: AI_ENGINE_API_KEY není nastavený v .env")
    sys.exit(2)

payload = {
    "source": os.getenv("AI_ENGINE_APP_ID") or "eda",
    "project_id": "eda:diagnostic",
    "task_type": "chat",
    "operation": "chat.conversation",
    "quality": os.getenv("AI_ENGINE_QUALITY") or "balanced",
    "prompt": "Odpověz pouze: EDA ENGINE OK",
    "context": {
        "application": "Eda",
        "diagnostic": True,
        "system_prompt": "Jsi Eda. Jde o diagnostický test připojení."
    },
    "max_cost_usd": float(os.getenv("AI_ENGINE_MAX_COST_USD") or "0.20"),
}

url = f"{base}/v1/engine/run"
print(f"POST {url}")
try:
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "Accept": "application/json"},
        json=payload,
        timeout=int(os.getenv("AI_ENGINE_TIMEOUT_SECONDS") or "90"),
    )
except requests.RequestException as exc:
    print(f"ERROR: {exc}")
    sys.exit(3)

print(f"HTTP {r.status_code}")
try:
    data = r.json()
    print(json.dumps(data, ensure_ascii=False, indent=2)[:12000])
except ValueError:
    print(r.text[:12000])

sys.exit(0 if r.status_code < 400 else 4)
