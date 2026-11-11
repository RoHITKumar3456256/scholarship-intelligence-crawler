"""Field extraction module: regex-based rules + optional Ollama LLM, with grounding checks."""

import json
import logging
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import dateparser

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

DATE_RX = re.compile(
    r"(?:last\s+date|closing\s+date|deadline|apply\s+(?:on\s+or\s+)?before|apply\s+by|last\s+date\s+to\s+apply)"
    r"[^.\n]{0,80}?"
    r"(\d{1,2}[\s\-/.](?:\w+|\d{1,2})[\s\-/.]\d{2,4})",
    re.I,
)

OPEN_DATE_RX = re.compile(
    r"(?:opening\s+date|start(?:ing)?\s+date|applications?\s+open(?:s)?)[^.\n]{0,80}?"
    r"(\d{1,2}[\s\-/.](?:\w+|\d{1,2})[\s\-/.]\d{2,4})",
    re.I,
)

INCOME_RX = re.compile(
    r"(?:family|annual|parental|household)\s+income[^.\n]{0,80}?"
    r"(?:Rs\.?|INR|₹)\s*([\d,\.]+)\s*(lakh|lac|lakhs)?",
    re.I,
)

AMOUNT_RX = re.compile(
    r"(?:scholarship|stipend|award|grant|amount|per\s+annum|per\s+month|p\.a\.|p\.m\.)[^.\n]{0,80}?"
    r"(?:Rs\.?|INR|₹)\s*([\d,\.]+(?:\s*,\s*\d+)*)",
    re.I,
)

AGE_RX = re.compile(r"(?:age)[^.\n]{0,40}?(\d{1,2})\s*(?:to|-)\s*(\d{1,2})\s*years?", re.I)

PERCENT_RX = re.compile(r"(\d{2,3}(?:\.\d+)?)\s*%\s*(?:marks?|aggregate|score|cgpa)?", re.I)

GENDER_RX = re.compile(
    r"\b(female|girl|women|woman|male|boy|men|man|transgender|any\s+gender|all\s+genders?)\b",
    re.I,
)

BENEFIT_TYPE_RX = re.compile(
    r"\b(fellowship|stipend|fee\s+waiver|grant|scholarship)\b",
    re.I,
)

# ---------------------------------------------------------------------------
# LLM prompt schema
# ---------------------------------------------------------------------------

SCHEMA_PROMPT = """Extract scholarship information from the given text. Return ONLY valid JSON.
Rules:
1. Use ONLY information explicitly stated in the text
2. If a field is not stated, return null
3. For every non-null field, include the exact supporting sentence as 'quote'
4. Do not infer, assume, or guess any values

JSON schema:
{
  "name": {"value": "string or null", "quote": "string or null"},
  "provider": {"value": "string or null", "quote": "string or null"},
  "amount_text": {"value": "string or null", "quote": "string or null"},
  "amount_min": {"value": "integer or null", "quote": "string or null"},
  "amount_max": {"value": "integer or null", "quote": "string or null"},
  "benefit_type": {"value": "SCHOLARSHIP|FELLOWSHIP|STIPEND|GRANT|FEE_WAIVER|OTHER or null", "quote": null},
  "eligibility_text": {"value": "string or null", "quote": "string or null"},
  "education_level": {"value": "string or null", "quote": "string or null"},
  "income_limit": {"value": "integer or null", "quote": "string or null"},
  "closing_date": {"value": "YYYY-MM-DD or null", "quote": "string or null"},
  "opening_date": {"value": "YYYY-MM-DD or null", "quote": "string or null"},
  "gender": {"value": "ALL|FEMALE|MALE or null", "quote": "string or null"},
  "categories": {"value": "string or null", "quote": "string or null"},
  "domicile_states": {"value": "string or null", "quote": "string or null"},
  "academic_req": {"value": "string or null", "quote": "string or null"},
  "documents_required": {"value": "string or null", "quote": "string or null"},
  "selection_process": {"value": "string or null", "quote": "string or null"},
  "application_url": {"value": "string or null", "quote": null}
}

TEXT:
"""

# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def norm(s: str) -> str:
    """Normalize whitespace in *s*: collapse runs, strip edges."""
    return re.sub(r"\s+", " ", s or "").strip()


def grounded(quote: Optional[str], page_text: str) -> bool:
    """Return ``True`` if *quote* (after normalization) appears in *page_text*.

    Args:
        quote: The quote string to look up. ``None`` or empty → ``False``.
        page_text: The full page text to search in.

    Returns:
        ``True`` iff the normalized quote is a substring of the normalized page text.
    """
    if not quote:
        return False
    return norm(quote).lower() in norm(page_text).lower()


def _parse_date(raw: str) -> Optional[str]:
    """Parse a raw date string using dateparser; return ISO date or ``None``."""
    try:
        dt = dateparser.parse(raw, settings={"PREFER_DAY_OF_MONTH": "first", "RETURN_AS_TIMEZONE_AWARE": False})
        if dt:
            return dt.date().isoformat()
    except Exception:
        pass
    return None


