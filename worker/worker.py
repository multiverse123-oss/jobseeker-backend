import os, time, logging, json
import requests
from dotenv import load_dotenv
from fpdf import FPDF, XPos, YPos
from openai import OpenAI
from jobspy import scrape_jobs

load_dotenv()

POCKETBASE_URL = os.getenv("POCKETBASE_URL", "http://localhost:8090")   # inside container, talk to local PocketBase
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

def fetch_users():
    resp = pb_request("GET", "/collections/users/records")
    return resp.json().get("items", []) if resp.status_code == 200 else []

def job_exists(title, company, source_url):
    filter_str = f"(title='{title}'&&company='{company}'&&source_url='{source_url}')"
    resp = pb_request("GET", f"/collections/job_listings/records?filter={filter_str}")
    return resp.json().get("totalItems", 0) > 0 if resp.status_code == 200 else False

def insert_job(job_dict):
    resp = pb_request("POST", "/collections/job_listings/records", json_data=job_dict)
    return resp.json() if resp.status_code == 200 else None

def update_job_analysis(job_id, analysis_dict):
    """Patch the analysis field of an existing job record."""
    return pb_request("PATCH", f"/collections/job_listings/records/{job_id}",
                      json_data={"analysis": analysis_dict})

def search_jobs(query, location="United States", results_wanted=20):
    try:
        jobs_df = scrape_jobs(
            site_name=["indeed","linkedin","glassdoor","google"],
            search_term=query, location=location,
            results_wanted=results_wanted, country_indeed='USA'
        )
        return jobs_df.to_dict(orient="records")
    except Exception as e:
        logging.error(f"JobSpy error: {e}")
        return []

def normalize_job(job):
    desc = str(job.get("description",""))
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
        "analysis": None      # will be filled later
    }

# ------------------- AI generation (CV & Guide) -------------------
def generate_cv_text(profile, job):
    prompt = f"""You are a professional CV writer. Produce a tailored CV in plain text with sections: Contact Info, Professional Summary, Skills, Work Experience, Education. Use only the user's real experience.
User Profile:
- Full Name: {profile.get('full_name','')}
- Phone: {profile.get('phone','')}
- Location: {profile.get('location','')}
- Skills: {profile.get('skills','')}
- Work Experience (JSON): {profile.get('work_experience','')}
- Education (JSON): {profile.get('education','')}
- Portfolio: {profile.get('portfolio_url','')}

Job Title: {job.get('title')}
Company: {job.get('company')}
Description: {job.get('description','')}
Generate the CV now."""
    try:
        resp = ai_client.chat.completions.create(
            model="mistral-large-latest",
            messages=[{"role":"user","content":prompt}],
            temperature=0.7, max_tokens=2000
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"CV generation failed: {e}")
        return None

def generate_guide(company, job_desc):
    prompt = f"""You are a career coach. Provide a strategic interview guide for {company}. Include:
1. Company Pain Points & How the Candidate Solves Them
2. Culture Hints
3. Suggested Questions to Ask
4. Application Process Heads-Up

Job Description: {job_desc}"""
    try:
        resp = ai_client.chat.completions.create(
            model="mistral-small-latest",
            messages=[{"role":"user","content":prompt}],
            temperature=0.7, max_tokens=2000
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"Guide generation failed: {e}")
        return None

# ------------------- Deep Job Analysis -------------------
def analyse_job(job):
    """Use Mistral to extract salary, company health, scam flags, odds, terms, etc.
    Returns a dict to store in the analysis field."""
    prompt = f"""Analyse the following job posting and return a JSON object with the following keys (use null if info not available):
- salary: estimated salary range (string, e.g. "$80k - $120k")
- company_status: whether the company is likely still operating (string: "active", "unknown", "defunct")
- legitimacy: scam probability (string: "legit", "suspicious", "scam")
- terms: any notable terms/conditions (string, e.g. "full-time, contract, remote")
- odds_of_hiring: subjective estimate of getting hired (string: "low", "medium", "high")
- good_shot: would you recommend applying? (boolean true/false)
- additional_notes: any other relevant insights (string)

Job Title: {job.get('title')}
Company: {job.get('company')}
Description: {job.get('description','')}
Location: {job.get('location','')}

Return ONLY a valid JSON, no other text."""
    try:
        resp = ai_client.chat.completions.create(
            model="mistral-large-latest",
            messages=[{"role":"user","content":prompt}],
            temperature=0.3, max_tokens=500
        )
        content = resp.choices[0].message.content.strip()
        # Parse JSON
        analysis = json.loads(content)
        return analysis
    except Exception as e:
        logging.error(f"Job analysis failed: {e}")
        return None

