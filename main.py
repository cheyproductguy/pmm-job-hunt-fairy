import json
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from openai import OpenAI
import requests
import yaml


def load_config():
  with open("config.yaml", "r", encoding="utf-8") as f:
    return yaml.safe_load(f)


def load_cv():
  if os.path.exists("cv.txt"):
    try:
      with open("cv.txt", "r", encoding="utf-8") as f:
        return f.read()
    except Exception:
      pass
  # Fallback default CV context if cv.txt is missing
  return (
      "Bilingual Product Marketing Manager (Native English, Professional "
      "French) with 7+ years of experience across tech, insurance, and "
      "energy. Expert in GTM, positioning, and sales enablement."
  )


def load_history():
  if os.path.exists(".job_history.json"):
    try:
      with open(".job_history.json", "r", encoding="utf-8") as f:
        return json.load(f)
    except Exception:
      return []
  return []


def save_history(history):
  with open(".job_history.json", "w", encoding="utf-8") as f:
    json.dump(history, f, indent=2)


def fetch_adzuna_jobs(config):
  all_jobs = []
  app_id = os.environ.get("ADZUNA_APP_ID")
  app_key = os.environ.get("ADZUNA_APP_KEY")

  if not app_id or not app_key:
    print("Error: Adzuna API credentials missing.")
    return []

  locations = config.get("locations", {})
  titles = config.get("target_titles", ["Product Marketing Manager"])
  results_per_query = config.get("search", {}).get("results_per_query", 40)
  max_pages = config.get("search", {}).get("pages", 2)

  for country_key, country_data in locations.items():
    if country_key == "include_remote":
      continue
    country_code = country_data.get("country_code", "fr")

    for title in titles:
      for page in range(1, max_pages + 1):
        url = f"https://api.adzuna.com/v1/api/jobs/{country_code}/search/{page}"
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "what": title,
            "results_per_page": results_per_query,
            "content-type": "application/json",
        }
        try:
          response = requests.get(url, params=params, timeout=10)
          if response.status_code == 200:
            data = response.json()
            results = data.get("results", [])
            for job in results:
              job["search_country"] = country_key
              all_jobs.append(job)
        except Exception as e:
          print(f"Error fetching {title} in {country_code}: {e}")

  return all_jobs


def score_and_filter_jobs(jobs, config):
  min_score = config.get("search", {}).get("minimum_fit_score", 3.0)
  negative_keywords = [
      kw.lower() for kw in config.get("negative_keywords", [])
  ]
  industries = [ind.lower() for ind in config.get("target_industries", [])]
  competencies = [
      comp.lower() for comp in config.get("core_competencies", [])
  ]
  b2c_keywords = [
      kw.lower() for kw in config.get("business_model_focus", [])
  ]

  scored_listings = []
  seen_ids = set()

  for job in jobs:
    job_id = str(job.get("id"))
    if not job_id or job_id in seen_ids:
      continue
    seen_ids.add(job_id)

    title = job.get("title", "").lower()
    description = job.get("description", "").lower()
    location_name = job.get("location", {}).get("display_name", "").lower()

    if any(neg in title or neg in description for neg in negative_keywords):
      continue

    matched_competencies = [
        comp for comp in competencies if comp in description
    ]
    if not matched_competencies:
      continue

    score = 4.0
    score += min(len(matched_competencies) * 0.5, 1.5)

    if any(ind in description for ind in industries):
      score += 1.0

    if any(kw in description or kw in title for kw in b2c_keywords):
      score += 1.5

    local_hubs = [
        "chamonix",
        "annecy",
        "geneva",
        "genève",
        "haute-savoie",
        "annemasse",
    ]
    if any(loc in location_name for loc in local_hubs):
      score += 1.0
    else:
      score += 0.5  # Nationwide France or Europe remote

    if score >= min_score:
      job["score"] = round(score, 1)
      job["url"] = job.get("redirect_url", "#")
      job["company"] = job.get("company", {}).get("display_name", "Unknown")
      job["location_str"] = job.get("location", {}).get(
          "display_name", "Unknown"
      )
      scored_listings.append(job)

  scored_listings.sort(key=lambda x: x["score"], reverse=True)
  return scored_listings


