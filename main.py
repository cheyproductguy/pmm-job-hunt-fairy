"""Find, score, deduplicate, and email relevant PMM roles from Adzuna."""

from __future__ import annotations

import html
import json
import logging
import os
import smtplib
import ssl
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
import yaml


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
CV_PATH = ROOT / "cv.txt"
HISTORY_PATH = Path(os.getenv("JOB_HISTORY_PATH", ROOT / ".job_history.json"))
ADZUNA_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
LOG = logging.getLogger("pmm_job_hunter")
REQUEST_TIMEOUT = 25

# Each pair represents the same competency in English and French. Search and
# scoring are accent-insensitive, so e.g. "valeur" matches either spelling.
BILINGUAL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "product marketing": ("product marketing", "marketing produit"),
    "positioning": ("positioning", "positionnement"),
    "value proposition": ("value proposition", "proposition de valeur"),
    "go to market": ("go-to-market", "go to market", "mise sur le marché", "lancement marché"),
    "sales enablement": ("sales enablement", "aide à la vente", "activation commerciale", "outils de vente"),
    "user research": ("user research", "recherche utilisateur", "étude utilisateur"),
    "voice of customer": ("voice of customer", "voix du client", "retour client", "insights client"),
    "jobs to be done": ("jobs-to-be-done", "jobs to be done", "besoins utilisateurs"),
    "messaging": ("messaging", "message produit", "plateforme de messages", "discours de marque"),
    "market research": ("market research", "étude de marché", "recherche marché"),
    "launch": ("product launch", "lancement produit", "lancement d'offre"),
    "customer insights": ("customer insights", "insights consommateurs", "analyse client"),
    "segmentation": ("segmentation", "segmentation marché", "segmentation client"),
    "competitive analysis": ("competitive analysis", "analyse concurrentielle", "veille concurrentielle"),
}

TITLE_SEARCH_TERMS = {
    "Product Marketing Manager": ("Product Marketing Manager", "Responsable marketing produit"),
    "PMM": ("PMM", "Product Marketing"),
    "Go-to-Market Manager": ("Go-to-Market Manager", "Responsable go-to-market"),
    "GTM Manager": ("GTM Manager", "Responsable GTM"),
    "Product Marketing Lead": ("Product Marketing Lead", "Lead marketing produit"),
}


