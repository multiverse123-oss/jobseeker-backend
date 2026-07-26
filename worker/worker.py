import os, time, logging, json, threading, traceback, re
import requests
from openai import OpenAI
from serpapi import GoogleSearch

POCKETBASE_URL = "http://localhost:8090"
POCKETBASE_ADMIN_TOKEN = os.getenv("POCKETBASE_ADMIN_TOKEN")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
if not POCKETBASE_ADMIN_TOKEN: raise Exception("Missing POCKETBASE_ADMIN_TOKEN")
if not MISTRAL_API_KEY: raise Exception("Missing MISTRAL_API_KEY")

ai = OpenAI(api_key=MISTRAL_API_KEY, base_url="https://api.mistral.ai/v1")

def pb(method, path, json_data=None):
    url = f"{POCKETBASE_URL}/api/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {POCKETBASE_ADMIN_TOKEN}"}
    return requests.request(method, url, headers=headers, json=json_data)

def strip_html(text):
    clean = re.sub(r'<[^>]+>', '', text)
    clean = re.sub(r'\s+', ' ', clean)
    return clean.strip()

# ---------- AI QUERY PARSER ----------
def parse_natural_query(raw_query):
    prompt = f"""Extract the key job search parameters from the following user query. Return ONLY a valid JSON object with these keys (use null if not mentioned):
- title: the job title or keywords (string)
- location: the desired location (string)
- remote: true if remote work is requested, false otherwise
- company: specific company name if mentioned, otherwise null
- additional_filters: any other relevant words (string)

User query: "{raw_query}"

JSON:"""
    try:
        resp = ai.chat.completions.create(
            model="mistral-small-latest",
            messages=[{"role":"user","content":prompt}],
            temperature=0.1, max_tokens=200
        )
        content = resp.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("\n", 1)[0]
        return json.loads(content)
    except Exception as e:
        logging.error(f"Query parsing failed: {e}")
        return {"title": raw_query, "location": None, "remote": False, "company": None, "additional_filters": None}

# ---------- COUNTRY LOOKUP FOR ADZUNA ----------
COUNTRY_MAP = {
    "united states": "us", "usa": "us", "us": "us",
    "canada": "ca", "ca": "ca",
    "united kingdom": "gb", "uk": "gb", "gb": "gb",
    "australia": "au", "au": "au",
    "germany": "de", "de": "de",
    "switzerland": "ch", "ch": "ch",
    "france": "fr", "fr": "fr",
    "netherlands": "nl", "nl": "nl",
    "italy": "it", "it": "it",
    "spain": "es", "es": "es",
    "brazil": "br", "br": "br",
    "india": "in", "in": "in",
    "mexico": "mx", "mx": "mx",
}

def get_adzuna_country(location):
    if not location:
        return None
    loc = location.lower().strip()
    if loc in COUNTRY_MAP:
        return COUNTRY_MAP[loc]
    for name, code in COUNTRY_MAP.items():
        if name in loc:
            return code
    return None

def search_adzuna_country(query, location, country_code, num=15):
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        return []
    try:
        url = f"https://api.adzuna.com/v1/api/jobs/{country_code}/search/1"
        params = {
            "app_id": ADZUNA_APP_ID,
            "app_key": ADZUNA_APP_KEY,
            "what": query,
            "where": location,
            "max_days_old": 30,
            "results_per_page": min(num, 50)
        }
        resp = requests.get(url, params=params)
        data = resp.json()
        jobs = []
        for r in data.get("results", []):
            jobs.append({
                "title": r.get("title"),
                "company": r.get("company", {}).get("display_name", ""),
                "description": strip_html(r.get("description", "")),
                "location": r.get("location", {}).get("display_name", ""),
                "remote": False,
                "application_link": r.get("redirect_url", ""),
                "source_url": r.get("redirect_url", ""),
                "posted_date": r.get("created", ""),
                "match_score": 0
            })
        logging.info(f"Adzuna ({country_code}): {len(jobs)} jobs for '{query}' in '{location}'")
        return jobs
    except Exception as e:
        logging.error(f"Adzuna ({country_code}) error: {e}")
        return []

