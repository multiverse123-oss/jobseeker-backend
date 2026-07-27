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
FINDWORK_KEY = os.getenv("FINDWORK_KEY")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
JSEARCH_HOST = "jsearch.p.rapidapi.com"

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

# ---------- AI QUERY PARSER (unchanged) ----------
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

# ---------- AI JOB TITLE GENERATOR ----------
def generate_job_titles(location, count=8):
    prompt = f"""List {count} of the most common job titles that people search for when looking for employment in {location}. 
Return ONLY a JSON array of strings, no other text. Example: ["software engineer", "nurse", "teacher", "driver", "accountant"]"""
    try:
        resp = ai.chat.completions.create(
            model="mistral-small-latest",
            messages=[{"role":"user","content":prompt}],
            temperature=0.7, max_tokens=300
        )
        content = resp.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("\n", 1)[0]
        titles = json.loads(content)
        logging.info(f"Generated {len(titles)} job titles for {location}: {titles}")
        return titles[:count]
    except Exception as e:
        logging.error(f"Job title generation failed: {e}")
        return []

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
    if not location: return None
    loc = location.lower().strip()
    if loc in COUNTRY_MAP: return COUNTRY_MAP[loc]
    for name, code in COUNTRY_MAP.items():
        if name in loc: return code
    return None

# ---------- INDIVIDUAL SOURCES ----------
def search_serpapi(query, location="United States", num=10):
    if not SERPAPI_KEY: return []
    try:
        params = {"engine": "google_jobs", "q": query, "location": location, "hl": "en", "api_key": SERPAPI_KEY, "num": num}
        search = GoogleSearch(params)
        results = search.get_dict()
        jobs = []
        for j in results.get("jobs_results", []):
            desc = strip_html(j.get("description", ""))
            jobs.append({"title": j.get("title"), "company": j.get("company_name"), "description": desc, "location": j.get("location"), "remote": any(w in desc.lower() for w in ["remote","work from home"]), "application_link": j.get("apply_link","") or j.get("share_link",""), "source_url": j.get("share_link",""), "posted_date": j.get("detected_extensions",{}).get("posted_at",""), "match_score": 0})
        logging.info(f"SerpAPI: {len(jobs)} jobs for '{query}'")
        return jobs
    except Exception as e:
        logging.error(f"SerpAPI error: {e}")
        return []

def search_adzuna_country(query, location, country_code, num=10):
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY: return []
    try:
        url = f"https://api.adzuna.com/v1/api/jobs/{country_code}/search/1"
        params = {"app_id": ADZUNA_APP_ID, "app_key": ADZUNA_APP_KEY, "what": query, "where": location, "max_days_old": 30, "results_per_page": min(num, 50)}
        resp = requests.get(url, params=params)
        data = resp.json()
        jobs = []
        for r in data.get("results", []):
            jobs.append({"title": r.get("title"), "company": r.get("company", {}).get("display_name", ""), "description": strip_html(r.get("description", "")), "location": r.get("location", {}).get("display_name", ""), "remote": False, "application_link": r.get("redirect_url", ""), "source_url": r.get("redirect_url", ""), "posted_date": r.get("created", ""), "match_score": 0})
        logging.info(f"Adzuna ({country_code}): {len(jobs)} jobs for '{query}'")
        return jobs
    except Exception as e:
        logging.error(f"Adzuna error: {e}")
        return []

def search_remotive(query, num=10):
    try:
        resp = requests.get(f"https://remotive.com/api/remote-jobs?search={query}")
        data = resp.json()
        jobs = []
        for j in data.get("jobs",[])[:num]:
            jobs.append({"title": j["title"], "company": j["company_name"], "description": strip_html(j.get("description","")), "location": j.get("candidate_required_location",""), "remote": True, "application_link": j.get("url",""), "source_url": j.get("url",""), "posted_date": j.get("publication_date",""), "match_score": 0})
        logging.info(f"Remotive: {len(jobs)} jobs for '{query}'")
        return jobs
    except Exception as e:
        logging.error(f"Remotive error: {e}")
        return []

