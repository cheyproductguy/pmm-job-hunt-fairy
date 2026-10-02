"""Find, score, and email the best relevant PMM roles from Adzuna."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import smtplib
import ssl
import sys
import time
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
ACRONYM_SIGNALS = {"b2b", "b2c", "b2b2c", "d2c", "sme", "pme", "voc", "jtbd", "sdr", "bdr"}

TITLE_SEARCH_TERMS = {
    "Product Marketing Manager": ("Product Marketing Manager", "Responsable marketing produit"),
    "PMM": ("PMM", "Product Marketing"),
    "Go-to-Market Manager": ("Go-to-Market Manager", "Responsable go-to-market"),
    "GTM Manager": ("GTM Manager", "Responsable GTM"),
    "Product Marketing Lead": ("Product Marketing Lead", "Lead marketing produit"),
    "Proposition Manager": ("Proposition Manager", "Responsable développement de l'offre"),
    "Market Insights Manager": ("Market Insights Manager", "Responsable études marketing"),
    "Customer Insights Manager": ("Customer Insights Manager", "Responsable insights clients"),
    "Product Strategy Manager": ("Product Strategy Manager", "Stratégie produit"),
    "Portfolio Marketing Manager": ("Portfolio Marketing Manager", "Marketing de portefeuille"),
    "Growth Marketing Manager": ("Growth Marketing Manager", "Growth marketing"),
    "Chef de produit": ("Chef de produit", "Product Manager"),
}


def normalize(text: str) -> str:
    """Lowercase text and remove accents for bilingual substring matching."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as stream:
        config = yaml.safe_load(stream) or {}
    if not config.get("target_titles") or not (
        config.get("negative_title_keywords") or config.get("negative_keywords")
    ):
        raise ValueError("config.yaml must define target_titles and title-exclusion keywords")
    return config


def load_history() -> set[str]:
    try:
        with HISTORY_PATH.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if isinstance(data, list):
            return {str(item) for item in data}
        if isinstance(data, dict):
            return {str(item) for item in data.get("sent_ids", [])}
        return set()
    except FileNotFoundError:
        return set()
    except (json.JSONDecodeError, OSError) as exc:
        LOG.warning("Could not read sent-job history (%s); starting empty", exc)
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