# ---------- OTHER SOURCES ----------
def search_serpapi(query, location="United States", num=15):
    if not SERPAPI_KEY: return []
    try:
        params = {"engine": "google_jobs", "q": query, "location": location, "hl": "en", "api_key": SERPAPI_KEY, "num": num}
        search = GoogleSearch(params)
        results = search.get_dict()
        jobs = []
        for j in results.get("jobs_results", []):
            desc = strip_html(j.get("description", ""))
            jobs.append({"title": j.get("title"), "company": j.get("company_name"), "description": desc, "location": j.get("location"), "remote": any(w in desc.lower() for w in ["remote","work from home"]), "application_link": j.get("apply_link","") or j.get("share_link",""), "source_url": j.get("share_link",""), "posted_date": j.get("detected_extensions",{}).get("posted_at",""), "match_score": 0})
        logging.info(f"SerpAPI: {len(jobs)} jobs for '{query}' in '{location}'")
        return jobs
    except Exception as e:
        logging.error(f"SerpAPI error: {e}")
        return []

def search_remotive(query, num=15):
    try:
        url = f"https://remotive.com/api/remote-jobs?search={query}"
        resp = requests.get(url)
        data = resp.json()
        jobs = []
        for j in data.get("jobs",[])[:num]:
            jobs.append({"title": j["title"], "company": j["company_name"], "description": strip_html(j.get("description","")), "location": j.get("candidate_required_location",""), "remote": True, "application_link": j.get("url",""), "source_url": j.get("url",""), "posted_date": j.get("publication_date",""), "match_score": 0})
        logging.info(f"Remotive: {len(jobs)} jobs for '{query}'")
        return jobs
    except Exception as e:
        logging.error(f"Remotive: {e}")
        return []

def search_remoteok(query, num=15):
    try:
        url = f"https://remoteok.com/api?search={query}"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        data = resp.json()
        jobs = []
        for j in data[1:]:
            jobs.append({"title": j.get("position",""), "company": j.get("company",""), "description": strip_html(j.get("description","")), "location": j.get("location",""), "remote": True, "application_link": j.get("url",""), "source_url": j.get("url",""), "posted_date": j.get("epoch",""), "match_score": 0})
        logging.info(f"RemoteOK: {len(jobs)} jobs for '{query}'")
        return jobs[:num]
    except Exception as e:
        logging.error(f"RemoteOK: {e}")
        return []

# ---------- MISTRAL LARGE WEB SEARCH (NEW BONUS SOURCE) ----------
def search_mistral_large_web(title, location, num=15):
    """Use mistral-large-latest with web_search tool to find jobs."""
    try:
        prompt = f"""
Search the web for real, current job postings for "{title}" in {location}.
Return ONLY a JSON array of job objects. Each object must have these exact keys:
- title: the job title (string)
- company: the company name (string)
- location: the job's location (string)
- description: a short description (max 300 chars)
- application_link: the URL where to apply (string)
- remote: true if remote, otherwise false

Only include jobs that are actually located in {location}. If no jobs are found, return an empty array [].
Do NOT include any other text or explanations.
"""
        resp = ai.chat.completions.create(
            model="mistral-large-latest",
            messages=[{"role":"user","content":prompt}],
            temperature=0.1,
            max_tokens=2000,
            tools=[{"type": "web_search"}],
            tool_choice="auto"
        )
        # The response may have tool_calls or direct content.
        # Usually Mistral returns the final answer in the message content after using the tool.
        content = resp.choices[0].message.content.strip()
        if not content:
            # If content is empty, maybe the model wants to use tools? Check for tool_calls
            if resp.choices[0].message.tool_calls:
                # Not handling complex tool execution here; just log and skip.
                logging.warning("Mistral Large web search returned tool calls instead of content")
                return []
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("\n", 1)[0]
        jobs = json.loads(content)
        logging.info(f"Mistral Large Web: {len(jobs)} jobs for '{title}' in '{location}'")
        results = []
        for j in jobs:
            results.append({
                "title": j.get("title", ""),
                "company": j.get("company", ""),
                "description": strip_html(j.get("description", "")),
                "location": j.get("location", ""),
                "remote": j.get("remote", False),
                "application_link": j.get("application_link", ""),
                "source_url": j.get("application_link", ""),
                "posted_date": "",
                "match_score": 0
            })
        return results
    except Exception as e:
        logging.error(f"Mistral Large Web error: {e}")
        return []

