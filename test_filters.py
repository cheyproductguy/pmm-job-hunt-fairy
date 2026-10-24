"""Run with: python -m pytest test_filters.py  (pip install pytest first).

Reads the real config.yaml, so these tests check the file you actually deploy.
"""

from pathlib import Path

import pytest
import yaml

from filters import compile_title_patterns, engineering_degree_requirement, matching_title_keyword

CONFIG = yaml.safe_load((Path(__file__).resolve().parent / "config.yaml").read_text(encoding="utf-8"))
PATTERNS = compile_title_patterns(CONFIG["negative_title_keywords"])

REJECT_TITLES = [
    "Stage – Product Marketing",
    "Alternance Marketing Produit",
    "Product Marketing Intern",
    "Stagiaire marketing produit (H/F)",
    "Apprenti(e) Marketing",
    "Jeune diplômée – Marketing",
    "Head of Product Marketing",
    "Directrice Marketing",
    "Marketing Director",
    "VP Marketing",
    "Ingénieur marketing produit",
    "Account Executive",
    "Product Owner",
]

KEEP_TITLES = [
    "Product Marketing Manager",
    "Senior Product Marketing Manager",
    "Product Marketing Lead",
    "Responsable Marketing Produit H/F",
    "International Marketing Manager",
    "Responsable marketing international",
    "Responsable communication interne",
    "Product Marketing Manager, Early-Stage Startup",
    "Growth Marketing Manager, Product",
]

DEGREE_REJECT = [
    "Ingénieur de formation, vous justifiez d'une expérience en marketing produits, "
    "idéalement acquise dans un environnement industriel ou technologique.",
    "Diplôme d'ingénieur exigé.",
    "Diplôme d\u2019ingénieur exigé.",
    "Vous êtes issu d'une école d'ingénieur et avez 3 ans d'expérience.",
    "Engineering degree required.",
]

DEGREE_KEEP = [
    "Idéalement ingénieur de formation, avec une expérience en marketing produit.",
    "Bac+5 école de commerce ou école d'ingénieur.",
    "Formation d'ingénieur ou équivalent.",
    "Engineering degree is a plus.",
    "Vous collaborez avec les équipes d'ingénieurs et le marketing produit.",
    "You will work closely with engineering teams.",
]


@pytest.mark.parametrize("title", REJECT_TITLES)
def test_title_rejected(title):
    assert matching_title_keyword(title, PATTERNS) is not None, f"should reject: {title}"


@pytest.mark.parametrize("title", KEEP_TITLES)
def test_title_kept(title):
    hit = matching_title_keyword(title, PATTERNS)
    assert hit is None, f"wrongly rejected '{title}' because of '{hit}'"


@pytest.mark.parametrize("text", DEGREE_REJECT)
def test_degree_rejected(text):
    assert engineering_degree_requirement(text, CONFIG) is not None, f"should reject: {text}"


@pytest.mark.parametrize("text", DEGREE_KEEP)
def test_degree_kept(text):
    reason = engineering_degree_requirement(text, CONFIG)
    assert reason is None, f"wrongly rejected '{text}': {reason}"
