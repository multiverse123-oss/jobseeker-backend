import os, time, logging, json, threading
import requests
from openai import OpenAI
from serpapi import GoogleSearch

# ---------- KEYS FROM ENVIRONMENT ----------
POCKETBASE_URL = "http://localhost:8090"
POCKETBASE_ADMIN_TOKEN = os.getenv("POCKETBASE_ADMIN_TOKEN")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
FINDWORK_KEY = os.getenv("FINDWORK_KEY")

# ---------- LOG WHAT WE GOT ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.info(f"POCKETBASE_ADMIN_TOKEN loaded: {bool(POCKETBASE_ADMIN_TOKEN)}")
logging.info(f"MISTRAL_API_KEY loaded: {bool(MISTRAL_API_KEY)}")
logging.info(f"SERPAPI_KEY loaded: {bool(SERPAPI_KEY)}")
logging.info(f"ADZUNA_APP_ID loaded: {bool(ADZUNA_APP_ID)}")

if not POCKETBASE_ADMIN_TOKEN:
    raise Exception("Missing POCKETBASE_ADMIN_TOKEN environment variable")
if not MISTRAL_API_KEY:
    raise Exception("Missing MISTRAL_API_KEY environment variable")

ai = OpenAI(api_key=MISTRAL_API_KEY, base_url="https://api.mistral.ai/v1")

def pb(method, path, json_data=None):
    url = f"{POCKETBASE_URL}/api/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {POCKETBASE_ADMIN_TOKEN}"}
    return requests.request(method, url, headers=headers, json=json_data)

# ... (rest of the worker code is identical to the env‑based version you just had)
# For brevity, I'm not re-pasting the entire 200-line file here.
# But you already have the full file in your working directory.
# So instead of replacing everything, we'll just add the debug lines.

# Startup debug (executed when the module loads)
import logging
logging.info(f"--- Startup debug ---")
logging.info(f"POCKETBASE_ADMIN_TOKEN = {'***' if POCKETBASE_ADMIN_TOKEN else 'MISSING'}")
logging.info(f"MISTRAL_API_KEY = {'***' if MISTRAL_API_KEY else 'MISSING'}")
logging.info(f"SERPAPI_KEY = {'***' if SERPAPI_KEY else 'MISSING'}")
logging.info(f"ADZUNA_APP_ID = {'***' if ADZUNA_APP_ID else 'MISSING'}")