# ------------------- PDF generation (Unicode safe) -------------------
DEJAVU_SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
def text_to_pdf_bytes(text):
    pdf = FPDF()
    pdf.add_page()
    try:
        pdf.add_font("DejaVu", "", DEJAVU_SANS, uni=True)
        pdf.set_font("DejaVu", size=12)
    except:
        pdf.set_font("Helvetica", size=12)
        text = text.replace('\u2018', "'").replace('\u2019', "'").replace('\u201c', '"').replace('\u201d', '"').replace('\u2013', '-').replace('\u2014', '--')
    for line in text.split("\n"):
        pdf.cell(0, 10, text=line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    return pdf.output()

def upload_cv(user_id, job_id, pdf_bytes):
    files = {"user": (None, user_id), "job": (None, job_id), "cv_file": ("cv.pdf", pdf_bytes, "application/pdf")}
    resp = pb_request("POST", "/collections/generated_cvs/records", files=files)
    logging.info("CV uploaded" if resp.status_code == 200 else f"CV upload failed: {resp.status_code} {resp.text}")

def upload_guide(user_id, job_id, pdf_bytes):
    files = {"user": (None, user_id), "job": (None, job_id), "guide_file": ("guide.pdf", pdf_bytes, "application/pdf")}
    resp = pb_request("POST", "/collections/interview_guides/records", files=files)
    logging.info("Guide uploaded" if resp.status_code == 200 else f"Guide upload failed: {resp.status_code} {resp.text}")

# ------------------- Main 24/7 loop -------------------
def main_loop():
    while True:
        logging.info("=== Starting job search cycle ===")
        users = fetch_users()
        for user in users:
            uid = user["id"]
            profile = {
                "full_name": user.get("full_name"),
                "phone": user.get("phone"),
                "location": user.get("location"),
                "remote_preference": user.get("remote_preference"),
                "skills": user.get("skills",""),
                "work_experience": user.get("work_experience"),
                "education": user.get("education"),
                "portfolio_url": user.get("portfolio_url")
            }
            query = profile["skills"]
            if not query: continue
            if profile.get("remote_preference") == "remote": query += " remote"
            logging.info(f"Searching jobs for user {uid}: {query}")
            for job_raw in search_jobs(query, location=profile.get("location","United States"), results_wanted=5):
                job = normalize_job(job_raw)
                if not job["title"]: continue
                if not job_exists(job["title"], job["company"], job["source_url"]):
                    inserted = insert_job(job)
                    if inserted:
                        jid = inserted["id"]
                        logging.info(f"Inserted: {job['title']} at {job['company']}")

                        # Generate CV
                        cv_text = generate_cv_text(profile, job)
                        if cv_text:
                            try:
                                pdf_bytes = text_to_pdf_bytes(cv_text)
                                upload_cv(uid, jid, pdf_bytes)
                            except Exception as e:
                                logging.error(f"CV PDF failed: {e}")

                        # Generate interview guide
                        guide_text = generate_guide(job["company"], job["description"])
                        if guide_text:
                            try:
                                guide_bytes = text_to_pdf_bytes(guide_text)
                                upload_guide(uid, jid, guide_bytes)
                            except Exception as e:
                                logging.error(f"Guide PDF failed: {e}")

                        # Deep analysis of the job
                        logging.info(f"Running deep analysis for job {jid}...")
                        analysis = analyse_job(job)
                        if analysis:
                            update_job_analysis(jid, analysis)
                            logging.info("Job analysis stored")
        logging.info("Cycle complete. Next cycle in 60 seconds.")
        time.sleep(60)

if __name__ == "__main__":
    main_loop()
