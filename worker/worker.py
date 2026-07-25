import os, time, logging, json, threading
import requests
from openai import OpenAI
from serpapi import GoogleSearch

# ---------- KEYS FROM ENVIRONMENT (Render will provide them) ----------
POCKETBASE_URL = "http://localhost:8090"
POCKETBASE_ADMIN_TOKEN = os.getenv("POCKETBASE_ADMIN_TOKEN")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
FINDWORK_KEY = os.getenv("FINDWORK_KEY")

# Validate critical keys
if not POCKETBASE_ADMIN_TOKEN:
    raise Exception("Missing POCKETBASE_ADMIN_TOKEN environment variable")
if not MISTRAL_API_KEY:
    raise Exception("Missing MISTRAL_API_KEY environment variable")

ai = OpenAI(api_key=MISTRAL_API_KEY, base_url="https://api.mistral.ai/v1")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

def pb(method, path, json_data=None):
    url = f"{POCKETBASE_URL}/api/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {POCKETBASE_ADMIN_TOKEN}"}
    return requests.request(method, url, headers=headers, json=json_data)

# ---------- AI QUERY PARSER ----------
def parse_natural_query(raw_query):
    prompt = f"""Extract the key job search parameters from the following user query. Return ONLY a valid JSON object with these keys (use null if not mentioned):
- title: the job title or keywords (string)
- location: the desired location (string, e.g., "Nigeria", "Canada", "Remote")
- remote: true if remote work is requested, false otherwise
- company: specific company name if mentioned, otherwise null
- additional_filters: any other relevant words (string, e.g., "full-time, part-time")

User query: "{raw_query}"

JSON:"""
    try:
        resp = ai.chat.completions.create(
            model="mistral-small-latest",
            messages=[{"role":"user","content":prompt}],
            temperature=0.1,
            max_tokens=200
        )
        content = resp.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("\n", 1)[0]
        params = json.loads(content)
        logging.info(f"Parsed query '{raw_query}' -> {params}")
        return params
    except Exception as e:
        logging.error(f"Query parsing failed: {e}, using raw query as title")
        return {"title": raw_query, "location": None, "remote": False, "company": None, "additional_filters": None}

def build_structured_search(params):
    title = params.get("title") or ""
    location = params.get("location") or ""
    remote = params.get("remote", False)
    company = params.get("company") or ""
    extra = params.get("additional_filters") or ""
    query_parts = [title, company, extra]
    if remote:
        query_parts.append("remote")
    query = " ".join(filter(None, query_parts)).strip()
    return query, location if location else "United States"

