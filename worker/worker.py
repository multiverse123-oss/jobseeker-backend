import os, time, logging, json
import requests
from dotenv import load_dotenv
from fpdf import FPDF, XPos, YPos
from openai import OpenAI
from jobspy import scrape_jobs

load_dotenv()

POCKETBASE_URL = os.getenv("POCKETBASE_URL", "http://localhost:8090")
POCKETBASE_ADMIN_TOKEN = os.getenv("POCKETBASE_ADMIN_TOKEN")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")

if not POCKETBASE_ADMIN_TOKEN: raise Exception("Missing POCKETBASE_ADMIN_TOKEN")
if not MISTRAL_API_KEY: raise Exception("Missing MISTRAL_API_KEY")

ai_client = OpenAI(api_key=MISTRAL_API_KEY, base_url="https://api.mistral.ai/v1")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def pb_request(method, path, json_data=None, files=None):
    url = f"{POCKETBASE_URL}/api/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {POCKETBASE_ADMIN_TOKEN}"}
    if files:
        return requests.request(method, url, headers=headers, files=files)
    else:
        return requests.request(method, url, headers=headers, json=json_data)

# ------------------- USERS & PROFILE HELPERS -------------------
def fetch_all_users():
    resp = pb_request("GET", "/collections/users/records")
    if resp.status_code == 200:
        return resp.json().get("items", [])
    return []

def build_search_query(user):
    """Combine skills + desired job title for a precise search."""
    skills = user.get("skills", "") or ""
    title = user.get("desired_job_title", "") or ""
    parts = [skills]
    if title:
        parts.insert(0, title)
    query = " ".join(parts).strip()
    if user.get("remote_preference") == "remote":
        query += " remote"
    return query

# ------------------- JOB SCRAPING & DEDUP -------------------
def job_exists(title, company, source_url):
    filter_str = f"(title='{title}'&&company='{company}'&&source_url='{source_url}')"
    resp = pb_request("GET", f"/collections/job_listings/records?filter={filter_str}")
    return resp.json().get("totalItems", 0) > 0 if resp.status_code == 200 else False

def insert_job(job_dict):
    resp = pb_request("POST", "/collections/job_listings/records", json_data=job_dict)
    return resp.json() if resp.status_code == 200 else None

def search_and_store_jobs(query, location="United States", results_wanted=30):
    """Scrape and store jobs, returning list of stored record IDs."""
    stored_ids = []
    try:
        jobs_df = scrape_jobs(
            site_name=["indeed","linkedin","glassdoor","google"],
            search_term=query, location=location,
            results_wanted=results_wanted, country_indeed='USA'
        )
        records = jobs_df.to_dict(orient="records")
        for job_raw in records:
            job = normalize_job(job_raw)
            if not job["title"]:
                continue
            if not job_exists(job["title"], job["company"], job["source_url"]):
                inserted = insert_job(job)
                if inserted:
                    stored_ids.append(inserted["id"])
                    logging.info(f"Inserted job: {job['title']} at {job['company']}")
    except Exception as e:
        logging.error(f"JobSpy error: {e}")
    return stored_ids

def normalize_job(job):
    desc = str(job.get("description", ""))
    return {
        "title": str(job.get("title","")),
        "company": str(job.get("company","")),
        "description": desc,
        "location": str(job.get("location","")),
        "remote": any(w in desc.lower() for w in ["remote","work from home"]),
        "application_link": str(job.get("job_url","")),
        "application_email": "",
        "source_url": str(job.get("job_url","")),
        "posted_date": str(job.get("date_posted","")),
        "match_score": 0,
        "analysis": None
    }

# ------------------- IMMEDIATE SEARCH REQUESTS -------------------
def process_search_requests():
    """Handle pending job_search_requests."""
    resp = pb_request("GET", "/collections/job_search_requests/records?filter=(status='pending')")
    if resp.status_code != 200:
        return
    requests = resp.json().get("items", [])
    for req in requests:
        req_id = req["id"]
        user_id = req["user"]
        query = req["query"]
        logging.info(f"Processing search request {req_id} for query: {query}")
        pb_request("PATCH", f"/collections/job_search_requests/records/{req_id}", json_data={"status":"running"})
        user = pb_request("GET", f"/collections/users/records/{user_id}").json()
        location = user.get("location", "United States") if user else "United States"
        job_ids = search_and_store_jobs(query, location=location, results_wanted=30)
        pb_request("PATCH", f"/collections/job_search_requests/records/{req_id}",
                   json_data={"status":"completed", "results": job_ids})

# ------------------- CHAT HANDLING -------------------
def process_chat_messages():
    """Answer any unanswered chat messages with Mistral."""
    resp = pb_request("GET", "/collections/chat_messages/records?filter=(response='')&sort=created")
    if resp.status_code != 200:
        return
    messages = resp.json().get("items", [])
    for msg in messages:
        msg_id = msg["id"]
        user_id = msg["user"]
        text = msg["message"]
        # Get user's skills/desired title for context
        user = pb_request("GET", f"/collections/users/records/{user_id}").json()
        context = ""
        if user:
            context = f"User skills: {user.get('skills','')}. Desired job: {user.get('desired_job_title','')}"
        prompt = f"{context}\nUser asked: {text}\nAnswer helpfully and suggest relevant job search queries."
        try:
            resp = ai_client.chat.completions.create(
                model="mistral-small-latest",
                messages=[{"role":"user","content":prompt}],
                temperature=0.7, max_tokens=500
            )
            answer = resp.choices[0].message.content.strip()
            pb_request("PATCH", f"/collections/chat_messages/records/{msg_id}", json_data={"response": answer})
            logging.info(f"Replied to chat message {msg_id}")
        except Exception as e:
            logging.error(f"Chat reply failed: {e}")

# ------------------- MAIN 24/7 LOOP (already runs every 60 seconds) -------------------
def main_loop():
    while True:
        logging.info("=== JobSeeker AI cycle start ===")
        # 1. Process immediate search requests (user-triggered)
        process_search_requests()
        # 2. Answer chat messages
        process_chat_messages()
        # 3. Regular background scraping for all users
        users = fetch_all_users()
        for user in users:
            uid = user["id"]
            query = build_search_query(user)
            if not query:
                continue
            logging.info(f"Background scrape for user {uid}: {query}")
            search_and_store_jobs(query, location=user.get("location", "United States"), results_wanted=15)
            time.sleep(2)  # small gap between users

        logging.info("Cycle complete. Sleeping 60 seconds.")
        time.sleep(60)

if __name__ == "__main__":
    main_loop()
