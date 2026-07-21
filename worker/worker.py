import os, time, logging, json
import requests
from dotenv import load_dotenv
from openai import OpenAI
from jobspy import scrape_jobs

load_dotenv()

POCKETBASE_URL = os.getenv("POCKETBASE_URL", "http://localhost:8090")
POCKETBASE_ADMIN_TOKEN = os.getenv("POCKETBASE_ADMIN_TOKEN")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")

ai = OpenAI(api_key=MISTRAL_API_KEY, base_url="https://api.mistral.ai/v1")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def pb(method, path, json_data=None, files=None):
    url = f"{POCKETBASE_URL}/api/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {POCKETBASE_ADMIN_TOKEN}"}
    if files:
        return requests.request(method, url, headers=headers, files=files)
    return requests.request(method, url, headers=headers, json=json_data)

def fetch_all_users():
    resp = pb("GET", "/collections/users/records")
    return resp.json().get("items", []) if resp.status_code == 200 else []

def build_search_query(user):
    title = user.get("desired_job_title", "").strip()
    skills = user.get("skills", "").strip()
    query = " ".join(filter(None, [title, skills]))
    if user.get("remote_preference") == "remote":
        query += " remote"
    return query

def normalize_and_deduplicate(jobs):
    seen = set()
    unique = []
    for job in jobs:
        title = str(job.get("title",""))
        company = str(job.get("company",""))
        url = str(job.get("job_url",""))
        key = f"{title}|{company}|{url}"
        if key in seen:
            continue
        seen.add(key)
        desc = str(job.get("description",""))
        unique.append({
            "title": title,
            "company": company,
            "description": desc,
            "location": str(job.get("location","")),
            "remote": any(w in desc.lower() for w in ["remote","work from home"]),
            "application_link": url,
            "application_email": "",
            "source_url": url,
            "posted_date": str(job.get("date_posted","")),
            "match_score": 0
        })
    return unique

def insert_jobs_if_new(job_dicts):
    inserted = []
    for job in job_dicts:
        if not job["title"]:
            continue
        # Check existence
        filter_str = f"(title='{job['title']}'&&company='{job['company']}'&&source_url='{job['source_url']}')"
        resp = pb("GET", f"/collections/job_listings/records?filter={filter_str}")
        if resp.status_code == 200 and resp.json()["totalItems"] == 0:
            resp2 = pb("POST", "/collections/job_listings/records", json_data=job)
            if resp2.status_code == 200:
                inserted.append(resp2.json()["id"])
                logging.info(f"Inserted: {job['title']} at {job['company']}")
        time.sleep(0.1)  # tiny pause to avoid hammering the DB
    return inserted

def search_and_store(query, location="United States", results_wanted=50):
    logging.info(f"Searching: {query} in {location}")
    try:
        df = scrape_jobs(
            site_name=["indeed","linkedin","glassdoor","google"],
            search_term=query,
            location=location,
            results_wanted=results_wanted,
            country_indeed='USA'
        )
        jobs_raw = df.to_dict(orient="records")
        clean = normalize_and_deduplicate(jobs_raw)
        return insert_jobs_if_new(clean)
    except Exception as e:
        logging.error(f"JobSpy error: {e}")
        return []

# Immediate search requests
def process_search_requests():
    resp = pb("GET", "/collections/job_search_requests/records?filter=(status='pending')")
    if resp.status_code != 200:
        return
    for req in resp.json().get("items", []):
        req_id = req["id"]
        user_id = req["user"]
        query = req["query"]
        pb("PATCH", f"/collections/job_search_requests/records/{req_id}", json_data={"status":"running"})
        user = pb("GET", f"/collections/users/records/{user_id}").json()
        location = user.get("location") if user else "United States"
        ids = search_and_store(query, location=location, results_wanted=50)
        pb("PATCH", f"/collections/job_search_requests/records/{req_id}", json_data={"status":"completed","results":ids})
        time.sleep(2)

# Chat replies
def process_chat():
    resp = pb("GET", "/collections/chat_messages/records?filter=(response='')&sort=created")
    if resp.status_code != 200:
        return
    for msg in resp.json().get("items", []):
        msg_id = msg["id"]
        user_id = msg["user"]
        text = msg["message"]
        user = pb("GET", f"/collections/users/records/{user_id}").json()
        ctx = f"User skills: {user.get('skills','')}. Desired job: {user.get('desired_job_title','')}" if user else ""
        prompt = f"{ctx}\nUser: {text}\nAnswer helpfully and suggest job search queries."
        try:
            resp_ai = ai.chat.completions.create(
                model="mistral-small-latest",
                messages=[{"role":"user","content":prompt}],
                temperature=0.7, max_tokens=500
            )
            answer = resp_ai.choices[0].message.content.strip()
            pb("PATCH", f"/collections/chat_messages/records/{msg_id}", json_data={"response": answer})
            logging.info(f"Replied to chat {msg_id}")
        except Exception as e:
            logging.error(f"Chat failed: {e}")
        time.sleep(1)

# Main 24/7 loop
def main_loop():
    while True:
        logging.info("=== Cycle start ===")
        process_search_requests()
        process_chat()
        users = fetch_all_users()
        for user in users:
            query = build_search_query(user)
            if not query:
                continue
            loc = user.get("location") or "United States"
            search_and_store(query, location=loc, results_wanted=50)
            time.sleep(3)  # respect rate limits between users
        logging.info("Cycle complete. Sleeping 5 minutes.")
        time.sleep(300)   # wait 5 minutes between full cycles

if __name__ == "__main__":
    main_loop()