def normalize_and_deduplicate(jobs):
    seen = set()
    unique = []
    for job in jobs:
        if not job.get("title"): continue
        key = f"{job['title']}|{job.get('company','')}|{job.get('source_url','')}"
        if key in seen: continue
        seen.add(key)
        unique.append(job)
    return unique

def location_match(job, desired_location):
    if not desired_location: return False
    return desired_location.lower() in job.get("location", "").lower()

def agentic_job_search(title, location, num_per_source=15):
    all_jobs = []
    # 1. SerpAPI (Google Jobs)
    all_jobs.extend(search_serpapi(f"{title} {location}", location, num=num_per_source))
    # 2. Adzuna – country‑specific
    country_code = get_adzuna_country(location)
    if country_code:
        all_jobs.extend(search_adzuna_country(title, location, country_code, num=num_per_source))
    else:
        all_jobs.extend(search_adzuna_country(title, location, "us", num=num_per_source))
    # 3. Remote job boards
    all_jobs.extend(search_remotive(title, num=num_per_source))
    all_jobs.extend(search_remoteok(title, num=num_per_source))
    # 4. **NEW** Mistral Large with web search (bonus)
    all_jobs.extend(search_mistral_large_web(title, location, num=num_per_source))
    unique = normalize_and_deduplicate(all_jobs)
    exact = [j for j in unique if location_match(j, location)]
    others = [j for j in unique if not location_match(j, location)]
    combined = exact + others[:max(0, 100 - len(exact))]
    logging.info(f"Agentic search: {len(exact)} exact matches, {len(combined)} total returned.")
    return combined, len(exact)

def insert_or_get_ids(jobs):
    all_ids = []
    for job in jobs:
        if not job["title"]: continue
        filter_str = f"(title='{job['title']}'&&company='{job['company']}'&&source_url='{job['source_url']}')"
        resp = pb("GET", f"/collections/job_listings/records?filter={filter_str}")
        if resp.status_code == 200 and resp.json()["totalItems"] > 0:
            all_ids.append(resp.json()["items"][0]["id"])
        else:
            create_resp = pb("POST", "/collections/job_listings/records", json_data=job)
            if create_resp.status_code == 200:
                all_ids.append(create_resp.json()["id"])
        time.sleep(0.05)
    return all_ids

def process_search_requests():
    logging.info("Search request processor thread started.")
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
                title = params.get("title") or raw_query
                location = params.get("location") or "United States"
                jobs, exact_count = agentic_job_search(title, location, num_per_source=15)
                job_ids = insert_or_get_ids(jobs)
                pb("PATCH", f"/collections/job_search_requests/records/{req_id}", json_data={
                    "status": "completed",
                    "results": job_ids,
                    "exact_match_count": exact_count
                })
                logging.info(f"Search request {req_id} completed, {len(job_ids)} jobs, {exact_count} exact")
        except Exception as e:
            logging.error(f"Search request loop error: {e}")
            traceback.print_exc()
        time.sleep(10)

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
                profile = f"User profile: skills={user.get('skills','')}, desired job={user.get('desired_job_title','')}" if user else ""
                prompt = f"You are a helpful career coach. {profile}\nUser: {text}\nRespond helpfully."
                try:
                    resp_ai = ai.chat.completions.create(
                        model="mistral-small-latest",
                        messages=[{"role":"user","content":prompt}],
                        temperature=0.7, max_tokens=300
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

def scraping_loop():
    while True:
        users = pb("GET", "/collections/users/records").json().get("items", [])
        for user in users:
            query = " ".join(filter(None, [user.get("desired_job_title","").strip(), user.get("skills","").strip()]))
            if not query: continue
            if user.get("remote_preference") == "remote": query += " remote"
            loc = user.get("location") or "United States"
            jobs = []
            jobs.extend(search_serpapi(query, loc, num=10))
            jobs.extend(search_remotive(query, num=10))
            jobs.extend(search_remoteok(query, num=10))
            unique = normalize_and_deduplicate(jobs)
            insert_or_get_ids(unique)
            time.sleep(2)
        logging.info("Scraping cycle complete. Sleeping 10 minutes.")
        time.sleep(600)

if __name__ == "__main__":
    logging.info("Starting JobSeeker worker threads...")
    threading.Thread(target=fast_chat_loop, daemon=True).start()
    threading.Thread(target=process_search_requests, daemon=True).start()
    scraping_loop()