def normalize(text: str) -> str:
    """Lowercase text and remove accents for bilingual substring matching."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    if not config.get("target_titles") or not config.get("negative_keywords"):
        raise ValueError("config.yaml must define target_titles and negative_keywords")
    return config


def load_history() -> set[str]:
    try:
        with HISTORY_PATH.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        return set(data.get("sent_ids", []))
    except FileNotFoundError:
        return set()
    except (json.JSONDecodeError, OSError) as exc:
        LOG.warning("Could not read history (%s); starting with empty history", exc)
        return set()


def save_history(sent_ids: set[str]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = HISTORY_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({"sent_ids": sorted(sent_ids)}, indent=2), encoding="utf-8")
    temporary.replace(HISTORY_PATH)


def job_text(job: dict[str, Any]) -> str:
    company = job.get("company", {})
    location = job.get("location", {})
    return " ".join(
        [
            str(job.get("title", "")),
            str(job.get("description", "")),
            str(job.get("category", "")),
            str(company.get("display_name", "") if isinstance(company, dict) else company),
            str(location.get("display_name", "") if isinstance(location, dict) else location),
        ]
    )


def job_identifier(job: dict[str, Any]) -> str:
    return str(job.get("id") or job.get("redirect_url") or job.get("title", "unknown"))


def search_adzuna(config: dict[str, Any]) -> list[dict[str, Any]]:
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        raise RuntimeError("Set ADZUNA_APP_ID and ADZUNA_APP_KEY environment variables")

    titles = config["target_titles"]
    terms: set[str] = set()
    for title in titles:
        terms.update(TITLE_SEARCH_TERMS.get(title, (title,)))

    search_cfg = config.get("search", {})
    per_page = int(search_cfg.get("results_per_query", 30))
    pages = int(search_cfg.get("pages", 2))
    found: dict[str, dict[str, Any]] = {}
    session = requests.Session()
    for place in ("france", "switzerland"):
        country = config["locations"][place]["country_code"]
        for term in sorted(terms):
            for page in range(1, pages + 1):
                params = {
                    "app_id": app_id,
                    "app_key": app_key,
                    "results_per_page": per_page,
                    "what": term,
                    "content-type": "application/json",
                }
                response = session.get(
                    ADZUNA_URL.format(country=country, page=page),
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                )
                response.raise_for_status()
                for job in response.json().get("results", []):
                    job["_country_code"] = country
                    found.setdefault(job_identifier(job), job)
    return list(found.values())


def contains_any(text: str, phrases: list[str] | tuple[str, ...]) -> bool:
    normalized = normalize(text)
    return any(normalize(phrase) in normalized for phrase in phrases)


def is_recent(job: dict[str, Any], max_age_days: int) -> bool:
    created = job.get("created")
    if not created:
        return True
    try:
        parsed = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed >= datetime.now(timezone.utc) - timedelta(days=max_age_days)
    except ValueError:
        return True


def meets_language_rule(text: str, config: dict[str, Any]) -> bool:
    language = config.get("language_rules", {})
    if not language.get("require_english_role_signal", True):
        return True
    return contains_any(text, language.get("english_role_signals", []))


def meets_switzerland_sponsorship(job: dict[str, Any], config: dict[str, Any]) -> bool:
    if job.get("_country_code") != config["locations"]["switzerland"]["country_code"]:
        return True
    location = config["locations"]["switzerland"]
    return not location.get("visa_sponsorship_required", False) or contains_any(
        job_text(job), location.get("visa_sponsorship_signals", [])
    )


def workplace_score(job: dict[str, Any], config: dict[str, Any]) -> float:
    text = normalize(job_text(job))
    scoring = config.get("scoring", {}).get("workplace", {})
    remote_signals = ("remote", "fully remote", "teletravail", "100% a distance", "work from anywhere")
    hybrid_signals = ("hybrid", "hybride", "hybrid working")
    office_signals = ("on-site", "onsite", "in-office", "office based", "sur site", "presentiel")
    france = config["locations"]["france"]
    geneva = config["locations"]["switzerland"]
    if contains_any(text, remote_signals):
        return float(scoring.get("remote", 1.0))
    has_workplace_model = contains_any(text, hybrid_signals + office_signals)
    if job.get("_country_code") == geneva["country_code"]:
        location_match = contains_any(text, geneva.get("preferred", []))
        if location_match and has_workplace_model:
            return float(scoring.get("geneva_hybrid_or_office", 2.5))
    location_match = contains_any(text, france.get("preferred", []) + france.get("commute_hubs", []))
    if location_match and has_workplace_model:
        return float(scoring.get("local_hybrid_or_office", 2.5))
    return float(scoring.get("other", 0.5))


def score_job(job: dict[str, Any], config: dict[str, Any]) -> tuple[float, list[str]]:
    text = normalize(job_text(job))
    matched = [key for key, variants in BILINGUAL_KEYWORDS.items() if any(normalize(v) in text for v in variants)]
    weights = config.get("scoring", {})
    keyword_max = float(weights.get("keyword_overlap_max", 6.0))
    keyword_score = min(keyword_max, len(matched) / max(1, len(BILINGUAL_KEYWORDS)) * 12.0)

    industry_variants = {
        "insurance": ("insurance", "assurance", "assureur", "mutuelle"),
        "energy": ("energy", "énergie", "electricity", "électricité", "hydro"),
        "tech": ("technology", "tech", "software", "saas", "digital", "logiciel", "plateforme"),
        "health": ("health", "santé", "medical", "médical"),
    }
    selected = set(config.get("target_industries", []))
    industry_match = any(
        contains_any(text, industry_variants.get(industry, (industry,))) for industry in selected
    )
    industry_score = float(weights.get("industry_boost", 1.5)) if industry_match else 0.0
    total = min(10.0, keyword_score + industry_score + workplace_score(job, config))
    return round(total, 1), matched


def filter_and_score(jobs: list[dict[str, Any]], config: dict[str, Any], history: set[str]) -> list[dict[str, Any]]:
    negatives = [normalize(item) for item in config.get("negative_keywords", [])]
    max_age = int(config.get("search", {}).get("max_age_days", 30))
    minimum = float(config.get("search", {}).get("minimum_fit_score", 0))
    fresh: list[dict[str, Any]] = []
    for job in jobs:
        text = normalize(job_text(job))
        if any(word in text for word in negatives):
            continue
        if not is_recent(job, max_age) or job_identifier(job) in history:
            continue
        if not meets_language_rule(job_text(job), config) or not meets_switzerland_sponsorship(job, config):
            continue
        score, matched = score_job(job, config)
        if score < minimum:
            continue
        job["fit_score"] = score
        job["matched_keywords"] = matched
        fresh.append(job)
    return sorted(fresh, key=lambda job: (-job["fit_score"], str(job.get("created", ""))))


def safe_url(value: str) -> str:
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else "#"


def format_email(jobs: list[dict[str, Any]]) -> tuple[str, str]:
    paris_now = datetime.now(ZoneInfo("Europe/Paris"))
    date_label = paris_now.strftime("%d %b %Y")
    rows = []
    plain_rows = []
    for job in jobs:
        title = str(job.get("title", "Untitled role"))
        company = str(job.get("company", {}).get("display_name", "Company not listed"))
        location = str(job.get("location", {}).get("display_name", "Location not listed"))
        link = safe_url(str(job.get("redirect_url", "")))
        score = job["fit_score"]
        rows.append(
            "<tr><td style='padding:12px;border-bottom:1px solid #e5e7eb'>"
            f"<a href='{html.escape(link, quote=True)}' style='font-weight:600;color:#155eef'>"
            f"{html.escape(title)}</a><br><span style='color:#667085'>{html.escape(company)}</span></td>"
            f"<td style='padding:12px;border-bottom:1px solid #e5e7eb'>{html.escape(location)}</td>"
            f"<td style='padding:12px;border-bottom:1px solid #e5e7eb;text-align:center'>{score}/10</td></tr>"
        )
        plain_rows.append(f"{title} | {company} | {score}/10 | {location}\nApply: {link}")
    html_body = (
        "<html><body style='font-family:Arial,sans-serif;color:#101828;max-width:900px;margin:auto'>"
        f"<h2>Fresh Product Marketing roles — {date_label}</h2>"
        f"<p>Found {len(jobs)} new role(s), sorted by fit score.</p>"
        "<table style='border-collapse:collapse;width:100%'><thead><tr>"
        "<th align='left' style='padding:12px'>Role and company</th>"
        "<th align='left' style='padding:12px'>Location</th>"
        "<th style='padding:12px'>Fit</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></body></html>"
    )
    plain = f"Fresh Product Marketing roles — {date_label}\nFound {len(jobs)} new role(s).\n\n" + "\n\n".join(plain_rows)
    return plain, html_body


def send_email(jobs: list[dict[str, Any]], recipient: str) -> None:
    sender = os.getenv("GMAIL_ADDRESS")
    password = os.getenv("GMAIL_APP_PASSWORD")
    if not sender or not password:
        raise RuntimeError("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD to send email")
    plain, rich = format_email(jobs)
    message = EmailMessage()
    paris_now = datetime.now(ZoneInfo("Europe/Paris"))
    message["Subject"] = f"{len(jobs)} fresh PMM job match(es) — {paris_now:%d %b}"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(plain)
    message.add_alternative(rich, subtype="html")
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.send_message(message)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config()
    recipient = os.getenv("JOB_ALERT_EMAIL") or config.get("notification_email")
    if not recipient:
        raise RuntimeError("Set JOB_ALERT_EMAIL or notification_email in config.yaml")
    history = load_history()
    jobs = search_adzuna(config)
    matches = filter_and_score(jobs, config, history)
    LOG.info("Fetched %d unique listings; %d fresh listings passed filters", len(jobs), len(matches))
    if not matches:
        LOG.info("No fresh matches to email")
        return 0
    send_email(matches, recipient)
    history.update(job_identifier(job) for job in matches)
    save_history(history)
    LOG.info("Emailed %d fresh matches to %s", len(matches), recipient)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.RequestException as exc:
        LOG.error("Adzuna request failed: %s", exc)
        sys.exit(1)
    except (RuntimeError, ValueError, OSError) as exc:
        LOG.error("Job hunter failed: %s", exc)
        sys.exit(1)