def search_remoteok(query, num=10):
    try:
        resp = requests.get(f"https://remoteok.com/api?search={query}", headers={"User-Agent": "Mozilla/5.0"})
        data = resp.json()
        jobs = []
        for j in data[1:]:
            jobs.append({"title": j.get("position",""), "company": j.get("company",""), "description": strip_html(j.get("description","")), "location": j.get("location",""), "remote": True, "application_link": j.get("url",""), "source_url": j.get("url",""), "posted_date": j.get("epoch",""), "match_score": 0})
        logging.info(f"RemoteOK: {len(jobs)} jobs for '{query}'")
        return jobs[:num]
    except Exception as e:
        logging.error(f"RemoteOK error: {e}")
        return []

def search_findwork(query, num=10):
    if not FINDWORK_KEY: return []
    try:
        resp = requests.get(f"https://findwork.dev/api/jobs/?search={query}", headers={"Authorization": f"Token {FINDWORK_KEY}"})
        data = resp.json()
        jobs = []
        for j in data.get("results", [])[:num]:
            jobs.append({"title": j["role"], "company": j["company_name"], "description": strip_html(j.get("text","")), "location": j.get("location",""), "remote": j.get("remote", False), "application_link": j.get("url",""), "source_url": j.get("url",""), "posted_date": j.get("date_posted",""), "match_score": 0})
        logging.info(f"FindWork: {len(jobs)} jobs for '{query}'")
        return jobs
    except Exception as e:
        logging.error(f"FindWork error: {e}")
        return []

def search_jsearch(query, location=None, num=10):
    if not RAPIDAPI_KEY: return []
    try:
        headers = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": JSEARCH_HOST}
        params = {"query": query, "page": "1", "num_pages": "1", "date_posted": "all"}
        if location: params["location"] = location
        resp = requests.get("https://jsearch.p.rapidapi.com/search", headers=headers, params=params)
        data = resp.json()
        jobs = []
        for r in data.get("data", []):
            jobs.append({"title": r.get("job_title"), "company": r.get("employer_name"), "description": strip_html(r.get("job_description","")), "location": f"{r.get('job_city','')}, {r.get('job_country','')}", "remote": r.get("job_is_remote", False), "application_link": r.get("job_apply_link",""), "source_url": r.get("job_google_link",""), "posted_date": r.get("job_posted_at_datetime_utc",""), "match_score": 0})
        logging.info(f"JSearch: {len(jobs)} jobs for '{query}'")
        return jobs
    except Exception as e:
        logging.error(f"JSearch error: {e}")
        return []

def search_upwork_rss(query, num=10):
    try:
        resp = requests.get(f"https://www.upwork.com/ab/feed/jobs/rss?q={query}", headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.content, "xml")
        jobs = []
        for item in soup.find_all("item")[:num]:
            jobs.append({"title": item.find("title").text if item.find("title") else "", "company": "Upwork Client", "description": strip_html(item.find("description").text if item.find("description") else ""), "location": "", "remote": True, "application_link": item.find("link").text if item.find("link") else "", "source_url": item.find("link").text if item.find("link") else "", "posted_date": "", "match_score": 0})
        logging.info(f"Upwork RSS: {len(jobs)} jobs for '{query}'")
        return jobs
    except Exception as e:
        logging.error(f"Upwork RSS error: {e}")
        return []

def search_reddit_forhire(query, num=10):
    try:
        resp = requests.get(f"https://www.reddit.com/r/forhire/search.json?q={query}&restrict_sr=on&sort=new", headers={"User-Agent": "Mozilla/5.0"})
        data = resp.json()
        jobs = []
        for post in data.get("data", {}).get("children", [])[:num]:
            jobs.append({"title": post["data"]["title"], "company": "Reddit /r/forhire", "description": strip_html(post["data"].get("selftext","")), "location": "", "remote": "remote" in post["data"]["title"].lower(), "application_link": "https://reddit.com" + post["data"]["permalink"], "source_url": "https://reddit.com" + post["data"]["permalink"], "posted_date": "", "match_score": 0})
        logging.info(f"Reddit: {len(jobs)} jobs for '{query}'")
        return jobs
    except Exception as e:
        logging.error(f"Reddit error: {e}")
        return []

