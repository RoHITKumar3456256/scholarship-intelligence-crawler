"""Tests for rule-based field extraction in src.extract."""

import pytest

from src.extract import rule_extract


def test_income_limit_missing_returns_none():
    """When no income information is present, income_limit should not be extracted."""
    text = "This scholarship is open to all students. Apply before 31 March 2025."
    result = rule_extract(text)
    assert "income_limit" not in result or result.get("income_limit") is None


def test_closing_date_extracted():
    """A clear 'last date to apply' pattern should yield a closing_date."""
    text = "Last date to apply: 31 March 2025. Students must have 60% marks."
    result = rule_extract(text)
    assert "closing_date" in result
    val, quote = result["closing_date"]
    assert "2025" in str(val)


def test_income_extracted_in_lakhs():
    """Income in lakhs should be converted to the absolute annual integer."""
    text = "Family income should not exceed Rs. 2.5 lakh per annum."
    result = rule_extract(text)
    assert "income_limit" in result
    val, quote = result["income_limit"]
    assert val == 250000


def test_amount_extracted():
    """A scholarship amount in rupees should be captured."""
    text = "The scholarship amount is Rs. 50,000 per annum for meritorious students."
    result = rule_extract(text)
    assert "amount_text" in result or "amount_min" in result


def test_age_range_extracted():
    """Age range should be extracted when present."""
    text = "Applicant age must be between 18 to 25 years to apply."
    result = rule_extract(text)
    assert "age_min" in result
    assert "age_max" in result
    val_min, _ = result["age_min"]
    val_max, _ = result["age_max"]
    assert val_min == 18
    assert val_max == 25


def test_percent_marks_extracted():
    """Academic percentage requirement should be captured."""
    text = "Candidates must have secured at least 75% marks in their last examination."
    result = rule_extract(text)
    assert "academic_req" in result
    val, _ = result["academic_req"]
    assert "75" in str(val)


def test_gender_female_extracted():
    """'female' keyword should produce FEMALE gender field."""
    text = "This scholarship is exclusively for female candidates pursuing STEM."
    result = rule_extract(text)
    assert "gender" in result
    val, _ = result["gender"]
    assert val == "FEMALE"


def test_opening_date_extracted():
    """An opening date pattern should be captured."""
    text = "Opening date for applications: 01 July 2025."
    result = rule_extract(text)
    assert "opening_date" in result
    val, _ = result["opening_date"]
    assert "2025" in str(val)


def test_income_in_lakhs_decimal():
    """Decimal lakh values (e.g. 3.5 lakh) should be correctly converted."""
    text = "Annual family income should not exceed Rs. 3.5 lakhs."
    result = rule_extract(text)
    assert "income_limit" in result
    val, _ = result["income_limit"]
    assert val == 350000


def test_rule_extract_returns_dict():
    """rule_extract should always return a dict (never raise)."""
    result = rule_extract("")
    assert isinstance(result, dict)
