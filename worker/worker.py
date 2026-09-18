"""Resilient PocketBase worker for chat replies and job aggregation.

All external calls are bounded, logged, and isolated from the worker loops.
The supervisor owns process-level restarts; this module owns thread-level
recovery and source-level failure isolation.
"""

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from urllib.parse import quote, urlencode

import requests
from bs4 import BeautifulSoup
from openai import OpenAI
from serpapi import GoogleSearch


POCKETBASE_URL = os.getenv("POCKETBASE_URL", "http://localhost:8090").rstrip("/")
POCKETBASE_ADMIN_TOKEN = os.getenv("PB_ADMIN_TOKEN") or os.getenv("POCKETBASE_ADMIN_TOKEN")
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
SERPAPI_KEY = os.getenv("SERPAPI_KEY")
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY")
FINDWORK_KEY = os.getenv("FINDWORK_KEY")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")
JSEARCH_HOST = "jsearch.p.rapidapi.com"
HTTP_TIMEOUT = (5, 20)
CHAT_SYSTEM_PROMPT = (
    "You are JobSeeker AI Coach, a friendly and knowledgeable career advisor. "
    "Always respond directly to the user's latest message. "
    "Be natural, warm, and ask follow-up questions when appropriate. "
    "Remember previous conversation history. "
    "Never output generic templates, welcome messages, structured plans, or numbered lists "
    "unless the user explicitly asks for a plan."
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(threadName)s - %(message)s",
)
log = logging.getLogger("jobseeker-worker")
ai = OpenAI(api_key=MISTRAL_API_KEY, base_url="https://api.mistral.ai/v1") if MISTRAL_API_KEY else None
claim_lock = threading.Lock()
claimed_messages: dict[str, float] = {}
source_health: dict[str, dict[str, Any]] = {}


def _request(method: str, url: str, **kwargs: Any) -> requests.Response | None:
    """Make a bounded request with small backoff; never raises into a worker loop."""
    kwargs.setdefault("timeout", HTTP_TIMEOUT)
    for attempt in range(3):
        try:
            response = requests.request(method, url, **kwargs)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    time.sleep(0.5 * (2**attempt))
                    continue
            return response
        except requests.RequestException as exc:
            if attempt == 2:
                log.warning("HTTP %s %s failed: %s", method, url, exc)
                return None
            time.sleep(0.5 * (2**attempt))
    return None


def pb(method: str, path: str, json_data: dict[str, Any] | None = None) -> requests.Response | None:
    """Call PocketBase without allowing a network failure to kill a worker."""
    if not POCKETBASE_ADMIN_TOKEN:
        log.error("PB_ADMIN_TOKEN is not configured")
        return None
    url = f"{POCKETBASE_URL}/api/{path.lstrip('/')}"
    return _request(
        method,
        url,
        headers={"Authorization": f"Bearer {POCKETBASE_ADMIN_TOKEN}"},
        json=json_data,
    )


def _json(response: requests.Response | None) -> dict[str, Any]:
    if response is None or response.status_code >= 400:
        return {}
    try:
        value = response.json()
        return value if isinstance(value, dict) else {}
    except (ValueError, requests.RequestException):
        return {}