# ---------- CONVERSATION MEMORY ----------
def get_conversation_history(user_id, current_msg_id=None, limit=15):
    resp = pb("GET", f"/collections/chat_messages/records?filter=(user='{user_id}')&sort=created&perPage={limit}")
    if resp.status_code != 200:
        return []
    items = resp.json().get("items", [])
    if current_msg_id:
        items = [m for m in items if m["id"] != current_msg_id]
    dialogue = []
    for m in items:
        if m["message"]:
            dialogue.append(f"User: {m['message']}")
        if m["response"]:
            dialogue.append(f"Assistant: {m['response']}")
    return "\n".join(dialogue)

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
                user = pb("GET", f"/collections/users/records/{user_id}").json()
                profile = ""
                if user:
                    profile = f"User profile: skills={user.get('skills','')}, desired job={user.get('desired_job_title','')}"
                history = get_conversation_history(user_id, current_msg_id=msg_id, limit=15)
                full_prompt = f"""
You are a helpful, friendly, and knowledgeable career coach and interview trainer.
Your name is JobSeeker AI Coach. You remember all previous conversations with this user.

{profile}

Conversation history:
{history if history else 'No previous conversation.'}

The user just said: "{text}"

Respond helpfully, keeping the context of the conversation.
"""
                try:
                    resp_ai = ai.chat.completions.create(
                        model="mistral-small-latest",
                        messages=[{"role":"user","content":full_prompt}],
                        temperature=0.7,
                        max_tokens=300
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

# ---------- JOB SOURCES (unchanged, using the env‑provided keys) ----------
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

def search_serpapi(query, location="United States", num=10):
    if not SERPAPI_KEY: return []
    try:
        params = {"engine": "google_jobs", "q": query, "location": location, "hl": "en", "api_key": SERPAPI_KEY, "num": num}
        search = GoogleSearch(params)
        results = search.get_dict()
        jobs = []
        for j in results.get("jobs_results", []):
            desc = j.get("description", "")
            jobs.append({"title": j.get("title"), "company": j.get("company_name"), "description": desc, "location": j.get("location"), "remote": any(w in desc.lower() for w in ["remote","work from home"]), "application_link": j.get("apply_link","") or j.get("share_link",""), "source_url": j.get("share_link",""), "posted_date": j.get("detected_extensions",{}).get("posted_at",""), "match_score": 0})
        logging.info(f"SerpAPI: {len(jobs)} jobs for '{query}' in '{location}'")
        return jobs
    except Exception as e:
        logging.error(f"SerpAPI error: {e}")
        return []

def search_adzuna(query, location="United States", num=10):
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY: return []
    try:
        params = {"app_id": ADZUNA_APP_ID, "app_key": ADZUNA_APP_KEY, "what": query, "where": location, "max_days_old": 30, "results_per_page": min(num, 50)}
        resp = requests.get("https://api.adzuna.com/v1/api/jobs/us/search/1", params=params)
        data = resp.json()
        jobs = []
        for r in data.get("results", []):
            jobs.append({"title": r.get("title"), "company": r.get("company",{}).get("display_name",""), "description": r.get("description",""), "location": r.get("location",{}).get("display_name",""), "remote": False, "application_link": r.get("redirect_url",""), "source_url": r.get("redirect_url",""), "posted_date": r.get("created",""), "match_score": 0})
        logging.info(f"Adzuna: {len(jobs)} jobs for '{query}'")
        return jobs
    except Exception as e:
        logging.error(f"Adzuna: {e}")
        return []

def search_remotive(query, num=10):
    try:
        url = f"https://remotive.com/api/remote-jobs?search={query}"
        resp = requests.get(url)
        data = resp.json()
        jobs = []
        for j in data.get("jobs",[])[:num]:
            jobs.append({"title": j["title"], "company": j["company_name"], "description": j.get("description",""), "location": j.get("candidate_required_location",""), "remote": True, "application_link": j.get("url",""), "source_url": j.get("url",""), "posted_date": j.get("publication_date",""), "match_score": 0})
        logging.info(f"Remotive: {len(jobs)} jobs for '{query}'")
        return jobs
    except Exception as e:
        logging.error(f"Remotive: {e}")
        return []

def search_remoteok(query, num=10):
    try:
        url = f"https://remoteok.com/api?search={query}"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        data = resp.json()
        jobs = []
        for j in data[1:]:
            jobs.append({"title": j.get("position",""), "company": j.get("company",""), "description": j.get("description",""), "location": j.get("location",""), "remote": True, "application_link": j.get("url",""), "source_url": j.get("url",""), "posted_date": j.get("epoch",""), "match_score": 0})
        logging.info(f"RemoteOK: {len(jobs)} jobs for '{query}'")
        return jobs[:num]
    except Exception as e:
        logging.error(f"RemoteOK: {e}")
        return []

def normalize_and_deduplicate(jobs):
    seen = set()
    unique = []
    for job in jobs:
        if not job.get("title"): continue
        key = f"{job['title']}|{job.get('company','')}|{job.get('source_url','')}"
        if key in seen: continue
        seen.add(key)
        unique.append({"title": job["title"], "company": job.get("company",""), "description": job.get("description",""), "location": job.get("location",""), "remote": job.get("remote", False), "application_link": job.get("application_link",""), "source_url": job.get("source_url",""), "posted_date": job.get("posted_date",""), "match_score": 0})
    return unique

def insert_jobs_if_new(jobs):
    """Insert jobs and return list of new record IDs."""
    inserted_ids = []
    for job in jobs:
        if not job["title"]: continue
        filter_str = f"(title='{job['title']}'&&company='{job['company']}'&&source_url='{job['source_url']}')"
        resp = pb("GET", f"/collections/job_listings/records?filter={filter_str}")
        if resp.status_code == 200 and resp.json()["totalItems"] == 0:
            create_resp = pb("POST", "/collections/job_listings/records", json_data=job)
            if create_resp.status_code == 200:
                inserted_ids.append(create_resp.json()["id"])
        time.sleep(0.05)
    logging.info(f"Inserted {len(inserted_ids)} new jobs out of {len(jobs)}")
    return inserted_ids

# ---------- SEARCH REQUEST PROCESSOR ----------
def process_search_requests():
    while True:
        try:
            resp = pb("GET", "/collections/job_search_requests/records?filter=(status='pending')&sort=created&perPage=5")
            if resp.status_code != 200:
                time.sleep(5)
                continue
            for req in resp.json().get("items", []):
                req_id = req["id"]
                raw_query = req["query"]
                logging.info(f"Processing search request {req_id}: '{raw_query}'")
                pb("PATCH", f"/collections/job_search_requests/records/{req_id}", json_data={"status":"running"})
                params = parse_natural_query(raw_query)
                query, location = build_structured_search(params)
                logging.info(f"Final search: query='{query}', location='{location}'")
                all_jobs = []
                all_jobs.extend(search_serpapi(query, location, num=15))
                all_jobs.extend(search_adzuna(query, location, num=15))
                all_jobs.extend(search_remotive(query, num=15))
                all_jobs.extend(search_remoteok(query, num=15))
                unique = normalize_and_deduplicate(all_jobs)
                new_ids = insert_jobs_if_new(unique)
                pb("PATCH", f"/collections/job_search_requests/records/{req_id}", json_data={"status":"completed", "results": new_ids})
                logging.info(f"Search request {req_id} completed, {len(new_ids)} new jobs")
        except Exception as e:
            logging.error(f"Search request loop error: {e}")
        time.sleep(10)

def scraping_loop():
    while True:
        logging.info("=== Lightweight job cycle ===")
        users = fetch_all_users()
        for user in users:
            query = build_search_query(user)
            if not query: continue
            loc = user.get("location") or "United States"
            all_jobs = []
            all_jobs.extend(search_serpapi(query, loc, num=10))
            all_jobs.extend(search_adzuna(query, loc, num=10))
            all_jobs.extend(search_remotive(query, num=10))
            all_jobs.extend(search_remoteok(query, num=10))
            unique = normalize_and_deduplicate(all_jobs)
            insert_jobs_if_new(unique)
            time.sleep(2)
        logging.info("Cycle complete. Sleeping 10 minutes.")
        time.sleep(600)

if __name__ == "__main__":
    threading.Thread(target=fast_chat_loop, daemon=True).start()
    threading.Thread(target=process_search_requests, daemon=True).start()
    scraping_loop()