def canonical_job_key(job: dict[str, Any]) -> str:
    """Collapse duplicate copies of one posting returned by different search queries."""
    company = job.get("company", {})
    location = job.get("location", {})
    company_name = company.get("display_name", "") if isinstance(company, dict) else company
    location_name = location.get("display_name", "") if isinstance(location, dict) else location

    def compact(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", normalize(str(value))).strip()

    parts = [
        str(job.get("_country_code", "")),
        compact(job.get("title", "")),
        compact(company_name),
        compact(location_name),
    ]
    if not parts[1] or not parts[2]:
        return job_identifier(job)
    return "|".join(parts)


def industry_label(job: dict[str, Any], config: dict[str, Any]) -> str:
    """Use a matched target sector, then fall back to Adzuna's job category."""
    industries = config.get("target_industries", {})
    matched = [name.replace("_", " ").title() for name, phrases in industries.items() if contains_any(job_text(job), phrases)]
    if matched:
        return ", ".join(matched)

    category = job.get("category", {})
    if isinstance(category, dict):
        label = category.get("label") or category.get("tag")
    else:
        label = category
    return str(label or "Not specified")


def request_adzuna_page(
    session: requests.Session,
    url: str,
    params: dict[str, Any],
    retries: int,
    backoff_seconds: float,
    max_retry_delay: float,
) -> tuple[requests.Response | None, str | None, int]:
    """Retry transient Adzuna/network errors without logging credential URLs."""
    retryable_statuses = {429, 500, 502, 503, 504}
    last_status: str | None = None
    attempts = retries + 1
    for attempt in range(attempts):
        try:
            response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            last_status = type(exc).__name__
            if attempt + 1 >= attempts:
                return None, last_status, attempts
            delay = min(max_retry_delay, backoff_seconds * (2**attempt))
            LOG.warning("Adzuna network error (%s); retrying in %.1fs", last_status, delay)
            time.sleep(delay)
            continue

        if response.ok:
            return response, None, attempt + 1
        status = response.status_code
        last_status = str(status)
        if status not in retryable_statuses:
            raise RuntimeError(
                f"Adzuna rejected the search request (HTTP {status}); "
                "check API credentials, account status, and request limits."
            )
        if attempt + 1 >= attempts:
            return None, last_status, attempts

        retry_after = response.headers.get("Retry-After", "")
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = backoff_seconds * (2**attempt)
        delay = min(max_retry_delay, max(0.0, delay))
        LOG.warning("Adzuna returned HTTP %s; retrying in %.1fs", status, delay)
        time.sleep(delay)
    return None, last_status, attempts


def search_adzuna(config: dict[str, Any]) -> list[dict[str, Any]]:
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        raise RuntimeError("Set ADZUNA_APP_ID and ADZUNA_APP_KEY environment variables")

    titles = config["target_titles"]
    # Configured search_terms are curated for breadth. Add title aliases while
    # enforcing a query ceiling to keep daily Adzuna API use predictable.
    terms = list(dict.fromkeys(str(term) for term in config.get("search_terms", [])))
    for title in titles:
        for term in TITLE_SEARCH_TERMS.get(title, (title,)):
            if term not in terms:
                terms.append(term)

    search_cfg = config.get("search", {})
    per_page = int(search_cfg.get("results_per_query", 30))
    pages = int(search_cfg.get("pages", 2))
    max_queries = int(search_cfg.get("max_queries_per_country", 18))
    retries = int(search_cfg.get("api_retries", 3))
    backoff_seconds = float(search_cfg.get("retry_backoff_seconds", 2))
    max_retry_delay = float(search_cfg.get("max_retry_delay_seconds", 30))
    max_consecutive_failures = int(search_cfg.get("max_consecutive_failed_pages", 4))
    terms = terms[:max_queries]
    found: dict[str, dict[str, Any]] = {}
    duplicates_collapsed = 0
    session = requests.Session()
    successful_pages = 0
    failed_pages = 0
    consecutive_failures = 0
    stop_search = False
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
                response, error, attempts = request_adzuna_page(
                    session,
                    ADZUNA_URL.format(country=country, page=page),
                    params,
                    retries,
                    backoff_seconds,
                    max_retry_delay,
                )
                if response is None:
                    failed_pages += 1
                    consecutive_failures += 1
                    LOG.warning(
                        "Skipping Adzuna %s page %d query '%s' after %d attempts (%s)",
                        country,
                        page,
                        term,
                        attempts,
                        error or "temporary API error",
                    )
                    if consecutive_failures >= max_consecutive_failures:
                        LOG.error("Stopping Adzuna search after repeated consecutive failures")
                        stop_search = True
                        break
                    continue
                successful_pages += 1
                consecutive_failures = 0
                for job in response.json().get("results", []):
                    job["_country_code"] = country
                    key = canonical_job_key(job)
                    if key in found:
                        duplicates_collapsed += 1
                    else:
                        found[key] = job
            if stop_search:
                break
        if stop_search:
            break
    if successful_pages == 0:
        raise RuntimeError(
            f"Adzuna was unavailable: all {failed_pages} search pages failed after retries. "
            "Try the workflow again later."
        )
    if failed_pages:
        LOG.warning(
            "Continuing with partial Adzuna results: %d pages succeeded and %d failed",
            successful_pages,
            failed_pages,
        )
    if duplicates_collapsed:
        LOG.info("Collapsed %d duplicate listing copies", duplicates_collapsed)
    return list(found.values())


def contains_any(text: str, phrases: list[str] | tuple[str, ...]) -> bool:
    normalized = normalize(text)
    for phrase in phrases:
        candidate = normalize(phrase)
        if candidate in ACRONYM_SIGNALS:
            if re.search(rf"(?<![a-z0-9]){re.escape(candidate)}(?![a-z0-9])", normalized):
                return True
        elif candidate in normalized:
            return True
    return False


def posted_at(job: dict[str, Any]) -> datetime | None:
    created = job.get("created")
    if not created:
        return None
    try:
        parsed = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def posting_age_days(job: dict[str, Any]) -> float | None:
    posted = posted_at(job)
    if posted is None:
        return None
    age = (datetime.now(timezone.utc) - posted).total_seconds() / 86400
    return max(0.0, age)


def posting_date_label(job: dict[str, Any]) -> str:
    posted = posted_at(job)
    if posted is None:
        return "Date unavailable"
    local_posted = posted.astimezone(ZoneInfo("Europe/Paris"))
    age_days = posting_age_days(job)
    whole_days = int(age_days or 0)
    if whole_days == 0:
        age_label = "today"
    elif whole_days == 1:
        age_label = "1 day ago"
    else:
        age_label = f"{whole_days} days ago"
    return f"{local_posted:%d %b %Y} · {age_label}"


def is_recent(job: dict[str, Any], max_age_days: int) -> bool:
    posted = posted_at(job)
    if posted is None:
        return True
    return posted >= datetime.now(timezone.utc) - timedelta(days=max_age_days)


def excessive_experience_requirement(text: str, config: dict[str, Any]) -> str | None:
    """Return a reason when a posting states an excluded minimum experience level."""
    rules = config.get("seniority_filters", {})
    caps = rules.get("screen_out_minimums_at_or_above", {})
    normalized = normalize(text)
    year_units = r"(?:years?|yrs?|ans?|annees?)"
    patterns = (
        re.compile(rf"\b(?P<years>\d{{1,2}})\s*\+\s*{year_units}\b"),
        re.compile(
            rf"\b(?:at least|minimum(?: of)?|must have|required|requires|au moins|minimum de|exige(?:e)?|requis(?:e)?)\s+"
            rf"(?P<years>\d{{1,2}})\s*{year_units}\b"
        ),
        re.compile(
            rf"\b(?P<years>\d{{1,2}})\s*{year_units}"
            rf"[^.!?;\n]{{0,25}}\b(?:minimum|required|at least|au moins)\b"
        ),
    )
    preferred_signals = [normalize(item) for item in rules.get("preferred_signals", [])]
    b2b_pattern = re.compile(
        r"(?<![a-z0-9])(?:b2b|business[- ]to[- ]business)(?![a-z0-9])"
    )
    mixed_b2b_pattern = re.compile(
        r"(?<![a-z0-9])b2b2c(?![a-z0-9])|"
        r"(?<![a-z0-9])b2b\s*(?:/|&|and|or|to)\s*b2c(?![a-z0-9])|"
        r"(?<![a-z0-9])b2c\s*(?:/|&|and|or|to)\s*b2b(?![a-z0-9])"
    )
    pure_b2b_required = bool(rules.get("exclude_explicit_pure_b2b_requirement", True))
    required_signals = [normalize(item) for item in rules.get("pure_b2b_requirement_signals", [])]

    for pattern in patterns:
        for match in pattern.finditer(normalized):
            years = int(match.group("years"))
            # Limit context to the same sentence so nearby unrelated benefits
            # or qualifications do not change the meaning of the requirement.
            left_edge = max(normalized.rfind(mark, 0, match.start()) for mark in (".", ";", "!", "?", "\n")) + 1
            right_candidates = [normalized.find(mark, match.end()) for mark in (".", ";", "!", "?", "\n")]
            right_candidates = [position for position in right_candidates if position >= 0]
            right_edge = min(right_candidates) if right_candidates else len(normalized)
            sentence = normalized[left_edge:right_edge]
            if any(signal in sentence for signal in preferred_signals):
                continue

            is_pure_b2b = bool(b2b_pattern.search(sentence)) and not bool(mixed_b2b_pattern.search(sentence))
            if is_pure_b2b and rules.get("exclude_explicit_pure_b2b_minimum", True):
                return f"{years}+ years required in a pure B2B context"
            cap = int(caps.get("overall_years", 0))
            if cap and years >= cap:
                return f"{years}+ years required overall (screen-out threshold: {cap})"

    if pure_b2b_required:
        for sentence in re.split(r"[.;!?\n]+", normalized):
            if any(signal in sentence for signal in preferred_signals):
                continue
            if not b2b_pattern.search(sentence) or mixed_b2b_pattern.search(sentence):
                continue
            if any(signal in sentence for signal in required_signals):
                return "explicit pure B2B experience requirement"
    return None


def meets_language_rule(text: str, config: dict[str, Any]) -> bool:
    language = config.get("language_rules", {})
    if not language.get("require_english_role_signal", True):
        return True
    return contains_any(text, language.get("english_role_signals", []))


def meets_switzerland_sponsorship(job: dict[str, Any], config: dict[str, Any]) -> bool:
    if job.get("_country_code") != config["locations"]["switzerland"]["country_code"]:
        return True
    visa = config["locations"]["switzerland"].get("visa_sponsorship", {})
    return not visa.get("required", False) or contains_any(job_text(job), visa.get("signals", []))


def workplace_fit(job: dict[str, Any], config: dict[str, Any]) -> float:
    text = normalize(job_text(job))
    remote_signals = ("remote", "fully remote", "teletravail", "100% a distance", "work from anywhere")
    hybrid_signals = ("hybrid", "hybride", "hybrid working")
    office_signals = ("on-site", "onsite", "in-office", "office based", "sur site", "presentiel")
    france = config["locations"]["france"]
    geneva = config["locations"]["switzerland"]
    has_workplace_model = contains_any(text, hybrid_signals + office_signals)
    has_remote = contains_any(text, remote_signals)
    if job.get("_country_code") == geneva["country_code"]:
        location_match = contains_any(text, geneva.get("preferred", []))
        if location_match and has_workplace_model:
            return float(config.get("scoring", {}).get("workplace", {}).get("geneva_hybrid_or_office", 1.0))
    location_match = contains_any(text, france.get("preferred", []) + france.get("commute_hubs", []))
    if location_match and has_workplace_model:
        return float(config.get("scoring", {}).get("workplace", {}).get("local_hybrid_or_office", 1.0))
    workplace = config.get("scoring", {}).get("workplace", {})
    if has_remote and config.get("locations", {}).get("include_remote", True):
        return float(workplace.get("remote", 0.35))
    if not has_workplace_model:
        return float(workplace.get("unspecified", 0.45))
    return float(workplace.get("other", 0.2))


def language_fit(text: str, config: dict[str, Any]) -> float:
    rules = config.get("language_rules", {})
    scoring = config.get("scoring", {}).get("language", {})
    if contains_any(text, rules.get("english_role_signals", [])):
        return float(scoring.get("english_signal", 1.0))
    if contains_any(text, rules.get("french_role_signals", [])):
        return float(scoring.get("french_signal", 0.8))
    return float(scoring.get("unspecified", 0.65))


def visa_fit(job: dict[str, Any], config: dict[str, Any]) -> float:
    if job.get("_country_code") != config["locations"]["switzerland"]["country_code"]:
        return 1.0
    visa = config["locations"]["switzerland"].get("visa_sponsorship", {})
    if contains_any(job_text(job), visa.get("signals", [])):
        return float(config.get("scoring", {}).get("visa", {}).get("sponsorship_signal", 1.0))
    return float(config.get("scoring", {}).get("visa", {}).get("unspecified", 0.5))


def score_job(job: dict[str, Any], config: dict[str, Any]) -> tuple[float, list[str]]:
    raw_text = job_text(job)
    text = normalize(raw_text)
    competencies = config.get("core_competencies", {})
    matched_competencies = [
        group
        for group, phrases in competencies.items()
        if contains_any(text, phrases)
    ]
    industries = config.get("target_industries", {})
    matched_industries = [
        name for name, phrases in industries.items() if contains_any(text, phrases)
    ]
    business_models = config.get("business_model_focus", {})
    matched_models = [
        name for name, phrases in business_models.items() if contains_any(text, phrases)
    ]
    direct_groups = [name for name in competencies if name != "role_scope"]
    features = {
        "direct_experience": sum(name in matched_competencies for name in direct_groups) / max(1, len(direct_groups)),
        "transferable_responsibilities": float("role_scope" in matched_competencies),
        "industry": len(matched_industries) / max(1, len(industries)),
        "business_model": len(matched_models) / max(1, len(business_models)),
        "workplace": workplace_fit(job, config),
        "language": language_fit(raw_text, config),
        "visa": visa_fit(job, config),
    }
    profiles = config.get("scoring", {}).get("profiles", {})
    profile_name = config.get("scoring", {}).get("active_profile", "balanced")
    weights = profiles.get(profile_name)
    if not weights:
        raise ValueError(f"Unknown scoring profile: {profile_name}")
    total_weight = sum(float(weight) for weight in weights.values())
    if total_weight <= 0:
        raise ValueError("Scoring profile must have a positive total weight")
    total = 10.0 * sum(float(weights.get(key, 0)) * value for key, value in features.items()) / total_weight
    age_days = posting_age_days(job)
    recency_rules = config.get("scoring", {}).get("recency_bonus", {})
    if age_days is not None:
        if age_days <= 2:
            total += float(recency_rules.get("within_2_days", 0.5))
        elif age_days <= 7:
            total += float(recency_rules.get("within_7_days", 0.2))
    total = min(10.0, total)
    matched = matched_competencies + [f"industry:{item}" for item in matched_industries]
    return round(total, 1), matched


def filter_and_score(
    jobs: list[dict[str, Any]], config: dict[str, Any], history: set[str]
) -> list[dict[str, Any]]:
    negatives = [normalize(item) for item in config.get("negative_title_keywords", config.get("negative_keywords", []))]
    max_age = int(config.get("search", {}).get("max_age_days", 30))
    minimum = float(config.get("search", {}).get("minimum_fit_score", 0))
    matches: list[dict[str, Any]] = []
    seniority_excluded = 0
    for job in jobs:
        title = normalize(str(job.get("title", "")))
        if any(word in title for word in negatives):
            continue
        if (
            not is_recent(job, max_age)
            or canonical_job_key(job) in history
            or job_identifier(job) in history
        ):
            continue
        seniority_reason = excessive_experience_requirement(job_text(job), config)
        if seniority_reason:
            seniority_excluded += 1
            LOG.info("Excluded '%s': %s", job.get("title", "Untitled role"), seniority_reason)
            continue
        if not meets_language_rule(job_text(job), config) or not meets_switzerland_sponsorship(job, config):
            continue
        score, matched = score_job(job, config)
        if score < minimum:
            continue
        job["fit_score"] = score
        job["matched_keywords"] = matched
        job["industry_label"] = industry_label(job, config)
        job["posted_label"] = posting_date_label(job)
        matches.append(job)
    if seniority_excluded:
        LOG.info("Screened out %d listing(s) for explicit experience minimums", seniority_excluded)
    return sorted(matches, key=lambda job: (-job["fit_score"], str(job.get("created", ""))))


def safe_url(value: str) -> str:
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else "#"


def load_cv() -> str:
    """Load the bilingual profile used for optional AI match rationales."""
    try:
        return CV_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        LOG.warning("Could not read CV profile for rationales: %s", exc)
        return ""


def get_ai_rationale(job: dict[str, Any], cv_text: str) -> str | None:
    """Generate an optional, brief fit rationale when OPENAI_API_KEY is set."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or not cv_text:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        prompt = (
            "Compare this candidate profile with the job. In two concise sentences, "
            "explain the strongest transferable fit and one material gap or unknown. "
            "Do not invent qualifications.\n\n"
            f"Candidate profile:\n{cv_text[:5000]}\n\n"
            f"Job title: {job.get('title', '')}\n"
            f"Company: {job.get('company', {}).get('display_name', '')}\n"
            f"Location: {job.get('location', {}).get('display_name', '')}\n"
            f"Job description:\n{str(job.get('description', ''))[:3000]}"
        )
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.3,
        )
        content = response.choices[0].message.content
        return content.strip() if content else None
    except Exception as exc:  # AI summaries are optional; search/email still work.
        LOG.warning("Could not generate an AI rationale: %s", exc)
        return None


def format_email(jobs: list[dict[str, Any]], user_name: str) -> tuple[str, str]:
    paris_now = datetime.now(ZoneInfo("Europe/Paris"))
    date_label = paris_now.strftime("%d %b %Y")
    first_name = user_name.strip().split()[0] if user_name.strip() else "Friend"
    preview_text = "Job matches based on your PMM magic✨"
    encouragement = (
        f"Good morning, {user_name.strip() or first_name}! Your PMM Job Hunt Fairy has been "
        "scouting beyond the usual titles for roles that fit your experience and career-quest "
        "criteria. Here are today's best matches—may one lead to your next great adventure."
    )
    rows = []
    plain_rows = []
    for job in jobs:
        title = str(job.get("title", "Untitled role"))
        company = str(job.get("company", {}).get("display_name", "Company not listed"))
        location = str(job.get("location", {}).get("display_name", "Location not listed"))
        sector = str(job.get("industry_label") or "Not specified")
        posted = str(job.get("posted_label") or "Date unavailable")
        link = safe_url(str(job.get("redirect_url", "")))
        score = job["fit_score"]
        rationale = str(job.get("rationale") or "")
        rationale_html = (
            f"<br><span style='color:#475467'>{html.escape(rationale)}</span>"
            if rationale
            else ""
        )
        rows.append(
            "<tr><td style='padding:12px;border-bottom:1px solid #e5e7eb'>"
            f"<a href='{html.escape(link, quote=True)}' style='font-weight:600;color:#155eef'>"
            f"{html.escape(title)}</a><br><span style='color:#667085'>{html.escape(company)}</span>"
            f"{rationale_html}</td>"
            f"<td style='padding:12px;border-bottom:1px solid #e5e7eb'>{html.escape(sector)}</td>"
            f"<td style='padding:12px;border-bottom:1px solid #e5e7eb'>{html.escape(location)}</td>"
            f"<td style='padding:12px;border-bottom:1px solid #e5e7eb'>{html.escape(posted)}</td>"
            f"<td style='padding:12px;border-bottom:1px solid #e5e7eb;text-align:center'>{score}/10</td></tr>"
        )
        plain_rows.append(
            f"{title} | {company} | {sector} | {score}/10 | {location} | Posted {posted}\nApply: {link}"
            + (f"\nWhy it may fit: {rationale}" if rationale else "")
        )
    html_body = (
        "<html><body style='font-family:Arial,sans-serif;color:#101828;max-width:900px;margin:auto'>"
        f"<div style='display:none;max-height:0;overflow:hidden;opacity:0;color:transparent'>{html.escape(preview_text)}</div>"
        f"<h2>{html.escape(first_name)}’s PMM Job Hunt Fairy 🧚</h2>"
        f"<p>{html.escape(date_label)}</p>"
        f"<p>{html.escape(encouragement)}</p>"
        f"<p>Your top {len(jobs)} eligible role(s), sorted by fit score:</p>"
        "<table style='border-collapse:collapse;width:100%'><thead><tr>"
        "<th align='left' style='padding:12px'>Role and company</th>"
        "<th align='left' style='padding:12px'>Industry / category</th>"
        "<th align='left' style='padding:12px'>Location</th>"
        "<th align='left' style='padding:12px'>Posted</th>"
        "<th style='padding:12px'>Fit</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></body></html>"
    )
    plain = (
        f"{preview_text}\n\n{first_name}'s PMM Job Hunt Fairy — {date_label}\n\n"
        f"{encouragement}\n\nYour top {len(jobs)} eligible role(s), sorted by fit score:\n\n"
        + "\n\n".join(plain_rows)
    )
    return plain, html_body


def send_email(jobs: list[dict[str, Any]], recipient: str, user_name: str) -> None:
    sender = os.getenv("GMAIL_ADDRESS")
    password = os.getenv("GMAIL_APP_PASSWORD")
    if not sender or not password:
        raise RuntimeError("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD to send email")
    plain, rich = format_email(jobs, user_name)
    message = EmailMessage()
    date_label = datetime.now(ZoneInfo("Europe/Paris")).strftime("%d %b %Y")
    message["Subject"] = f"🧚 PMM Job Hunt Fairy | {date_label}"
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
    user_name = os.getenv("JOB_ALERT_NAME") or str(config.get("user_name", "Cheyguy"))
    history = load_history()
    jobs = search_adzuna(config)
    matches = filter_and_score(jobs, config, history)
    LOG.info("Fetched %d unique listings; %d unseen listings passed filters", len(jobs), len(matches))
    if not matches:
        LOG.info("No listings passed the configured filters")
        return 0
    max_email_results = max(1, int(config.get("search", {}).get("max_email_results", 8)))
    email_jobs = matches[:max_email_results]
    if len(matches) > max_email_results:
        LOG.info("Limiting digest to the top %d of %d matching roles", max_email_results, len(matches))
    if os.getenv("OPENAI_API_KEY"):
        cv_text = load_cv()
        for job in email_jobs:
            rationale = get_ai_rationale(job, cv_text)
            if rationale:
                job["rationale"] = rationale
    send_email(email_jobs, recipient, user_name)
    history.update(canonical_job_key(job) for job in email_jobs)
    history.update(job_identifier(job) for job in email_jobs)
    save_history(history)
    LOG.info("Emailed the top %d matches to %s", len(email_jobs), recipient)
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