def search_hackernews(query, num=10):
    try:
        resp = requests.get("https://hn.algolia.com/api/v1/search", params={"query": "Ask HN: Who is hiring?", "tags": "story", "hitsPerPage": 50})
        data = resp.json()
        jobs = []
        for hit in data.get("hits", []):
            if "who is hiring" not in hit.get("title","").lower(): continue
            if query.lower() in hit.get("story_text","").lower():
                jobs.append({"title": hit["title"], "company": "Hacker News", "description": strip_html(hit.get("story_text","")[:500]), "location": "", "remote": "remote" in hit.get("story_text","").lower(), "application_link": hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}", "source_url": f"https://news.ycombinator.com/item?id={hit['objectID']}", "posted_date": hit.get("created_at",""), "match_score": 0})
        logging.info(f"HackerNews: {len(jobs)} jobs for '{query}'")
        return jobs[:num]
    except Exception as e:
        logging.error(f"HackerNews error: {e}")
        return []

def search_careerjet(query, location=None, num=10):
    """Scrape CareerJet RSS (free, no key)."""
    try:
        url = f"https://www.careerjet.com/jobs?q={query}&l={location or ''}&format=rss"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.content, "xml")
        jobs = []
        for item in soup.find_all("item")[:num]:
            jobs.append({"title": item.find("title").text if item.find("title") else "", "company": item.find("company").text if item.find("company") else "", "description": strip_html(item.find("description").text if item.find("description") else ""), "location": item.find("location").text if item.find("location") else "", "remote": False, "application_link": item.find("link").text if item.find("link") else "", "source_url": item.find("link").text if item.find("link") else "", "posted_date": "", "match_score": 0})
        logging.info(f"CareerJet: {len(jobs)} jobs for '{query}'")
        return jobs
    except Exception as e:
        logging.error(f"CareerJet error: {e}")
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

# ---------- ORCHESTRATOR ----------
def agentic_job_search(title, location, num_per_source=10):
    all_jobs = []
    specific_titles = [title]
    # If generic, generate specific titles
    if title.lower() in ("jobs", "job", ""):
        specific_titles = generate_job_titles(location, count=8)
        if not specific_titles:
            specific_titles = [title]
    # For each title, hit every source
    for t in specific_titles:
        query = f"{t} {location}"
        all_jobs.extend(search_serpapi(query, location, num=num_per_source))
        country_code = get_adzuna_country(location)
        if country_code:
            all_jobs.extend(search_adzuna_country(t, location, country_code, num=num_per_source))
        else:
            all_jobs.extend(search_adzuna_country(t, location, "us", num=num_per_source))
        all_jobs.extend(search_remotive(t, num=num_per_source))
        all_jobs.extend(search_remoteok(t, num=num_per_source))
        all_jobs.extend(search_findwork(query, num=num_per_source))
        all_jobs.extend(search_jsearch(query, location, num=num_per_source))
        all_jobs.extend(search_upwork_rss(query, num=num_per_source))
        all_jobs.extend(search_reddit_forhire(query, num=num_per_source))
        all_jobs.extend(search_hackernews(query, num=num_per_source))
        all_jobs.extend(search_careerjet(query, location, num=num_per_source))
        time.sleep(0.3)
    unique = normalize_and_deduplicate(all_jobs)
    exact = [j for j in unique if location_match(j, location)]
    others = [j for j in unique if not location_match(j, location)]
    combined = exact + others[:max(0, 100 - len(exact))]
    logging.info(f"Massive search: {len(exact)} exact matches, {len(combined)} total.")
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
                jobs, exact_count = agentic_job_search(title, location, num_per_source=10)
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
