"""Title and degree filters for the PMM job hunter.

Kept separate from main.py so they can be tested without network access.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# Normal English phrases that contain a negative keyword ("early-stage startup").
TITLE_EXCEPTIONS = re.compile(r"(?<![a-z0-9])(?:early|late|growth|seed|mid)[- ]stage(?![a-z0-9])")


def normalize(text: str) -> str:
    """Lowercase, strip accents, and unify apostrophes."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.replace("\u2019", "'").replace("`", "'")


def compile_title_patterns(keywords: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    """Build whole-word patterns (optional plural 's') for each negative keyword."""
    patterns: list[tuple[str, re.Pattern[str]]] = []
    seen: set[str] = set()
    for keyword in keywords:
        key = normalize(str(keyword)).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        patterns.append((key, re.compile(rf"(?<![a-z0-9]){re.escape(key)}s?(?![a-z0-9])")))
    return patterns


def matching_title_keyword(title: str, patterns: list[tuple[str, re.Pattern[str]]]) -> str | None:
    """Return the first negative keyword found in the title as a whole word, else None."""
    text = TITLE_EXCEPTIONS.sub(" ", normalize(title))
    for keyword, pattern in patterns:
        if pattern.search(text):
            return keyword
    return None


def engineering_degree_requirement(description: str, config: dict[str, Any]) -> str | None:
    """Return a reason when the description requires an engineering degree.

    A degree phrase counts as required unless a preferred signal sits right next
    to it, or the posting offers an alternative (e.g. "école de commerce") nearby.
    A preferred word later in the sentence does not cancel it.
    """
    rules = config.get("degree_filters", {})
    if not rules.get("exclude_explicit_engineering_degree_requirement", False):
        return None

    text = normalize(description)
    signals = [normalize(item) for item in rules.get("engineering_degree_signals", [])]
    preferred_source = rules.get("preferred_signals") or config.get("seniority_filters", {}).get(
        "preferred_signals", []
    )
    preferred = [normalize(item) for item in preferred_source]
    alternatives = [normalize(item) for item in rules.get("alternative_signals", [])]
    required = [
        normalize(item)
        for item in config.get("seniority_filters", {}).get("pure_b2b_requirement_signals", [])
    ]
    before = int(rules.get("preferred_window_chars_before", 30))
    after = int(rules.get("preferred_window_chars_after", 15))
    alt_window = int(rules.get("alternative_window_chars", 60))
    default_is_requirement = bool(rules.get("default_is_requirement", True))

    for signal in signals:
        if not signal:
            continue
        start = 0
        while (index := text.find(signal, start)) != -1:
            end = index + len(signal)
            start = end
            near = text[max(0, index - before) : index] + " " + text[end : end + after]
            if any(item in near for item in preferred):
                continue
            wide = text[max(0, index - alt_window) : end + alt_window]
            if any(item in wide for item in alternatives):
                continue
            if not default_is_requirement and not any(item in wide for item in required):
                continue
            return f"engineering degree requirement ('{signal}')"
    return None
