"""Tests for change detection logic in src.changes."""

import pytest

from src.changes import diff_fields, fingerprint, TRACKED_FIELDS


# ---------------------------------------------------------------------------
# Fingerprint tests
# ---------------------------------------------------------------------------


def test_fingerprint_stable():
    """Same inputs should always produce the same fingerprint."""
    fp1 = fingerprint("Test Scholarship", "Test Foundation", "https://testfoundation.org")
    fp2 = fingerprint("Test Scholarship", "Test Foundation", "https://testfoundation.org")
    assert fp1 == fp2


def test_fingerprint_different_for_different_data():
    """Different scholarship names should produce different fingerprints."""
    fp1 = fingerprint("Scholarship A", "Provider X", "https://x.org")
    fp2 = fingerprint("Scholarship B", "Provider X", "https://x.org")
    assert fp1 != fp2


def test_fingerprint_is_40_chars():
    """SHA-1 fingerprint should be 40 hex characters."""
    fp = fingerprint("My Scholarship", "My Foundation", "https://myfoundation.org")
    assert len(fp) == 40
    assert all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_case_insensitive():
    """Fingerprint should normalise to lower case, so case variants match."""
    fp1 = fingerprint("Test Scholarship", "Test Foundation", "https://tf.org")
    fp2 = fingerprint("TEST SCHOLARSHIP", "TEST FOUNDATION", "https://tf.org")
    assert fp1 == fp2


def test_fingerprint_none_provider():
    """None provider should not raise an error."""
    fp = fingerprint("Test Scholarship", None, "https://tf.org")
    assert len(fp) == 40


def test_fingerprint_none_url():
    """None official_url should not raise an error."""
    fp = fingerprint("Test Scholarship", "Some Foundation", None)
    assert len(fp) == 40


# ---------------------------------------------------------------------------
# diff_fields tests
# ---------------------------------------------------------------------------


def test_diff_detects_deadline_change():
    """A closing_date change should be detected."""
    prev = {"closing_date": "2025-03-31", "amount_text": "Rs. 50,000"}
    new = {"closing_date": "2025-04-15", "amount_text": "Rs. 50,000"}
    diffs = diff_fields(prev, new, ["closing_date", "amount_text"])
    assert len(diffs) == 1
    assert diffs[0][0] == "closing_date"
    assert diffs[0][1] == "2025-03-31"
    assert diffs[0][2] == "2025-04-15"


def test_diff_no_changes():
    """Identical records should produce no diffs."""
    prev = {"closing_date": "2025-03-31"}
    new = {"closing_date": "2025-03-31"}
    diffs = diff_fields(prev, new, ["closing_date"])
    assert len(diffs) == 0


def test_diff_detects_multiple_changes():
    """Multiple changed fields should all be reported."""
    prev = {"closing_date": "2025-01-01", "amount_text": "Rs. 10,000", "income_limit": 150000}
    new = {"closing_date": "2025-06-30", "amount_text": "Rs. 20,000", "income_limit": 150000}
    diffs = diff_fields(prev, new, ["closing_date", "amount_text", "income_limit"])
    assert len(diffs) == 2
    changed_fields = {d[0] for d in diffs}
    assert "closing_date" in changed_fields
    assert "amount_text" in changed_fields


def test_diff_none_to_value_counts_as_change():
    """Transition from None to a value should be a detected change."""
    prev = {"closing_date": None}
    new = {"closing_date": "2025-12-31"}
    diffs = diff_fields(prev, new, ["closing_date"])
    assert len(diffs) == 1


def test_diff_value_to_none_counts_as_change():
    """Transition from a value to None should be a detected change."""
    prev = {"closing_date": "2025-01-01"}
    new = {"closing_date": None}
    diffs = diff_fields(prev, new, ["closing_date"])
    assert len(diffs) == 1


def test_diff_ignores_untracked_fields():
    """Fields not in tracked_fields list should be ignored."""
    prev = {"closing_date": "2025-01-01", "name": "Old Name"}
    new = {"closing_date": "2025-01-01", "name": "New Name"}
    diffs = diff_fields(prev, new, ["closing_date"])  # only track closing_date
    assert len(diffs) == 0


def test_tracked_fields_list_completeness():
    """TRACKED_FIELDS should include the essential change-sensitive columns."""
    essential = ["closing_date", "amount_text", "eligibility_text", "status"]
    for f in essential:
        assert f in TRACKED_FIELDS, f"Expected {f} in TRACKED_FIELDS"