def _parse_amount(raw: str) -> Optional[int]:
    """Convert a raw amount string (possibly with commas) to an integer."""
    cleaned = raw.replace(",", "").replace(" ", "").strip()
    try:
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


def _income_to_annual(value_str: str, unit: Optional[str]) -> Optional[int]:
    """Convert income value + optional unit (lakh/lac) to absolute integer."""
    cleaned = value_str.replace(",", "").strip()
    try:
        val = float(cleaned)
    except ValueError:
        return None
    if unit and unit.lower().startswith("lakh") or unit and unit.lower().startswith("lac"):
        val = val * 100_000
    return int(val)


# ---------------------------------------------------------------------------
# Rule-based extraction
# ---------------------------------------------------------------------------


def rule_extract(text: str) -> Dict[str, Tuple[Any, str]]:
    """Extract scholarship fields from *text* using regex and dateparser.

    Returns a dict mapping field name → ``(value, evidence_quote)``.
    Only fields where a value was found are included; missing fields are absent
    (not null) so callers can distinguish "not found" from "explicitly null".

    Args:
        text: Cleaned page text.

    Returns:
        Dict of ``{field: (value, quote)}``.
    """
    result: Dict[str, Tuple[Any, str]] = {}

    # --- Closing date ---
    for m in DATE_RX.finditer(text):
        raw_date = m.group(1)
        iso = _parse_date(raw_date)
        if iso:
            # Quote: the full matched string from the surrounding context
            start = max(0, m.start() - 10)
            end = min(len(text), m.end() + 10)
            quote = norm(text[start:end])
            result["closing_date"] = (iso, quote)
            break

    # --- Opening date ---
    for m in OPEN_DATE_RX.finditer(text):
        raw_date = m.group(1)
        iso = _parse_date(raw_date)
        if iso:
            start = max(0, m.start() - 10)
            end = min(len(text), m.end() + 10)
            quote = norm(text[start:end])
            result["opening_date"] = (iso, quote)
            break

    # --- Income limit ---
    for m in INCOME_RX.finditer(text):
        val_str = m.group(1)
        unit = m.group(2)
        income = _income_to_annual(val_str, unit)
        if income is not None:
            start = max(0, m.start() - 5)
            end = min(len(text), m.end() + 5)
            quote = norm(text[start:end])
            result["income_limit"] = (income, quote)
            break

    # --- Amount ---
    for m in AMOUNT_RX.finditer(text):
        raw_amount = m.group(1)
        amt = _parse_amount(raw_amount)
        if amt is not None and amt > 0:
            start = max(0, m.start() - 5)
            end = min(len(text), m.end() + 20)
            quote = norm(text[start:end])
            result["amount_text"] = (f"Rs. {raw_amount}", quote)
            result.setdefault("amount_min", (amt, quote))
            break

    # --- Age range ---
    for m in AGE_RX.finditer(text):
        age_min = int(m.group(1))
        age_max = int(m.group(2))
        start = max(0, m.start() - 5)
        end = min(len(text), m.end() + 5)
        quote = norm(text[start:end])
        result["age_min"] = (age_min, quote)
        result["age_max"] = (age_max, quote)
        break

    # --- Academic percentage ---
    for m in PERCENT_RX.finditer(text):
        pct = m.group(1)
        start = max(0, m.start() - 10)
        end = min(len(text), m.end() + 20)
        quote = norm(text[start:end])
        result["academic_req"] = (f"{pct}% marks", quote)
        break

    # --- Gender ---
    for m in GENDER_RX.finditer(text):
        raw = m.group(1).lower()
        if any(w in raw for w in ("female", "girl", "women", "woman")):
            gender = "FEMALE"
        elif any(w in raw for w in ("male", "boy", "men", "man")):
            gender = "MALE"
        else:
            gender = "ALL"
        start = max(0, m.start() - 10)
        end = min(len(text), m.end() + 20)
        quote = norm(text[start:end])
        result["gender"] = (gender, quote)
        break

    # --- Benefit type ---
    for m in BENEFIT_TYPE_RX.finditer(text):
        raw = m.group(1).lower().replace(" ", "_")
        mapping = {
            "fellowship": "FELLOWSHIP",
            "stipend": "STIPEND",
            "fee_waiver": "FEE_WAIVER",
            "grant": "GRANT",
            "scholarship": "SCHOLARSHIP",
        }
        benefit = mapping.get(raw, "OTHER")
        result.setdefault("benefit_type", (benefit, m.group(0)))
        break

    return result


# ---------------------------------------------------------------------------
# LLM extraction (Ollama)
# ---------------------------------------------------------------------------


