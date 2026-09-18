"""Probe every configured job source without printing credentials.

Run inside the worker image:
    python /app/source_probe.py
"""

import logging
import os
from typing import Any

import requests


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - source-probe - %(message)s")
log = logging.getLogger("source-probe")
TIMEOUT = (5, 20)


def probe(name: str, url: str, **kwargs: Any) -> None:
    try:
        response = requests.get(url, timeout=TIMEOUT, **kwargs)
        healthy = 200 <= response.status_code < 400 and len(response.content) > 0
        log.info("%s status=%s bytes=%s result=%s", name, response.status_code, len(response.content), "OK" if healthy else "ERROR")
    except requests.RequestException as exc:
        log.info("%s result=ERROR reason=%s", name, exc)


def configured(name: str, *keys: str) -> bool:
    missing = [key for key in keys if not os.getenv(key)]
    if missing:
        log.info("%s result=SKIPPED missing=%s", name, ",".join(missing))
        return False
    return True


def main() -> None:
    if configured("SerpAPI", "SERPAPI_KEY"):
        probe("SerpAPI", "https://serpapi.com/search.json", params={"engine": "google_jobs", "q": "software", "api_key": os.environ["SERPAPI_KEY"]})
    if configured("Adzuna-us", "ADZUNA_APP_ID", "ADZUNA_APP_KEY"):
        probe("Adzuna-us", "https://api.adzuna.com/v1/api/jobs/us/search/1", params={"app_id": os.environ["ADZUNA_APP_ID"], "app_key": os.environ["ADZUNA_APP_KEY"], "what": "software", "results_per_page": 1})
    probe("Remotive", "https://remotive.com/api/remote-jobs?search=software")
    probe("RemoteOK", "https://remoteok.com/api?search=software", headers={"User-Agent": "JobSeekerAI/1.0"})
    if configured("FindWork", "FINDWORK_KEY"):
        probe("FindWork", "https://findwork.dev/api/jobs/?search=software", headers={"Authorization": f"Token {os.environ['FINDWORK_KEY']}"})
    if configured("JSearch", "RAPIDAPI_KEY"):
        probe("JSearch", "https://jsearch.p.rapidapi.com/search?query=software", headers={"X-RapidAPI-Key": os.environ["RAPIDAPI_KEY"], "X-RapidAPI-Host": "jsearch.p.rapidapi.com"})
    probe("UpworkRSS", "https://www.upwork.com/ab/feed/jobs/rss?q=software", headers={"User-Agent": "JobSeekerAI/1.0"})
    probe("RedditForHire", "https://www.reddit.com/r/forhire/new.json?limit=5", headers={"User-Agent": "JobSeekerAI/1.0"})
    probe("HackerNews", "https://hn.algolia.com/api/v1/search_by_date?query=who%20is%20hiring%20software&tags=story&hitsPerPage=5")
    probe("CareerJet", "https://www.careerjet.com/search/rss?s=software&c=us")
    probe("SearXNG", "https://search.sapti.me/search?q=software%20jobs&format=json&categories=it")
    probe("MetaGer", "https://metager.org/search?q=software%20jobs&format=json")
    probe("Mojeek", "https://www.mojeek.com/search?q=software%20jobs")
    probe("Stract", "https://stract.com/search?q=software%20jobs&format=json")
    probe("Arbeitnow", "https://www.arbeitnow.com/api/job-board-api")
    probe("Jobicy", "https://jobicy.com/api/v2/remote-jobs?count=5")
    probe("Himalayas", "https://himalayas.app/jobs/api")
    probe("WorkingNomads", "https://www.workingnomads.com/api/exposed_jobs/")
    probe("TheMuse", "https://www.themuse.com/api/public/jobs?page=0")
    probe("Jobspresso", "https://jobspresso.co/remote-work/feed/")
    probe("WeWorkRemotely", "https://weworkremotely.com/remote-jobs.rss")
    probe("Remote.co", "https://remote.co/remote-jobs/feed/")
    if configured("Mistral", "MISTRAL_API_KEY"):
        probe("Mistral", "https://api.mistral.ai/v1/models", headers={"Authorization": f"Bearer {os.environ['MISTRAL_API_KEY']}"})


if __name__ == "__main__":
    main()