"""Tests for the grounding verification function in src.extract."""

import pytest

from src.extract import grounded, norm


def test_grounded_exact_match():
    """An exact quote should be grounded."""
    assert grounded("last date to apply is 31 March 2025", "last date to apply is 31 March 2025")


def test_grounded_normalized_whitespace():
    """Quotes with extra internal whitespace should still ground against normal text."""
    assert grounded("last  date  to apply", "last date to apply is 31 March 2025")


def test_grounded_missing_returns_false():
    """A quote not present in the page text should return False."""
    assert not grounded("deadline is 1 January 2020", "this page has no deadline")


def test_grounded_empty_quote():
    """An empty string quote should return False."""
    assert not grounded("", "some text")


def test_grounded_none_quote():
    """A None quote should return False without raising."""
    assert not grounded(None, "some text")


def test_grounded_case_insensitive():
    """Grounding check should be case-insensitive."""
    assert grounded("LAST DATE TO APPLY", "last date to apply is 31 March 2025")


def test_grounded_partial_match():
    """A sub-string of the page text should be grounded."""
    assert grounded("31 March 2025", "students must apply before 31 March 2025 to be eligible")


def test_norm_collapses_whitespace():
    """norm() should collapse multiple spaces and strip edges."""
    assert norm("  hello   world  ") == "hello world"


def test_norm_newlines():
    """norm() should treat newlines as whitespace."""
    assert norm("hello\n\nworld") == "hello world"


def test_norm_empty():
    """norm() on empty string should return empty string."""
    assert norm("") == ""


def test_norm_none_safe():
    """norm() should handle None input (passed as empty string guard)."""
    assert norm(None) == ""