def _ai_chat(messages: list[dict[str, str]], temperature: float, max_tokens: int) -> str:
    if ai is None:
        raise RuntimeError("MISTRAL_API_KEY is not configured")
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = ai.chat.completions.create(
                model="mistral-small-latest",
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            answer = response.choices[0].message.content if response.choices else ""
            if not answer:
                raise RuntimeError("Mistral returned an empty response")
            return answer.strip()
        except Exception as exc:  # SDK errors vary by provider/version.
            last_error = exc
            if attempt < 2:
                time.sleep(0.75 * (2**attempt))
    raise RuntimeError(f"Mistral request failed after retries: {last_error}")


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", BeautifulSoup(text, "html.parser").get_text(" ")).strip()


def parse_natural_query(raw_query: str) -> dict[str, Any]:
    prompt = f"""Extract job search parameters from this query. Return only valid JSON with:
title, location, remote, company, additional_filters. Use null when absent.
Query: {raw_query}"""
    try:
        content = _ai_chat([{"role": "user", "content": prompt}], 0.1, 200)
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
        value = json.loads(content)
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        log.warning("Natural query parsing failed; using raw query: %s", exc)
        return {"title": raw_query, "location": None, "remote": False, "company": None}


def generate_job_titles(raw_query: str, limit: int = 5) -> list[str]:
    """Expand a broad request into a few useful search titles without failing a search."""
    prompt = (
        f"Return only a JSON array of up to {limit} concise job-title searches "
        f"for this request: {raw_query}"
    )
    try:
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", _ai_chat([{"role": "user", "content": prompt}], 0.2, 160))
        titles = json.loads(content)
        if isinstance(titles, list):
            cleaned = [str(title).strip() for title in titles if str(title).strip()]
            if cleaned:
                return cleaned[:limit]
    except Exception as exc:
        log.warning("Job-title generation failed; using raw query: %s", exc)
    return [raw_query.strip()]


def _job(
    title: Any,
    company: Any = "",
    description: Any = "",
    location: Any = "",
    remote: Any = False,
    link: Any = "",
    posted: Any = "",
    source_url: Any = "",
) -> dict[str, Any]:
    url = str(link or source_url or "")
    return {
        "title": str(title or "").strip(),
        "company": str(company or "").strip(),
        "description": strip_html(str(description or "")),
        "location": str(location or "").strip(),
        "remote": bool(remote),
        "application_link": url,
        "source_url": str(source_url or url),
        "posted_date": str(posted or ""),
        "match_score": 0,
    }


def search_serpapi(query: str, location: str = "United States", num: int = 10) -> list[dict[str, Any]]:
    if not SERPAPI_KEY:
        return []
    try:
        results = GoogleSearch({
            "engine": "google_jobs",
            "q": query,
            "location": location,
            "hl": "en",
            "api_key": SERPAPI_KEY,
            "num": num,
        }).get_dict()
        jobs = [
            _job(
                item.get("title"),
                item.get("company_name"),
                item.get("description"),
                item.get("location"),
                "remote" in str(item.get("description", "")).lower(),
                item.get("apply_link") or item.get("share_link"),
                item.get("detected_extensions", {}).get("posted_at"),
                item.get("share_link"),
            )
            for item in results.get("jobs_results", [])
        ]
        return jobs[:num]
    except Exception as exc:
        log.warning("SerpAPI failed: %s", exc)
        return []


def search_adzuna_country(query: str, country: str = "us", location: str = "", num: int = 10) -> list[dict[str, Any]]:
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        return []
    try:
        params = {
            "app_id": ADZUNA_APP_ID,
            "app_key": ADZUNA_APP_KEY,
            "what": query,
            "where": location,
            "max_days_old": 30,
            "results_per_page": min(num, 50),
        }
        response = _request("GET", f"https://api.adzuna.com/v1/api/jobs/{country}/search/1", params=params)
        data = _json(response)
        return [
            _job(
                item.get("title"),
                item.get("company", {}).get("display_name"),
                item.get("description"),
                item.get("location", {}).get("display_name"),
                False,
                item.get("redirect_url"),
                item.get("created"),
                item.get("redirect_url"),
            )
            for item in data.get("results", [])[:num]
        ]
    except Exception as exc:
        log.warning("Adzuna %s failed: %s", country, exc)
        return []


def search_adzuna(query: str, location: str = "United States", num: int = 10) -> list[dict[str, Any]]:
    return search_adzuna_country(query, "us", location, num)


def search_remotive(query: str, num: int = 10) -> list[dict[str, Any]]:
    try:
        data = _json(_request("GET", "https://remotive.com/api/remote-jobs", params={"search": query}))
        return [
            _job(j.get("title"), j.get("company_name"), j.get("description"), j.get("candidate_required_location"), True, j.get("url"), j.get("publication_date"), j.get("url"))
            for j in data.get("jobs", [])[:num]
        ]
    except Exception as exc:
        log.warning("Remotive failed: %s", exc)
        return []


def search_remoteok(query: str, num: int = 10) -> list[dict[str, Any]]:
    try:
        response = _request("GET", "https://remoteok.com/api", params={"search": query}, headers={"User-Agent": "JobSeekerAI/1.0"})
        data = response.json() if response is not None and response.status_code < 400 else []
        return [
            _job(j.get("position"), j.get("company"), j.get("description"), j.get("location"), True, j.get("url"), j.get("epoch"), j.get("url"))
            for j in data[1:] if isinstance(j, dict)
        ][:num]
    except Exception as exc:
        log.warning("RemoteOK failed: %s", exc)
        return []


def search_findwork(query: str, num: int = 10) -> list[dict[str, Any]]:
    if not FINDWORK_KEY:
        return []
    try:
        data = _json(_request("GET", "https://findwork.dev/api/jobs/", params={"search": query}, headers={"Authorization": f"Token {FINDWORK_KEY}"}))
        return [
            _job(j.get("role"), j.get("company_name"), j.get("text"), j.get("location"), j.get("remote"), j.get("url"), j.get("date_posted"), j.get("url"))
            for j in data.get("results", [])[:num]
        ]
    except Exception as exc:
        log.warning("FindWork failed: %s", exc)
        return []


def search_jsearch(query: str, location: str | None = None, num: int = 10) -> list[dict[str, Any]]:
    if not RAPIDAPI_KEY:
        return []
    try:
        params: dict[str, Any] = {"query": query, "page": "1", "num_pages": "1", "date_posted": "all"}
        if location:
            params["location"] = location
        data = _json(_request("GET", "https://jsearch.p.rapidapi.com/search", params=params, headers={"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": JSEARCH_HOST}))
        return [
            _job(r.get("job_title"), r.get("employer_name"), r.get("job_description"), f"{r.get('job_city', '')}, {r.get('job_country', '')}", r.get("job_is_remote"), r.get("job_apply_link"), r.get("job_posted_at_datetime_utc"), r.get("job_google_link"))
            for r in data.get("data", [])[:num]
        ]
    except Exception as exc:
        log.warning("JSearch failed: %s", exc)
        return []


def _rss_jobs(url: str, query: str, num: int = 10) -> list[dict[str, Any]]:
    response = _request("GET", url, headers={"User-Agent": "JobSeekerAI/1.0"})
    if response is None or response.status_code >= 400:
        return []
    soup = BeautifulSoup(response.text, "xml")
    jobs = []
    for item in soup.find_all(["item", "entry"]):
        text = " ".join(item.stripped_strings)
        if query and query.lower() not in text.lower():
            continue
        link_node = item.find("link")
        link = link_node.get("href", "") if link_node and link_node.has_attr("href") else (link_node.get_text(strip=True) if link_node else "")
        jobs.append(_job(item.find_text("title"), "", item.find_text("description") or item.find_text("summary"), "", True, link, item.find_text("pubDate") or item.find_text("published"), link))
    return jobs[:num]


def search_upwork_rss(query: str, num: int = 10) -> list[dict[str, Any]]:
    return _rss_jobs(f"https://www.upwork.com/ab/feed/jobs/rss?{urlencode({'q': query})}", query, num)


def search_reddit_forhire(query: str, num: int = 10) -> list[dict[str, Any]]:
    try:
        response = _request("GET", "https://www.reddit.com/r/forhire/new.json", params={"limit": 50}, headers={"User-Agent": "JobSeekerAI/1.0"})
        data = response.json() if response is not None and response.status_code < 400 else {}
        jobs = []
        for child in data.get("data", {}).get("children", []):
            post = child.get("data", {})
            title = post.get("title", "")
            if query.lower() not in title.lower() and query.lower() not in post.get("selftext", "").lower():
                continue
            jobs.append(_job(title, "Reddit /r/forhire", post.get("selftext"), "Remote / Reddit", True, f"https://www.reddit.com{post.get('permalink', '')}", post.get("created_utc"), f"https://www.reddit.com{post.get('permalink', '')}"))
        return jobs[:num]
    except Exception as exc:
        log.warning("Reddit forhire failed: %s", exc)
        return []


def search_hackernews(query: str, num: int = 10) -> list[dict[str, Any]]:
    try:
        data = _json(_request("GET", "https://hn.algolia.com/api/v1/search_by_date", params={"query": f"who is hiring {query}", "tags": "story", "hitsPerPage": num}))
        return [_job(h.get("title"), "Hacker News", h.get("story_text"), "Remote / Hacker News", True, h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}", h.get("created_at"), h.get("url", "")) for h in data.get("hits", [])]
    except Exception as exc:
        log.warning("Hacker News failed: %s", exc)
        return []


def search_careerjet(query: str, country: str = "us", num: int = 10) -> list[dict[str, Any]]:
    return _rss_jobs(f"https://www.careerjet.com/search/rss?s={quote(query)}&l=&c={country}", query, num)


def search_searxng(query: str, instance: str = "https://search.sapti.me", num: int = 10) -> list[dict[str, Any]]:
    try:
        data = _json(_request("GET", f"{instance.rstrip('/')}/search", params={"q": f"{query} jobs", "format": "json", "categories": "it"}))
        return [_job(r.get("title"), "SearXNG", r.get("content"), "", True, r.get("url"), "", r.get("url")) for r in data.get("results", [])[:num]]
    except Exception as exc:
        log.warning("SearXNG %s failed: %s", instance, exc)
        return []


def search_metager(query: str, num: int = 10) -> list[dict[str, Any]]:
    return search_searxng(query, "https://metager.org", num)


def search_mojeek(query: str, num: int = 10) -> list[dict[str, Any]]:
    try:
        response = _request("GET", "https://www.mojeek.com/search", params={"q": f"{query} jobs"})
        soup = BeautifulSoup(response.text if response is not None else "", "html.parser")
        return [_job(node.get_text(" ", strip=True), "Mojeek", "", "", True, node.get("href"), "", node.get("href")) for node in soup.select("a.title")[:num]]
    except Exception as exc:
        log.warning("Mojeek failed: %s", exc)
        return []


def search_stract(query: str, num: int = 10) -> list[dict[str, Any]]:
    return search_searxng(query, "https://stract.com", num)


# Eight keyless additions: Arbeitnow, Jobicy, Himalayas, Working Nomads,
# The Muse, Jobspresso, We Work Remotely, and Remote.co.
def search_arbeitnow(query: str, num: int = 10) -> list[dict[str, Any]]:
    try:
        data = _json(_request("GET", "https://www.arbeitnow.com/api/job-board-api"))
        return [_job(j.get("title"), j.get("company_name"), j.get("description"), j.get("location"), j.get("remote"), j.get("url"), j.get("created_at"), j.get("url")) for j in data.get("data", []) if query.lower() in json.dumps(j).lower()][:num]
    except Exception as exc:
        log.warning("Arbeitnow failed: %s", exc)
        return []


def search_jobicy(query: str, num: int = 10) -> list[dict[str, Any]]:
    try:
        data = _json(_request("GET", "https://jobicy.com/api/v2/remote-jobs", params={"count": 50}))
        return [_job(j.get("jobTitle"), j.get("companyName"), j.get("jobDescription"), j.get("jobGeo"), True, j.get("url"), j.get("pubDate"), j.get("url")) for j in data.get("jobs", []) if query.lower() in json.dumps(j).lower()][:num]
    except Exception as exc:
        log.warning("Jobicy failed: %s", exc)
        return []


def search_himalayas(query: str, num: int = 10) -> list[dict[str, Any]]:
    try:
        data = _json(_request("GET", "https://himalayas.app/jobs/api"))
        return [_job(j.get("title"), j.get("companyName"), j.get("description"), j.get("location"), True, j.get("applicationLink") or j.get("url"), j.get("pubDate"), j.get("url")) for j in data.get("jobs", []) if query.lower() in json.dumps(j).lower()][:num]
    except Exception as exc:
        log.warning("Himalayas failed: %s", exc)
        return []


def search_working_nomads(query: str, num: int = 10) -> list[dict[str, Any]]:
    try:
        response = _request("GET", "https://www.workingnomads.com/api/exposed_jobs/")
        data = response.json() if response is not None and response.status_code < 400 else []
        return [_job(j.get("title"), j.get("company"), j.get("description"), j.get("location"), True, j.get("url"), j.get("date"), j.get("url")) for j in data if query.lower() in json.dumps(j).lower()][:num]
    except Exception as exc:
        log.warning("Working Nomads failed: %s", exc)
        return []


def search_themuse(query: str, num: int = 10) -> list[dict[str, Any]]:
    try:
        data = _json(_request("GET", "https://www.themuse.com/api/public/jobs", params={"page": 0, "descending": "true"}))
        return [_job(j.get("name"), j.get("company", {}).get("name"), j.get("contents"), j.get("locations", [{}])[0].get("name"), False, j.get("refs", {}).get("landing_page"), "", j.get("refs", {}).get("landing_page")) for j in data.get("results", []) if query.lower() in json.dumps(j).lower()][:num]
    except Exception as exc:
        log.warning("The Muse failed: %s", exc)
        return []


def search_jobspresso(query: str, num: int = 10) -> list[dict[str, Any]]:
    return _rss_jobs("https://jobspresso.co/remote-work/feed/", query, num)


def search_weworkremotely(query: str, num: int = 10) -> list[dict[str, Any]]:
    return _rss_jobs("https://weworkremotely.com/remote-jobs.rss", query, num)


def search_remoteco(query: str, num: int = 10) -> list[dict[str, Any]]:
    return _rss_jobs("https://remote.co/remote-jobs/feed/", query, num)


def normalize_and_deduplicate(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique = []
    for job in jobs:
        if not job.get("title"):
            continue
        key = "|".join(str(job.get(field, "")).lower().strip() for field in ("title", "company", "source_url"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(job)
    return unique


def location_match(job: dict[str, Any], desired_location: str | None) -> bool:
    if not desired_location:
        return bool(job.get("remote"))
    location = re.sub(r"\s+", " ", re.sub(r"[,\-]+", " ", str(job.get("location", "")).lower())).strip()
    desired = desired_location.lower().strip()
    return bool(job.get("remote")) or desired in location


def _source_functions(title: str, location: str) -> list[tuple[str, Callable[[], list[dict[str, Any]]]]]:
    query = f"{title} {location}".strip()
    sources: list[tuple[str, Callable[[], list[dict[str, Any]]]]] = [
        ("SerpAPI", lambda: search_serpapi(query, location)),
        ("Adzuna-us", lambda: search_adzuna_country(title, "us", location)),
        ("Adzuna-ca", lambda: search_adzuna_country(title, "ca", location)),
        ("Adzuna-gb", lambda: search_adzuna_country(title, "gb", location)),
        ("Adzuna-ng", lambda: search_adzuna_country(title, "ng", location)),
        ("Remotive", lambda: search_remotive(title)),
        ("RemoteOK", lambda: search_remoteok(title)),
        ("FindWork", lambda: search_findwork(query)),
        ("JSearch", lambda: search_jsearch(query, location)),
        ("UpworkRSS", lambda: search_upwork_rss(title)),
        ("RedditForHire", lambda: search_reddit_forhire(title)),
        ("HackerNews", lambda: search_hackernews(title)),
        ("CareerJet", lambda: search_careerjet(title)),
        ("SearXNG", lambda: search_searxng(title)),
        ("MetaGer", lambda: search_metager(title)),
        ("Mojeek", lambda: search_mojeek(title)),
        ("Stract", lambda: search_stract(title)),
        ("Arbeitnow", lambda: search_arbeitnow(title)),
        ("Jobicy", lambda: search_jobicy(title)),
        ("Himalayas", lambda: search_himalayas(title)),
        ("WorkingNomads", lambda: search_working_nomads(title)),
        ("TheMuse", lambda: search_themuse(title)),
        ("Jobspresso", lambda: search_jobspresso(title)),
        ("WeWorkRemotely", lambda: search_weworkremotely(title)),
        ("Remote.co", lambda: search_remoteco(title)),
    ]
    return sources


def agentic_job_search(title: str, location: str | None, num_per_source: int = 8) -> tuple[list[dict[str, Any]], int]:
    location = location or "United States"
    all_jobs: list[dict[str, Any]] = []
    sources = _source_functions(title, location)
    with ThreadPoolExecutor(max_workers=min(12, len(sources))) as executor:
        active_sources = [
            (name, fn) for name, fn in sources
            if not source_health.get(name, {}).get("disabled")
        ]
        futures = {executor.submit(fn): name for name, fn in active_sources}
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()[:num_per_source]
                all_jobs.extend(result)
                health = source_health.setdefault(name, {"empty_runs": 0, "disabled": False})
                if result:
                    health["empty_runs"] = 0
                else:
                    health["empty_runs"] += 1
                    if health["empty_runs"] >= 3:
                        health["disabled"] = True
                        log.warning("source=%s disabled_after_consecutive_empty_runs=3", name)
                log.info("source=%s jobs=%d query=%s", name, len(result), title)
            except Exception as exc:
                health = source_health.setdefault(name, {"empty_runs": 0, "disabled": False})
                health["empty_runs"] += 1
                if health["empty_runs"] >= 3:
                    health["disabled"] = True
                    log.warning("source=%s disabled_after_consecutive_errors=3", name)
                log.warning("source=%s error=%s", name, exc)
    unique = normalize_and_deduplicate(all_jobs)
    exact = [job for job in unique if location_match(job, location)]
    others = [job for job in unique if job not in exact]
    combined = (exact + others)[:50]
    log.info("agentic_search title=%s exact=%d total=%d sources=%d", title, len(exact), len(combined), len(sources))
    return combined, len(exact)


def insert_or_get_ids(jobs: list[dict[str, Any]]) -> list[str]:
    ids = []
    for job in jobs:
        try:
            filter_value = f"(title='{job.get('title', '')}'&&company='{job.get('company', '')}'&&source_url='{job.get('source_url', '')}')"
            path = f"/collections/job_listings/records?filter={quote(filter_value)}"
            response = pb("GET", path)
            data = _json(response)
            if data.get("totalItems", 0) > 0:
                ids.append(data["items"][0]["id"])
            else:
                created = pb("POST", "/collections/job_listings/records", json_data=job)
                created_data = _json(created)
                if created_data.get("id"):
                    ids.append(created_data["id"])
            time.sleep(0.05)
        except Exception as exc:
            log.warning("Job insert failed: %s", exc)
    return ids


def process_search_requests() -> None:
    log.info("Search request processor thread started")
    while True:
        try:
            data = _json(pb("GET", "/collections/job_search_requests/records?filter=(status='pending')&sort=created&perPage=5"))
            for request in data.get("items", []):
                request_id = request.get("id")
                raw_query = request.get("query", "")
                if not request_id or not raw_query:
                    continue
                pb("PATCH", f"/collections/job_search_requests/records/{request_id}", {"status": "running"})
                params = parse_natural_query(raw_query)
                title = params.get("title") or raw_query
                location = params.get("location") or "United States"
                jobs, exact_count = agentic_job_search(title, location)
                job_ids = insert_or_get_ids(jobs)
                pb("PATCH", f"/collections/job_search_requests/records/{request_id}", {"status": "completed", "results": job_ids, "exact_match_count": exact_count})
                log.info("search_request=%s completed jobs=%d exact=%d", request_id, len(job_ids), exact_count)
        except Exception as exc:
            log.exception("Search request loop recovered from error: %s", exc)
        time.sleep(10)


def _claim_message(message_id: str) -> bool:
    with claim_lock:
        now = time.time()
        for key, claimed_at in list(claimed_messages.items()):
            if now - claimed_at > 900:
                claimed_messages.pop(key, None)
        if message_id in claimed_messages:
            return False
        claimed_messages[message_id] = now
        return True


def _chat_once(message: dict[str, Any]) -> None:
    message_id = message.get("id")
    user_id = message.get("user")
    text = str(message.get("message", "")).strip()
    if not message_id or not user_id or not text or not _claim_message(message_id):
        return
    try:
        history_data = _json(pb("GET", f"/collections/chat_messages/records?filter=(user='{quote(str(user_id))}')&sort=created&perPage=10"))
        history: list[dict[str, str]] = []
        for item in history_data.get("items", []):
            if item.get("id") == message_id:
                continue
            if item.get("message"):
                history.append({"role": "user", "content": str(item["message"])})
            if item.get("response"):
                history.append({"role": "assistant", "content": str(item["response"])})
        history.append({"role": "user", "content": text})

        user_data = _json(pb("GET", f"/collections/users/records/{quote(str(user_id))}"))
        profile = "\n".join(
            f"{label}: {user_data.get(field, '')}"
            for label, field in (("Name", "full_name"), ("Skills", "skills"), ("Desired Job", "desired_job_title"), ("Location", "location"))
            if user_data.get(field)
        )
        messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
        if profile:
            messages.append({"role": "system", "content": f"Known user profile:\n{profile}"})
        messages.extend(history[-10:])
        answer = _ai_chat(messages, temperature=0.8, max_tokens=500)
        patched = pb("PATCH", f"/collections/chat_messages/records/{message_id}", {"response": answer})
        if patched is None or patched.status_code >= 400:
            raise RuntimeError(f"PocketBase did not accept chat response: {patched.status_code if patched else 'no response'}")
        log.info("Replied to chat message=%s", message_id)
    except Exception as exc:
        log.exception("Chat message failed message=%s: %s", message_id, exc)
    finally:
        with claim_lock:
            claimed_messages.pop(str(message_id), None)


def fast_chat_loop(worker_name: str = "chat-1", startup_offset: int = 0) -> None:
    if startup_offset:
        time.sleep(startup_offset)
    log.info("Chat worker started name=%s", worker_name)
    while True:
        try:
            data = _json(pb("GET", "/collections/chat_messages/records?filter=(response='')&sort=created&perPage=10"))
            for message in data.get("items", []):
                _chat_once(message)
        except Exception as exc:
            log.exception("Chat loop recovered name=%s error=%s", worker_name, exc)
        time.sleep(3)


def health_check_loop() -> None:
    while True:
        try:
            data = _json(pb("GET", "/collections/chat_messages/records?filter=(response='')&perPage=1"))
            unanswered = data.get("totalItems", 0)
            reachable = False
            if MISTRAL_API_KEY:
                response = _request("GET", "https://api.mistral.ai/v1/models", headers={"Authorization": f"Bearer {MISTRAL_API_KEY}"})
                reachable = response is not None and response.status_code < 400
            log.info("health unanswered_messages=%s mistral_reachable=%s", unanswered, reachable)
        except Exception as exc:
            log.exception("Health check recovered from error: %s", exc)
        time.sleep(60)


def scraping_loop() -> None:
    log.info("Scraping worker started")
    while True:
        try:
            users = _json(pb("GET", "/collections/users/records?perPage=200")).get("items", [])
            for user in users:
                query = " ".join(filter(None, [str(user.get("desired_job_title", "")).strip(), str(user.get("skills", "")).strip()]))
                if not query:
                    continue
                if user.get("remote_preference") == "remote":
                    query += " remote"
                jobs, _ = agentic_job_search(query, user.get("location") or "United States")
                insert_or_get_ids(jobs)
                time.sleep(2)
            log.info("Scraping cycle complete; sleeping 10 minutes")
        except Exception as exc:
            log.exception("Scraping loop recovered from error: %s", exc)
        time.sleep(600)


def _start_thread(target: Callable[..., None], name: str, *args: Any) -> threading.Thread:
    thread = threading.Thread(target=target, args=args, name=name, daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    if not POCKETBASE_ADMIN_TOKEN:
        raise RuntimeError("PB_ADMIN_TOKEN is required")
    if not MISTRAL_API_KEY:
        raise RuntimeError("MISTRAL_API_KEY is required")
    log.info("Starting resilient JobSeeker worker threads")
    _start_thread(fast_chat_loop, "chat-worker-1", "chat-1", 0)
    _start_thread(fast_chat_loop, "chat-worker-2", "chat-2", 5)
    _start_thread(process_search_requests, "search-worker")
    _start_thread(health_check_loop, "health-worker")
    scraping_loop()