def llm_extract(text: str, model: str, base_url: str, temperature: float = 0) -> Dict[str, Tuple[Any, Optional[str]]]:
    """Call Ollama to extract scholarship fields from *text*.

    Args:
        text: Cleaned page text (should be pre-truncated to ``max_text_chars``).
        model: Ollama model name (e.g. ``"llama3.1:8b"``).
        base_url: Ollama API base URL (e.g. ``"http://localhost:11434"``).
        temperature: LLM temperature (0 = deterministic).

    Returns:
        Dict of ``{field: (value, quote)}``.  Empty dict on failure.
    """
    try:
        import requests as _req

        prompt = SCHEMA_PROMPT + text
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        resp = _req.post(f"{base_url}/api/generate", json=payload, timeout=120)
        resp.raise_for_status()
        raw_json = resp.json().get("response", "")
    except Exception as exc:
        logger.warning("LLM call failed: %s", exc)
        return {}

    # Parse JSON from LLM response (may have surrounding text)
    try:
        # Find first '{' and last '}'
        start = raw_json.find("{")
        end = raw_json.rfind("}") + 1
        if start == -1 or end == 0:
            raise ValueError("No JSON found in LLM response")
        parsed = json.loads(raw_json[start:end])
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to parse LLM JSON: %s\nRaw: %s", exc, raw_json[:200])
        return {}

    result: Dict[str, Tuple[Any, Optional[str]]] = {}
    for field_name, obj in parsed.items():
        if not isinstance(obj, dict):
            continue
        value = obj.get("value")
        quote = obj.get("quote")
        if value is not None:
            result[field_name] = (value, quote)

    return result


# ---------------------------------------------------------------------------
# Merge rule + LLM results with grounding
# ---------------------------------------------------------------------------


def merge(
    rule_result: Dict[str, Tuple[Any, Optional[str]]],
    llm_result: Dict[str, Tuple[Any, Optional[str]]],
    page_text: str,
) -> Dict[str, Tuple[Any, Optional[str]]]:
    """Merge rule-based and LLM results, keeping only grounded values.

    Rule-based results take precedence when both are present. Any value whose
    quote cannot be grounded in *page_text* is **dropped** (anti-hallucination
    guard).

    Args:
        rule_result: Output of :func:`rule_extract`.
        llm_result: Output of :func:`llm_extract` (may be empty).
        page_text: Original page text used as the grounding corpus.

    Returns:
        Merged dict of ``{field: (value, quote)}`` with only grounded entries.
    """
    merged: Dict[str, Tuple[Any, Optional[str]]] = {}

    all_fields = set(rule_result) | set(llm_result)
    for field_name in all_fields:
        # Prefer rule-based
        if field_name in rule_result:
            val, quote = rule_result[field_name]
        else:
            val, quote = llm_result[field_name]

        # Grounding check – skip ungrounded values
        if quote and not grounded(quote, page_text):
            # Try the LLM alternative if rule was used and ungrounded
            if field_name in llm_result:
                val, quote = llm_result[field_name]
                if quote and not grounded(quote, page_text):
                    logger.debug("Dropping ungrounded field %r (both rule and LLM)", field_name)
                    continue
            else:
                logger.debug("Dropping ungrounded field %r", field_name)
                continue

        merged[field_name] = (val, quote)

    return merged


# ---------------------------------------------------------------------------
# Name / provider extraction
# ---------------------------------------------------------------------------


def extract_name_provider(text: str, url: str) -> Tuple[Optional[str], Optional[str]]:
    """Heuristically extract the scholarship name and provider.

    Tries (in order):
    1. First ``<h1>`` / ``<h2>`` that contains 'scholarship' or 'fellowship'.
    2. Title-pattern lines from plain text.
    3. URL path segments.

    Args:
        text: Cleaned page text.
        url: Source URL (fallback).

    Returns:
        ``(name, provider)`` – either may be ``None``.
    """
    name: Optional[str] = None
    provider: Optional[str] = None

    # Try to find heading-like lines
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # Generic portal header titles that shouldn't be picked as scholarship names
    _GENERIC_HEADERS = (
        "vani - virtual assist", "national scholarship portal", "home", "portal",
        "welcome to", "helpdesk", "disclaimer", "privacy policy", "terms and conditions",
        "hyperlink policy", "copyright policy", "officer announcements", "institute announcements",
    )

    for line in lines[:30]:
        low = line.lower()
        if any(w in low for w in ("scholarship", "fellowship", "stipend", "award")):
            if 5 < len(line) < 200:
                if not any(g in low for g in _GENERIC_HEADERS):
                    name = line
                    break

    # If still not found, try any line matching scheme names
    if not name:
        for line in lines[:50]:
            low = line.lower()
            if any(w in low for w in ("scheme", "guidelines", "financial assistance", "top class")):
                if 8 < len(line) < 180 and not any(g in low for g in _GENERIC_HEADERS):
                    name = line
                    break

    # If still not found, use first heading that matched
    if not name:
        for line in lines[:20]:
            if 5 < len(line) < 120 and not any(g in line.lower() for g in _GENERIC_HEADERS):
                name = line
                break

    # Provider: look for 'by <org>' or 'offered by <org>' patterns
    provider_rx = re.compile(
        r"(?:offered|provided|given|awarded|instituted|sponsored)\s+by\s+([A-Z][^.]{3,80})",
        re.I,
    )
    m = provider_rx.search(text[:3000])
    if m:
        provider = norm(m.group(1))

    # Fallback provider from URL domain
    if not provider:
        parsed = urlparse(url)
        parts = parsed.netloc.split(".")
        # e.g. www.tatacapital.com → tatacapital
        if len(parts) >= 2:
            provider = parts[-2].replace("-", " ").title()

    return name, provider
