import threading
import os, time, logging, json, re, subprocess
import requests
from dotenv import load_dotenv
from openai import OpenAI
from serpapi import GoogleSearch
from jobspy import scrape_jobs as jobspy_scrape
from bs4 import BeautifulSoup
import threading

load_dotenv()

# ---------- CONFIG ----------
POCKETBASE_URL = os.getenv("POCKETBASE_URL", "http://localhost:8090")
POCKETBASE_ADMIN_TOKEN = os.getenv("POCKETBASE_ADMIN_TOKEN")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
FINDWORK_KEY = os.getenv("FINDWORK_KEY")
JSEARCH_HOST = "jsearch.p.rapidapi.com"

if not POCKETBASE_ADMIN_TOKEN: raise Exception("Missing POCKETBASE_ADMIN_TOKEN")
if not MISTRAL_API_KEY: raise Exception("Missing MISTRAL_API_KEY")

ai = OpenAI(api_key=MISTRAL_API_KEY, base_url="https://api.mistral.ai/v1")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ---------- PocketBase helpers ----------
def pb(method, path, json_data=None, files=None):
    url = f"{POCKETBASE_URL}/api/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {POCKETBASE_ADMIN_TOKEN}"}
    if files:
        return requests.request(method, url, headers=headers, files=files)
    return requests.request(method, url, headers=headers, json=json_data)

# ... (all existing source functions: search_serpapi, search_jobspy, etc., keep them exactly as before) ...

# For brevity, I'll assume the full source list is already present in the file.
# The only change is the main loop structure.

# ---------- FAST CHAT LOOP ----------
def fast_chat_loop():
    while True:
        try:
            resp = pb("GET", "/collections/chat_messages/records?filter=(response='')&sort=created&perPage=10")
            if resp.status_code != 200:
                time.sleep(5)
                continue
            messages = resp.json().get("items", [])
            for msg in messages:
                msg_id = msg["id"]
                user_id = msg["user"]
                text = msg["message"]
                # Get user context
                user = pb("GET", f"/collections/users/records/{user_id}").json()
                ctx = ""
                if user:
                    ctx = f"User skills: {user.get('skills','')}. Desired job: {user.get('desired_job_title','')}"
                # If the message already contains the coaching system prefix, use it as is; otherwise, it's a normal chat.
                # The worker just sends whatever message is stored. The frontend already prepends the prefix for coaching.
                prompt = f"{ctx}\nUser: {text}\nAnswer helpfully and suggest job search queries."
                try:
                    resp_ai = ai.chat.completions.create(
                        model="mistral-small-latest",
                        messages=[{"role":"user","content":prompt}],
                        temperature=0.7,
                        max_tokens=150   # <-- FAST! Only 150 tokens for quick replies
                    )
                    answer = resp_ai.choices[0].message.content.strip()
                    pb("PATCH", f"/collections/chat_messages/records/{msg_id}", json_data={"response": answer})
                    logging.info(f"Replied to chat {msg_id}")
                except Exception as e:
                    logging.error(f"Chat failed: {e}")
                time.sleep(0.2)  # tiny gap between messages
        except Exception as e:
            logging.error(f"Fast chat loop error: {e}")
        time.sleep(10)   # check every 10 seconds

# ---------- BACKGROUND SCRAPING LOOP (unchanged) ----------
def background_scraping_loop():
    while True:
        logging.info("=== Background job scraping cycle start ===")
        # Process search requests first
        # ... (same code as before: process_search_requests, fetch_all_users, build_search_query, etc.)
        # Since the full source functions are already in the file, I'll just call them.
        # For simplicity, I'll include a placeholder – but in the actual file, the full functions are there.
        # We'll reuse the existing code by not overwriting it; this snippet only modifies the main loops.
        # So the file should contain the entire previous worker, but with this new loop structure.
        time.sleep(300)   # 5 minutes

# In the actual worker.py, we'll have both loops running as threads.
if __name__ == "__main__":
    chat_thread = threading.Thread(target=fast_chat_loop, daemon=True)
    chat_thread.start()
    
    # Start fast chat loop in a thread
    chat_thread = threading.Thread(target=fast_chat_loop, daemon=True)
    chat_thread.start()
    # Start background scraping loop in main thread (or another thread)
    # For simplicity, run scraping loop in main thread
    background_scraping_loop()

def fast_chat_loop():
    while True:
        try:
            resp = pb("GET", "/collections/chat_messages/records?filter=(response='')&sort=created&perPage=10")
            if resp.status_code != 200:
                time.sleep(5)
                continue
            messages = resp.json().get("items", [])
            for msg in messages:
                msg_id = msg["id"]
                user_id = msg["user"]
                text = msg["message"]
                user = pb("GET", f"/collections/users/records/{user_id}").json()
                ctx = ""
                if user:
                    ctx = f"User skills: {user.get('skills','')}. Desired job: {user.get('desired_job_title','')}"
                prompt = f"{ctx}\nUser: {text}\nAnswer helpfully and suggest job search queries."
                try:
                    resp_ai = ai.chat.completions.create(
                        model="mistral-small-latest",
                        messages=[{"role":"user","content":prompt}],
                        temperature=0.7,
                        max_tokens=150
                    )
                    answer = resp_ai.choices[0].message.content.strip()
                    pb("PATCH", f"/collections/chat_messages/records/{msg_id}", json_data={"response": answer})
                    logging.info(f"Replied to chat {msg_id}")
                except Exception as e:
                    logging.error(f"Chat failed: {e}")
                time.sleep(0.2)
        except Exception as e:
            logging.error(f"Fast chat loop error: {e}")
        time.sleep(10)

# Fast chat loop (runs in a thread, replies every 10 seconds)
def fast_chat_loop():
    while True:
        try:
            resp = pb("GET", "/collections/chat_messages/records?filter=(response='')&sort=created&perPage=10")
            if resp.status_code != 200:
                time.sleep(5)
                continue
            messages = resp.json().get("items", [])
            for msg in messages:
                msg_id = msg["id"]
                user_id = msg["user"]
                text = msg["message"]
                user = pb("GET", f"/collections/users/records/{user_id}").json()
                ctx = ""
                if user:
                    ctx = f"User skills: {user.get('skills','')}. Desired job: {user.get('desired_job_title','')}"
                prompt = f"{ctx}\nUser: {text}\nAnswer helpfully and suggest job search queries."
                try:
                    import openai
                    ai = openai.OpenAI(api_key=os.getenv("MISTRAL_API_KEY"), base_url="https://api.mistral.ai/v1")
                    resp_ai = ai.chat.completions.create(
                        model="mistral-small-latest",
                        messages=[{"role":"user","content":prompt}],
                        temperature=0.7,
                        max_tokens=150
                    )
                    answer = resp_ai.choices[0].message.content.strip()
                    pb("PATCH", f"/collections/chat_messages/records/{msg_id}", json_data={"response": answer})
                    logging.info(f"Replied to chat {msg_id}")
                except Exception as e:
                    logging.error(f"Chat failed: {e}")
                time.sleep(0.2)
        except Exception as e:
            logging.error(f"Fast chat loop error: {e}")
        time.sleep(10)

# Start the chat loop in a background thread
import threading
threading.Thread(target=fast_chat_loop, daemon=True).start()
