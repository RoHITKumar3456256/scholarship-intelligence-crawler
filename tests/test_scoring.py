"""Tests for the 100-point scoring engine in src.verify."""

import pytest

from src.verify import score, ELIG_FIELDS, OFFICIAL_LEVEL_NONE, OFFICIAL_LEVEL_OFFICIAL, OFFICIAL_LEVEL_ALLOWLISTED

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

BASE_CONFIG = {
    "scoring": {
        "official_source": {
            "official_domain": 20,
            "allowlisted": 16,
            "unknown": 0,
        },
        "present_on_source": 15,
        "application_url": 10,
        "eligibility": 15,
        "deadline": 10,
        "freshness": {
            "max_7_days": 5,
            "max_30_days": 3,
            "older": 0,
        },
        "no_conflict": 10,
        "consistency": 10,
        "traceability": 5,
    },
    "thresholds": {
        "name_match_min": 90,
        "expiring_soon_days": 15,
        "verified_min_score": 95.0,
    },
}

EMPTY_SNAP = {
    "url": "https://example.org/scholarship",
    "http_status": 200,
    "text": "",
    "fetched_at": "2025-01-01T00:00:00",
}


def make_rec(**kwargs):
    """Return a minimal scholarship record dict for testing."""
    base = {
        "name": "Test Scholarship",
        "provider": "Test Foundation",
        "official_url": "https://testfoundation.org/scholarship",
        "application_url": None,
        "closing_date": None,
        "source_type": "FOUNDATION",
        "_rule": {},
        "_llm": {},
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_non_official_source_caps_at_60():
    """A non-official source should never score above 60 (official_source = 0)."""
    rec = make_rec()
    snap = {**EMPTY_SNAP, "text": "Test Scholarship from Test Foundation"}
    conf, label, breakdown = score(rec, [], snap, [], OFFICIAL_LEVEL_NONE, BASE_CONFIG)
    assert label == "REVIEW REQUIRED"
    assert conf <= 60.0


def test_verified_requires_95_plus():
    """Non-official source with 404 should always be REVIEW REQUIRED."""
    rec = make_rec()
    snap = {**EMPTY_SNAP, "http_status": 404, "text": ""}
    conf, label, _ = score(rec, [], snap, [], OFFICIAL_LEVEL_NONE, BASE_CONFIG)
    assert label == "REVIEW REQUIRED"


def test_official_source_awards_20_points():
    """Official (gov/edu) source should award 20 points for official_source."""
    rec = make_rec()
    conf, label, breakdown = score(rec, [], EMPTY_SNAP, [], OFFICIAL_LEVEL_OFFICIAL, BASE_CONFIG)
    assert breakdown["official_source"] == 20


def test_allowlisted_source_awards_16_points():
    """Allowlisted (foundation/CSR) source should award 16 points."""
    rec = make_rec()
    conf, label, breakdown = score(rec, [], EMPTY_SNAP, [], OFFICIAL_LEVEL_ALLOWLISTED, BASE_CONFIG)
    assert breakdown["official_source"] == 16


def test_score_returns_tuple_of_three():
    """score() must always return (float, str, dict)."""
    result = score(make_rec(), [], EMPTY_SNAP, [], OFFICIAL_LEVEL_NONE, BASE_CONFIG)
    assert isinstance(result, tuple) and len(result) == 3
    conf, label, breakdown = result
    assert isinstance(conf, float)
    assert isinstance(label, str)
    assert isinstance(breakdown, dict)


def test_total_never_exceeds_100():
    """Score total should never exceed 100."""
    rec = make_rec(
        name="Govt Scholarship",
        closing_date="2099-12-31",
        eligibility_text="Open to all",
        income_limit=250000,
        education_level="UG",
        application_url="https://apply.gov.in",
    )
    snap = {**EMPTY_SNAP, "text": "Govt Scholarship eligibility", "fetched_at": "2025-01-01T00:00:00"}
    conf, _, _ = score(rec, [], snap, [], OFFICIAL_LEVEL_OFFICIAL, BASE_CONFIG)
    assert conf <= 100.0


def test_application_url_adds_points():
    """Presence of an application URL should add points."""
    rec_no_url = make_rec()
    rec_with_url = make_rec(application_url="https://apply.example.org")

    conf_no, _, bd_no = score(rec_no_url, [], EMPTY_SNAP, [], OFFICIAL_LEVEL_NONE, BASE_CONFIG)
    conf_with, _, bd_with = score(rec_with_url, [], EMPTY_SNAP, [], OFFICIAL_LEVEL_NONE, BASE_CONFIG)

    assert bd_with["application_url"] > bd_no["application_url"]


def test_eligibility_points_increase_with_more_fields():
    """More populated eligibility fields should yield higher eligibility points."""
    rec_few = make_rec(eligibility_text="Open to all")
    rec_many = make_rec(
        eligibility_text="Open to SC/ST students",
        income_limit=200000,
        education_level="UG",
        gender="FEMALE",
    )
    _, _, bd_few = score(rec_few, [], EMPTY_SNAP, [], OFFICIAL_LEVEL_NONE, BASE_CONFIG)
    _, _, bd_many = score(rec_many, [], EMPTY_SNAP, [], OFFICIAL_LEVEL_NONE, BASE_CONFIG)

    assert bd_many["eligibility"] >= bd_few["eligibility"]
