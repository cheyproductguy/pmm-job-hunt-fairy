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

from filters import compile_title_patterns, engineering_degree_requirement, matching_title_keyword


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
CV_PATH = ROOT / "cv.txt"
HISTORY_PATH = Path(os.getenv("JOB_HISTORY_PATH", ROOT / ".job_history.json"))
ADZUNA_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
LOG = logging.getLogger("pmm_job_hunter")
REQUEST_TIMEOUT = 25
ACRONYM_SIGNALS = {"b2b", "b2c", "b2b2c", "d2c", "dtc", "sme", "pme", "voc", "jtbd", "sdr", "bdr"}

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
    # Keep the French product/offer role searchable without querying generic
    # Product Manager roles, which usually require product ownership skills.
    "Chef de produit": ("Chef de produit",),
}


def normalize(text: str) -> str:
    """Lowercase text and remove accents for bilingual substring matching."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def unique_terms(terms: list[str]) -> list[str]:
    """Preserve term order while deduplicating case- and accent-insensitively."""
    unique: list[str] = []
    seen: set[str] = set()
    for term in terms:
        value = str(term).strip()
        key = normalize(value)
        if key and key not in seen:
            unique.append(value)
            seen.add(key)
    return unique


def spread_title_terms(groups: list[list[str]], limit: int) -> list[str]:
    """Select title variants across the full target list, then add extra aliases."""
    groups = [unique_terms(group) for group in groups if group]
    if limit <= 0 or not groups:
        return []
    selected: list[str] = []
    if len(groups) > limit:
        indexes = sorted({round(i * (len(groups) - 1) / max(1, limit - 1)) for i in range(limit)})
        selected.extend(groups[index][0] for index in indexes if groups[index])
    else:
        for variant in range(max(map(len, groups))):
            for group in groups:
                if variant < len(group) and len(selected) < limit:
                    selected.append(group[variant])
    if len(selected) < limit:
        for group in groups:
            for term in group:
                if term not in selected and len(selected) < limit:
                    selected.append(term)
    return selected[:limit]


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


def matched_industries(job: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """Return target sectors, applying configured context-based deprioritizers."""
    industries = config.get("target_industries", {})
    text = job_text(job)
    filters = config.get("industry_context_filters", {})
    business_models = config.get("business_model_focus", {})
    matched = []
    for name, phrases in industries.items():
        if not contains_any(text, phrases):
            continue
        rule = filters.get(name, {})
        if contains_any(text, rule.get("deprioritize_if_any", [])):
            exempt_models = rule.get("unless_business_models", [])
            if not any(contains_any(text, business_models.get(model, [])) for model in exempt_models):
                continue
        matched.append(name)
    return matched


def industry_label(job: dict[str, Any], config: dict[str, Any]) -> str:
    """Use a matched target sector, then fall back to Adzuna's job category."""
    matched = [name.replace("_", " ").title() for name in matched_industries(job, config)]
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
    search_cfg = config.get("search", {})
    per_page = int(search_cfg.get("results_per_query", 30))
    pages = int(search_cfg.get("pages", 2))
    max_queries = int(search_cfg.get("max_queries_per_country", 18))
    core_terms = unique_terms([str(term) for term in config.get("search_terms", [])])
    partner_channel_terms = unique_terms(
        [str(term) for term in config.get("partner_channel_search_terms", [])]
    )
    retail_marketing_terms = unique_terms(
        [str(term) for term in config.get("retail_marketing_search_terms", [])]
    )
    model_terms = unique_terms([str(term) for term in config.get("business_model_search_terms", [])])
    title_groups = [list(TITLE_SEARCH_TERMS.get(title, (title,))) for title in titles]
    core_slots = min(max(0, int(search_cfg.get("core_query_slots", 6))), max_queries)
    partner_channel_slots = min(
        max(0, int(search_cfg.get("partner_channel_query_slots", 0))),
        max_queries - core_slots,
    )
    retail_marketing_slots = min(
        max(0, int(search_cfg.get("retail_marketing_query_slots", 0))),
        max_queries - core_slots - partner_channel_slots,
    )
    model_slots = min(
        max(0, int(search_cfg.get("business_model_query_slots", 7))),
        max_queries - core_slots - partner_channel_slots - retail_marketing_slots,
    )
    title_slots = max_queries - core_slots - partner_channel_slots - retail_marketing_slots - model_slots
    terms = unique_terms(
        core_terms[:core_slots]
        + partner_channel_terms[:partner_channel_slots]
        + retail_marketing_terms[:retail_marketing_slots]
        + model_terms[:model_slots]
        + spread_title_terms(title_groups, title_slots)
    )
    # Backfill any slots freed by duplicates, keeping the request ceiling fixed.
    remaining_title_terms = [term for group in title_groups for term in group]
    seen_terms = {normalize(term) for term in terms}
    for term in (
        core_terms[core_slots:]
        + partner_channel_terms[partner_channel_slots:]
        + retail_marketing_terms[retail_marketing_slots:]
        + model_terms[model_slots:]
        + remaining_title_terms
    ):
        key = normalize(term)
        if len(terms) >= max_queries:
            break
        if key not in seen_terms:
            terms.append(term)
            seen_terms.add(key)

    retries = int(search_cfg.get("api_retries", 3))
    backoff_seconds = float(search_cfg.get("retry_backoff_seconds", 2))
    max_retry_delay = float(search_cfg.get("max_retry_delay_seconds", 30))
    max_consecutive_failures = int(search_cfg.get("max_consecutive_failed_pages", 4))
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
        # French and English ranges such as "5 à 10 ans", "5-10 years",
        # "between 5 and 10 years", and "entre 5 et 10 ans".
        re.compile(
            rf"\b(?:between\s+|entre\s+)?(?P<years>\d{{1,2}})\s*"
            rf"(?:a|to|and|et|[-–—])\s*(?P<upper>\d{{1,2}})\s*{year_units}\b"
        ),
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
    # Looser phrasings without "minimum" or "+": "10 ans d'expérience",
    # "10 years of experience", "plus de 10 ans". These are skipped when the
    # sentence talks about the company ("depuis plus de 30 ans, nous...").
    loose_patterns = (
        re.compile(
            rf"\b(?P<years>\d{{1,2}})\s*{year_units}\s*"
            rf"(?:d\W{{0,2}}|de\s+|of\s+|\W{{0,2}}\s*)experience\b"
        ),
        re.compile(rf"\b(?:plus de|more than|over)\s+(?P<years>\d{{1,2}})\s*{year_units}\b"),
    )
    company_context = re.compile(
        r"\b(?:nous|notre|nos|we|our|depuis|since|founded|fondee?|leader|history|histoire)\b"
    )
    preferred_signals = [normalize(item) for item in rules.get("preferred_signals", [])]
    direct_pmm_signals = [
        normalize(item) for item in rules.get("direct_pmm_signals", [])
    ]
    direct_pmm_cap = int(caps.get("direct_pmm_years", 0))
    preferred_before = int(rules.get("preferred_window_chars_before", 25))
    preferred_after = int(rules.get("preferred_window_chars_after", 30))
    b2b_term = r"(?:b\s*(?:2|to)\s*b|bto[b]|business[- ]to[- ]business)"
    b2c_term = r"b\s*(?:2|to)\s*c"
    b2b2c_term = r"b\s*(?:2|to)\s*b\s*(?:2|to)\s*c"
    b2b_pattern = re.compile(
        rf"(?<![a-z0-9]){b2b_term}(?![a-z0-9])"
    )
    mixed_b2b_pattern = re.compile(
        rf"(?<![a-z0-9]){b2b2c_term}(?![a-z0-9])|"
        rf"(?<![a-z0-9]){b2b_term}\s*(?:/|&|and|or|to|,)\s*{b2c_term}(?![a-z0-9])|"
        rf"(?<![a-z0-9]){b2c_term}\s*(?:/|&|and|or|to|,)\s*{b2b_term}(?![a-z0-9])"
    )
    pure_b2b_required = bool(rules.get("exclude_explicit_pure_b2b_requirement", True))
    required_signals = [normalize(item) for item in rules.get("pure_b2b_requirement_signals", [])]

    for pattern in patterns + loose_patterns:
        is_loose = pattern in loose_patterns
        for match in pattern.finditer(normalized):
            years = int(match.group("years"))
            # Limit context to the same sentence so nearby unrelated benefits
            # or qualifications do not change the meaning of the requirement.
            left_edge = max(normalized.rfind(mark, 0, match.start()) for mark in (".", ";", "!", "?", "\n")) + 1
            right_candidates = [normalized.find(mark, match.end()) for mark in (".", ";", "!", "?", "\n")]
            right_candidates = [position for position in right_candidates if position >= 0]
            right_edge = min(right_candidates) if right_candidates else len(normalized)
            sentence = normalized[left_edge:right_edge]
            if is_loose and company_context.search(sentence):
                continue
            # For ranges like "5 à 8 ans", only the lower bound counts (handled by the
            # range pattern), so ignore the upper number here.
            if is_loose and re.search(r"\d\s*(?:a|to|and|et|[-–—])\s*$", normalized[: match.start()]):
                continue
            # Adzuna text often has no sentence breaks, so only look at the words
            # right around the number instead of the whole stretch of text.
            near = (
                normalized[max(0, match.start() - preferred_before) : match.start()]
                + " "
                + normalized[match.end() : match.end() + preferred_after]
            )

            # A high minimum tied specifically to PMM is a hard scope mismatch.
            # Do not confuse an overall marketing tenure requirement with PMM
            # tenure, and do not let "ideally B2B" soften a mandatory PMM minimum.
            pmm_positions = [
                (sentence.find(signal), sentence.find(signal) + len(signal))
                for signal in direct_pmm_signals
                if signal and sentence.find(signal) >= 0
            ]
            pmm_positions = [(start, end) for start, end in pmm_positions]
            if direct_pmm_cap and years >= direct_pmm_cap and pmm_positions:
                preferred_positions = [
                    sentence.find(signal)
                    for signal in preferred_signals
                    if signal and sentence.find(signal) >= 0
                ]
                explicit_pmm_preference = any(
                    0 <= match.start() - left_edge - position <= 45
                    for position in preferred_positions
                )
                if not explicit_pmm_preference:
                    for _, pmm_end in pmm_positions:
                        after_pmm = sentence[pmm_end : pmm_end + 24]
                        if (
                            any(signal in after_pmm for signal in preferred_signals)
                            and not re.search(r"\b(?:b2b|b2c|b2b2c|btob|business to business)\b", sentence)
                        ):
                            explicit_pmm_preference = True
                            break
                if not explicit_pmm_preference:
                    return f"{years}+ years specifically in product marketing (screen-out threshold: {direct_pmm_cap})"

            if any(signal in near for signal in preferred_signals):
                continue

            # A stated experience range tied to pure B2B is itself an explicit
            # experience requirement, even if the posting omits "required".
            is_pure_b2b = bool(b2b_pattern.search(sentence)) and not bool(mixed_b2b_pattern.search(sentence))
            if is_pure_b2b and rules.get("exclude_explicit_pure_b2b_minimum", True):
                upper = match.groupdict().get("upper")
                experience = f"{years}–{upper} years" if upper else f"{years}+ years"
                return f"{experience} stated in a pure B2B context"
            cap = int(caps.get("overall_years", 0))
            if cap and years >= cap:
                return f"{years}+ years required overall (screen-out threshold: {cap})"

    # Also catch plain wording such as "5 years in product marketing" that
    # contains no plus sign, range, or explicit "experience" phrase.
    direct_year_pattern = re.compile(
        rf"\b(?P<years>\d{{1,2}})(?:\s*(?:\+|[-–—]|to|a|and|et)\s*\d{{1,2}})?\s*{year_units}\b"
    )
    for sentence in re.split(r"[.;!?\n]+", normalized):
        pmm_positions = [
            (sentence.find(signal), sentence.find(signal) + len(signal))
            for signal in direct_pmm_signals
            if signal and sentence.find(signal) >= 0
        ]
        if not pmm_positions:
            continue
        for match in direct_year_pattern.finditer(sentence):
            years = int(match.group("years"))
            if not direct_pmm_cap or years < direct_pmm_cap:
                continue
            preferred_positions = [
                sentence.find(signal)
                for signal in preferred_signals
                if signal and sentence.find(signal) >= 0
            ]
            preferred_for_pmm = any(
                0 <= match.start() - position <= 45
                for position in preferred_positions
            )
            if not preferred_for_pmm:
                for _, pmm_end in pmm_positions:
                    after_pmm = sentence[pmm_end : pmm_end + 24]
                    if (
                        any(signal in after_pmm for signal in preferred_signals)
                        and not re.search(r"\b(?:b2b|b2c|b2b2c|btob|business to business)\b", sentence)
                    ):
                        preferred_for_pmm = True
                        break
            if not preferred_for_pmm:
                return f"{years}+ years specifically in product marketing (screen-out threshold: {direct_pmm_cap})"

    if pure_b2b_required or rules.get("exclude_mandatory_b2b_or_saas_experience", False):
        domain_patterns = [b2b_pattern]
        if rules.get("exclude_mandatory_b2b_or_saas_experience", False):
            domain_patterns.append(
                re.compile(r"\b(?:saas|software as a service|logiciel en tant que service)\b")
            )
        for sentence in re.split(r"[.;!?\n]+", normalized):
            required_positions = [
                match.start()
                for signal in required_signals
                if signal
                for match in re.finditer(re.escape(signal), sentence)
            ]
            preferred_positions = [
                match.start()
                for signal in preferred_signals
                if signal
                for match in re.finditer(re.escape(signal), sentence)
            ]
            for domain_pattern in domain_patterns:
                for domain_match in domain_pattern.finditer(sentence):
                    # A specific preferred/ideal signal only softens the nearby
                    # B2B/SaaS criterion; it cannot cancel a closer mandatory cue.
                    required_distance = min(
                        (abs(position - domain_match.start()) for position in required_positions),
                        default=10_000,
                    )
                    preferred_distance = min(
                        (abs(position - domain_match.start()) for position in preferred_positions),
                        default=10_000,
                    )
                    if required_distance <= 65 and required_distance < preferred_distance:
                        if domain_pattern is b2b_pattern and pure_b2b_required:
                            return "explicit mandatory B2B experience requirement"
                        return "explicit mandatory SaaS experience requirement"
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
    english_first = contains_any(text, rules.get("english_role_signals", []))
    high_french = contains_any(text, rules.get("high_french_proficiency_signals", []))
    if high_french and english_first:
        return float(scoring.get("english_with_high_french_requirement", 0.55))
    if high_french:
        return float(scoring.get("high_french_proficiency", 0.35))
    if english_first:
        return float(scoring.get("english_signal", 1.0))
    if contains_any(text, rules.get("french_role_signals", [])):
        return float(scoring.get("french_signal", 0.8))
    return float(scoring.get("unspecified", 0.65))