def get_ai_rationale(job, cv_text):
  api_key = os.environ.get("OPENAI_API_KEY")
  if not api_key:
    return "AI rationale unavailable (Missing OPENAI_API_KEY secret)."

  client = OpenAI(api_key=api_key)
  prompt = f"""You are an expert career coach. Based on the candidate's extended CV profile and this job description, write a punchy, 2-sentence rationale explaining why this is a strong match for a Product Marketing Manager / GTM role, and clearly highlight any potential gaps (such as location, industry, or specific requirements).

Candidate Background:
{cv_text}

Job Title: {job.get('title')}
Company: {job.get('company', 'Unknown')}
Location: {job.get('location_str', 'Unknown')}
Job Description: {job.get('description', '')[:1200]}
"""
  try:
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=150,
        temperature=0.3,
    )
    return response.choices[0].message.content.strip()
  except Exception as e:
    return f"AI rationale generation error: {e}"


def send_html_email(scored_jobs):
  sender = os.environ.get("GMAIL_ADDRESS")
  password = os.environ.get("GMAIL_APP_PASSWORD")
  recipient = os.environ.get("JOB_ALERT_EMAIL")

  if not sender or not password or not recipient:
    print("Email credentials missing; skipping email dispatch.")
    return

  msg = MIMEMultipart("alternative")
  msg["Subject"] = (
      f"🚀 Daily PMM Radar: {len(scored_jobs)} Curated Matches Found"
  )
  msg["From"] = sender
  msg["To"] = recipient

  cards_html = ""
  for job in scored_jobs:
    cards_html += f"""
        <div style="border: 1px solid #e1e4e8; border-radius: 8px; padding: 20px; margin-bottom: 20px; background-color: #ffffff; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;">
            <h2 style="margin: 0 0 10px 0; color: #0366d6; font-size: 18px;">
                <a href="{job.get('url', '#')}" target="_blank" style="text-decoration: none; color: #0366d6;">{job.get('title')}</a>
            </h2>
            <p style="margin: 0 0 10px 0; color: #586069; font-size: 14px;"><strong>Company:</strong> {job.get('company')} | <strong>Location:</strong> {job.get('location_str')}</p>
            <p style="margin: 0 0 15px 0;"><span style="background-color: #28a745; color: white; padding: 3px 8px; border-radius: 4px; font-size: 12px; font-weight: bold;">Fit Score: {job.get('score')}</span></p>
            
            <blockquote style="margin: 0 0 15px 0; padding: 12px 15px; border-left: 4px solid #0366d6; background-color: #f6f8fa; color: #24292e; font-size: 14px; border-radius: 0 4px 4px 0; line-height: 1.4;">
                <strong>Coach Rationale:</strong> {job.get('rationale', 'No rationale generated.')}
            </blockquote>

            <p style="margin: 0; color: #586069; font-size: 13px; line-height: 1.4;">{job.get('description', '')[:200]}...</p>
        </div>
        """

  html_content = f"""
    <html>
        <body style="background-color: #f1f8fc; padding: 20px;">
            <div style="max-width: 600px; margin: auto;">
                <h1 style="color: #24292e; font-size: 22px; text-align: center;">🎯 Daily PMM Job Matches</h1>
                <p style="color: #586069; text-align: center; font-size: 14px;">Curated matches tailored for your positioning, GTM, and B2C/B2B2C profile.</p>
                {cards_html}
                <p style="text-align: center; color: #6a737d; font-size: 12px; margin-top: 30px;">Generated automatically by your AI-powered PMM Job Hunter.</p>
            </div>
        </body>
    </html>
    """

  msg.attach(MIMEText(html_content, "html"))

  try:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
      server.login(sender, password)
      server.sendmail(sender, recipient, msg.as_string())
    print("HTML email notification sent successfully!")
  except Exception as e:
    print(f"Failed to send email: {e}")


def main():
  config = load_config()
  print("Fetching raw jobs from Adzuna...")
  raw_jobs = fetch_adzuna_jobs(config)
  print(f"Fetched {len(raw_jobs)} total listings.")

  scored = score_and_filter_jobs(raw_jobs, config)
  print(f"{len(scored)} listings passed filter criteria.")

  history = load_history()
  history_set = set(history)

  fresh_jobs = [j for j in scored if str(j.get("id")) not in history_set]
  print(f"{len(fresh_jobs)} fresh (un-emailed) listings found.")

  if fresh_jobs:
    top_jobs = fresh_jobs[:10]
    cv_text = load_cv()

    print("Generating AI rationales for top matches...")
    for job in top_jobs:
      job["rationale"] = get_ai_rationale(job, cv_text)

    send_html_email(top_jobs)

    for j in top_jobs:
      history.append(str(j.get("id")))
    save_history(history)
  else:
    print("No fresh matches to email today.")


if __name__ == "__main__":
  main()