def seniority_adjustment(job: dict[str, Any], config: dict[str, Any]) -> float:
    """Favor early PMM requirements and flag roles that imply a steep solo ramp."""
    rules = config.get("seniority_filters", {})
    soft = rules.get("soft_downrank", {})
    description = normalize(str(job.get("description", "")))
    title = normalize(str(job.get("title", "")))
    preferred = [normalize(item) for item in rules.get("preferred_signals", [])]
    pmm_signals = (
        "product marketing",
        "pmm",
        "marketing produit",
        "responsable marketing produit",
        "chef de produit marketing",
    )
    similar_signals = [normalize(item) for item in soft.get("similar_role_signals", [])]
    years_pattern = re.compile(
        r"\b(?P<years>\d{1,2})(?:\s*(?:\+|[-–—]|to|a|and|et)\s*\d{1,2})?\s*"
        r"(?:years?|yrs?|ans?|annees?)\b"
    )
    direct_pmm_5plus = False
    pmm_years_in_sweet_spot = False
    similar_pmm_1_2 = False
    threshold = int(soft.get("direct_pmm_years_threshold", 5))
    desired_max = int(soft.get("desired_similar_role_years_max", 2))

    for match in years_pattern.finditer(description):
        left = max(description.rfind(mark, 0, match.start()) for mark in (".", ";", "!", "?", "\n")) + 1
        right_candidates = [description.find(mark, match.end()) for mark in (".", ";", "!", "?", "\n")]
        right_candidates = [index for index in right_candidates if index >= 0]
        right = min(right_candidates) if right_candidates else len(description)
        sentence = description[left:right]
        near = description[max(left, match.start() - 35) : min(right, match.end() + 45)]
        is_preferred_requirement = any(signal and signal in near for signal in preferred)

        years = int(match.group("years"))
        has_pmm_context = any(signal in sentence for signal in pmm_signals)
        if not has_pmm_context and any(signal in sentence for signal in similar_signals):
            has_pmm_context = any(signal in title for signal in pmm_signals)
        if not has_pmm_context:
            continue

        # Preferred high-tenure requirements should not be treated as mandatory;
        # preferred 1–2 year PMM experience can still signal a strong fit.
        if is_preferred_requirement and years > desired_max:
            continue

        if years >= threshold:
            direct_pmm_5plus = True
        elif years <= desired_max:
            pmm_years_in_sweet_spot = True
            if any(signal in sentence for signal in similar_signals):
                similar_pmm_1_2 = True

    penalty = 0.0
    if direct_pmm_5plus:
        penalty += float(soft.get("direct_pmm_years_penalty", 0.8))

    scope_signals = soft.get("solo_or_build_scope_signals", [])
    solo_scope = contains_any(description, scope_signals)
    if solo_scope:
        penalty += float(soft.get("solo_scope_penalty", 0.8))
        senior_titles = [normalize(item) for item in soft.get("senior_title_signals", [])]
        if any(re.search(rf"(?<![a-z0-9]){re.escape(item)}(?![a-z0-9])", title) for item in senior_titles):
            penalty += float(soft.get("senior_with_solo_scope_additional_penalty", 0.4))

    penalty = min(penalty, float(soft.get("max_penalty", 1.8)))
    if penalty:
        return -penalty
    if pmm_years_in_sweet_spot:
        return float(soft.get("direct_pmm_years_bonus", 0.5))
    if similar_pmm_1_2:
        return float(soft.get("similar_role_years_bonus", 0.25))
    return 0.0


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
    matched_industry_names = matched_industries(job, config)
    business_models = config.get("business_model_focus", {})
    matched_models = [
        name for name, phrases in business_models.items() if contains_any(text, phrases)
    ]
    model_fit = config.get("scoring", {}).get("business_model_fit", {})
    if "consumer_b2c" in matched_models:
        business_model_score = float(model_fit.get("consumer_b2c", 1.0))
    elif "b2b2c_and_partner" in matched_models:
        business_model_score = float(model_fit.get("b2b2c_and_partner", 0.9))
    elif "b2b_transferable" in matched_models:
        business_model_score = float(model_fit.get("b2b_transferable", 0.3))
    else:
        business_model_score = float(model_fit.get("unspecified", 0.45))
    direct_groups = [name for name in competencies if name != "role_scope"]
    features = {
        "direct_experience": sum(name in matched_competencies for name in direct_groups) / max(1, len(direct_groups)),
        "transferable_responsibilities": float("role_scope" in matched_competencies),
        "industry": len(matched_industry_names) / max(1, len(industries)),
        "business_model": business_model_score,
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
    market_scope_rules = config.get("scoring", {}).get("english_market_scope_bonus", {})
    has_target_english_market = contains_any(
        text, market_scope_rules.get("target_market_signals", [])
    )
    has_campaign_scope = contains_any(
        text, market_scope_rules.get("campaign_scope_signals", [])
    )
    english_market_bonus = has_target_english_market and has_campaign_scope
    if english_market_bonus:
        total += float(market_scope_rules.get("points", 0.4))
    role_fit = config.get("role_fit_adjustments", {})
    if (
        role_lane(job) == "marketing_management"
        and contains_any(text, role_fit.get("paid_channel_signals", []))
        and contains_any(text, role_fit.get("direct_execution_signals", []))
    ):
        total -= float(role_fit.get("hands_on_paid_execution_penalty", 1.0))
    partner_bonus = config.get("scoring", {}).get("partner_channel_bonus", {})
    bonus_matches: list[str] = []
    if role_lane(job) in {"partner_channel_marketing", "retail_marketing"}:
        bonus_signals = partner_bonus.get("signals", {})
        bonus_matches = [
            name for name, phrases in bonus_signals.items()
            if contains_any(raw_text, phrases)
        ]
        bonus_points = min(
            float(partner_bonus.get("points_per_match", 0.4)) * len(bonus_matches),
            float(partner_bonus.get("max_points", 0.8)),
        )
        total += bonus_points
    total = min(10.0, max(0.0, total + seniority_adjustment(job, config)))
    matched = (
        matched_competencies
        + [f"industry:{item}" for item in matched_industry_names]
        + [f"bonus:{name}" for name in bonus_matches]
        + (["bonus:english_speaking_target_market"] if english_market_bonus else [])
    )
    return round(total, 1), matched


def filter_and_score(
    jobs: list[dict[str, Any]], config: dict[str, Any], history: set[str]
) -> list[dict[str, Any]]:
    # Whole-word, accent-insensitive title matching (see filters.py).
    title_patterns = compile_title_patterns(
        config.get("negative_title_keywords", config.get("negative_keywords", []))
    )
    log_titles = bool(config.get("search", {}).get("log_rejected_titles", False))
    max_age = int(config.get("search", {}).get("max_age_days", 30))
    minimum = float(config.get("search", {}).get("minimum_fit_score", 0))
    matches: list[dict[str, Any]] = []
    seniority_excluded = 0
    for job in jobs:
        bad_keyword = matching_title_keyword(str(job.get("title", "")), title_patterns)
        if bad_keyword:
            if log_titles:
                LOG.info("Title excluded '%s' (matched '%s')", job.get("title", ""), bad_keyword)
            continue
        scope_reason = role_scope_exclusion(job, config)
        if scope_reason:
            LOG.info("Excluded '%s': %s", job.get("title", "Untitled role"), scope_reason)
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
        degree_reason = engineering_degree_requirement(str(job.get("description", "")), config)
        if degree_reason:
            LOG.info("Excluded '%s': %s", job.get("title", "Untitled role"), degree_reason)
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


def role_lane(job: dict[str, Any]) -> str:
    """Place role families into distinct, non-overlapping digest lanes."""
    title = normalize(str(job.get("title", "")))
    description = normalize(str(job.get("description", "")))
    pmm_signals = (
        "product marketing", "pmm", "marketing produit",
        "responsable marketing produit", "chef de produit marketing",
    )
    if any(signal in title for signal in pmm_signals):
        return "product_marketing"
    retail_signals = (
        "retail marketing", "marketing retail", "trade marketing",
        "retail partnership", "retail activation", "activation retail",
        "in-store marketing", "visual merchandising", "retail merchandising",
        "pos marketing", "point-of-sale marketing", "retail promotion",
        "store enablement", "store staff training", "retail network management",
        "retail network manager", "omnichannel marketing", "digital retail marketing",
        "merchandising compliance", "retail performance reporting",
        "marketing en magasin", "marketing en point de vente", "merchandising visuel",
        "merchandising en magasin", "marketing plv", "marketing omnicanal",
        "partenariats retail", "promotions en magasin", "formation des equipes magasin",
        "reporting de performance retail", "conformite merchandising",
    )
    if any(signal in title for signal in retail_signals):
        return "retail_marketing"
    partner_channel_signals = (
        "partner marketing", "channel marketing", "alliance marketing",
        "broker marketing", "broker channel marketing", "trade marketing",
        "brand partnerships", "co-marketing", "partner enablement",
        "channel enablement", "e-commerce partnerships",
        "marketing partenaires", "marketing des courtiers", "marketing de canal",
        "marketing reseau", "marketing de distribution",
    )
    if any(signal in title for signal in partner_channel_signals):
        return "partner_channel_marketing"

    has_pmm_function = any(signal in description for signal in pmm_signals)
    has_pmm_work = any(
        signal in description
        for signal in (
            "positioning", "positionnement", "product messaging", "messaging produit",
            "value proposition", "proposition de valeur", "go-to-market", "lancement produit",
        )
    )
    if has_pmm_function and has_pmm_work:
        return "product_marketing"
    if any(signal in description for signal in retail_signals):
        return "retail_marketing"

    has_partner_channel_function = any(
        signal in description for signal in partner_channel_signals
    )
    has_channel_work = any(
        signal in description
        for signal in (
            "partner enablement", "channel enablement", "broker activation",
            "broker enablement", "retail merchandising", "trade marketing",
            "partner campaigns", "channel campaigns", "marketing partenaires",
            "animation du reseau de courtiers", "merchandising en magasin",
        )
    )
    if has_partner_channel_function and has_channel_work:
        return "partner_channel_marketing"

    return "marketing_management"


def role_scope_exclusion(job: dict[str, Any], config: dict[str, Any]) -> str | None:
    """Exclude full-time CRM/lifecycle roles unless they include meaningful GTM scope."""
    rules = config.get("role_fit_filters", {})
    title = normalize(str(job.get("title", "")))
    if not any(
        normalize(signal) in title
        for signal in rules.get("crm_lifecycle_title_signals", [])
    ):
        return None
    if contains_any(job_text(job), rules.get("crm_lifecycle_gtm_signals", [])):
        return None
    return "CRM/lifecycle role without a clear GTM or product-launch component"


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


def format_email(
    jobs: list[dict[str, Any]], user_name: str, test_mode: bool = False
) -> tuple[str, str]:
    paris_now = datetime.now(ZoneInfo("Europe/Paris"))
    date_label = paris_now.strftime("%d %b %Y")
    first_name = user_name.strip().split()[0] if user_name.strip() else "Friend"
    preview_text = "TEST DIGEST — current criteria, independent of sent-role history." if test_mode else "Job matches based on your PMM magic✨"
    encouragement = (
        f"Good morning, {user_name.strip() or first_name}! Your PMM Job Hunt Fairy has been "
        "scouting beyond the usual titles for roles that fit your experience and career-quest "
        "criteria. Here are today's best matches—may one lead to your next great adventure."
    )
    sections = (
        ("product_marketing", "PMM & Product Marketing", "Roles where product marketing is the core function"),
        ("partner_channel_marketing", "Partner & Channel Marketing", "Partner, broker, channel, and co-marketing roles"),
        ("marketing_management", "Marketing Management", "Campaign strategy, events, brand, and broader marketing roles"),
        ("retail_marketing", "Retail Marketing", "Trade, in-store, merchandising, omnichannel, and retail activation roles"),
    )
    rows_by_lane: dict[str, list[str]] = {key: [] for key, _, _ in sections}
    plain_by_lane: dict[str, list[str]] = {key: [] for key, _, _ in sections}
    for job in jobs:
        lane = str(job.get("role_lane") or role_lane(job))
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
        rows_by_lane[lane].append(
            "<tr><td style='padding:12px;border-bottom:1px solid #e5e7eb'>"
            f"<a href='{html.escape(link, quote=True)}' style='font-weight:600;color:#155eef'>"
            f"{html.escape(title)}</a><br><span style='color:#667085'>{html.escape(company)}</span>"
            f"{rationale_html}</td>"
            f"<td style='padding:12px;border-bottom:1px solid #e5e7eb'>{html.escape(sector)}</td>"
            f"<td style='padding:12px;border-bottom:1px solid #e5e7eb'>{html.escape(location)}</td>"
            f"<td style='padding:12px;border-bottom:1px solid #e5e7eb'>{html.escape(posted)}</td>"
            f"<td style='padding:12px;border-bottom:1px solid #e5e7eb;text-align:center'>{score}/10</td></tr>"
        )
        plain_by_lane[lane].append(
            f"{title} | {company} | {sector} | {score}/10 | {location} | Posted {posted}\nApply: {link}"
            + (f"\nWhy it may fit: {rationale}" if rationale else "")
        )
    test_banner = (
        "<p style='padding:10px;background:#fff4cc;color:#7a4b00'><b>Test digest:</b> current criteria and rankings; sent-role history was left unchanged.</p>"
        if test_mode
        else ""
    )
    html_sections = []
    plain_sections = []
    for lane, heading, description in sections:
        lane_rows = rows_by_lane[lane]
        lane_plain = plain_by_lane[lane]
        html_sections.append(
            f"<h3 style='margin:24px 0 4px;color:#344054'>{html.escape(heading)} ({len(lane_rows)})</h3>"
            f"<p style='margin:0 0 8px;color:#667085'>{html.escape(description)}</p>"
            + (
                "<table style='border-collapse:collapse;width:100%'><thead><tr>"
                + "<th align='left' style='padding:12px'>Role and company</th>"
                + "<th align='left' style='padding:12px'>Industry / category</th>"
                + "<th align='left' style='padding:12px'>Location</th>"
                + "<th align='left' style='padding:12px'>Posted</th>"
                + "<th style='padding:12px'>Fit</th></tr></thead><tbody>"
                + "".join(lane_rows)
                + "</tbody></table>"
                if lane_rows else "<p style='color:#667085'>No matches in this lane today.</p>"
            )
        )
        plain_sections.append(
            f"{heading} ({len(lane_plain)})\n{description}\n\n"
            + ("\n\n".join(lane_plain) if lane_plain else "No matches in this lane today.")
        )
    html_body = (
        "<html><body style='font-family:Arial,sans-serif;color:#101828;max-width:900px;margin:auto'>"
        f"<div style='display:none;max-height:0;overflow:hidden;opacity:0;color:transparent'>{html.escape(preview_text)}</div>"
        f"<h2>{html.escape(first_name)}’s PMM Job Hunt Fairy 🧚</h2>"
        f"<p>{html.escape(date_label)}</p>"
        + test_banner
        + f"<p>{html.escape(encouragement)}</p>"
        + f"<p>Your top {len(jobs)} eligible role(s), sorted by fit score within each lane:</p>"
        + "".join(html_sections)
        + "</body></html>"
    )
    plain = (
        f"{preview_text}\n\n{first_name}'s PMM Job Hunt Fairy — {date_label}\n\n"
        f"{encouragement}\n\nYour top {len(jobs)} eligible role(s), sorted by fit score within each lane:\n\n"
        + "\n\n".join(plain_sections)
    )
    return plain, html_body


def send_email(
    jobs: list[dict[str, Any]], recipient: str, user_name: str, test_mode: bool = False
) -> None:
    sender = os.getenv("GMAIL_ADDRESS")
    password = os.getenv("GMAIL_APP_PASSWORD")
    if not sender or not password:
        raise RuntimeError("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD to send email")
    plain, rich = format_email(jobs, user_name, test_mode)
    message = EmailMessage()
    date_label = datetime.now(ZoneInfo("Europe/Paris")).strftime("%d %b %Y")
    prefix = "🧪 TEST — " if test_mode else ""
    message["Subject"] = f"{prefix}🧚 PMM Job Hunt Fairy | {date_label}"
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
    test_mode = os.getenv("JOB_ALERT_TEST_EMAIL", "").strip().lower() in {"1", "true", "yes"}
    history = load_history()
    jobs = search_adzuna(config)
    # A manual test may resend previously emailed roles to demonstrate updated
    # ranking, but must not alter the daily deduplication history.
    matches = filter_and_score(jobs, config, set() if test_mode else history)
    LOG.info("Fetched %d unique listings; %d unseen listings passed filters", len(jobs), len(matches))
    if not matches and not test_mode:
        LOG.info("No listings passed the configured filters")
        return 0
    per_lane_limit = max(1, int(config.get("search", {}).get("max_email_results_per_lane", 5)))
    lane_counts = {
        "product_marketing": 0,
        "partner_channel_marketing": 0,
        "marketing_management": 0,
        "retail_marketing": 0,
    }
    email_jobs = []
    for job in matches:
        lane = role_lane(job)
        if lane_counts.get(lane, 0) >= per_lane_limit:
            continue
        job["role_lane"] = lane
        email_jobs.append(job)
        lane_counts[lane] = lane_counts.get(lane, 0) + 1
    max_email_results = per_lane_limit * len(lane_counts)
    if len(matches) > len(email_jobs):
        LOG.info(
            "Selected up to %d roles per lane (%d total) from %d matching roles",
            per_lane_limit,
            max_email_results,
            len(matches),
        )
    if os.getenv("OPENAI_API_KEY"):
        cv_text = load_cv()
        for job in email_jobs:
            rationale = get_ai_rationale(job, cv_text)
            if rationale:
                job["rationale"] = rationale
    send_email(email_jobs, recipient, user_name, test_mode)
    if test_mode:
        LOG.info("Sent a test digest with %d matching roles to %s; history unchanged", len(email_jobs), recipient)
    else:
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
