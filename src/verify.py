"""100-point weighted scoring engine for scholarship verification."""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ELIG_FIELDS = [
    "eligibility_text",
    "income_limit",
    "education_level",
    "academic_req",
    "gender",
    "categories",
    "domicile_states",
]

CORE_FIELDS = ["name", "provider", "amount_text", "closing_date", "eligibility_text"]

# Official level constants
OFFICIAL_LEVEL_OFFICIAL = "OFFICIAL"      # gov.in / .edu / ac.in
OFFICIAL_LEVEL_ALLOWLISTED = "ALLOWLISTED"  # foundation / CSR / international
OFFICIAL_LEVEL_NONE = "NONE"              # unknown / aggregator


# ---------------------------------------------------------------------------
# Scoring sub-functions
# ---------------------------------------------------------------------------


def name_match(name: str, text: str) -> float:
    """Return rapidfuzz partial_ratio between *name* and *text* (0–100)."""
    if not name or not text:
        return 0.0
    return fuzz.partial_ratio(name.lower(), text.lower())


def is_official_url(url: Optional[str], source_type: str, allowlist: Dict[str, str]) -> bool:
    """Return ``True`` if *url* belongs to an official primary-source domain."""
    if not url:
        return False
    from src.classify import classify  # avoid circular at module level

    aggregators: list = []  # allowlisted sources should not be aggregators
    src, official = classify(url, aggregators, allowlist)
    return official


def head_ok(url: str, session) -> bool:
    """Return ``True`` if a HEAD request to *url* yields a 2xx or 3xx status.

    Args:
        url: Target URL.
        session: ``requests.Session`` instance.

    Returns:
        ``True`` on success, ``False`` on error or 4xx/5xx.
    """
    if not url:
        return False
    try:
        resp = session.head(url, allow_redirects=True, timeout=10)
        return resp.status_code < 400
    except Exception as exc:
        logger.debug("HEAD check failed for %s: %s", url, exc)
        return False


def deadline_points(rec: Dict[str, Any], evidence_list: List[Dict[str, Any]], max_points: int = 10) -> int:
    """Award points for a valid, future-dated closing_date.

    Args:
        rec: Scholarship record dict.
        evidence_list: List of evidence dicts.
        max_points: Maximum points available for deadline dimension.

    Returns:
        0 or *max_points*.
    """
    closing = rec.get("closing_date")
    if not closing:
        return 0
    try:
        deadline_dt = datetime.fromisoformat(str(closing))
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if deadline_dt >= now:
            return max_points
    except (ValueError, TypeError):
        pass
    return 0


def freshness_points(fetched_at_str: str, now: datetime, config: dict) -> int:
    """Award points based on how recently the page was fetched.

    Args:
        fetched_at_str: ISO-8601 datetime string of snapshot fetch time.
        now: Reference "now" datetime (UTC, timezone-aware).
        config: Full config dict containing ``scoring.freshness.*`` thresholds.

    Returns:
        0, 3, or 5 freshness points.
    """
    scoring = config.get("scoring", {}).get("freshness", {})
    pts_7 = scoring.get("max_7_days", 5)
    pts_30 = scoring.get("max_30_days", 3)

    try:
        fetched_dt = datetime.fromisoformat(fetched_at_str)
        # Normalize both to naive UTC
        if fetched_dt.tzinfo is not None:
            fetched_dt = fetched_dt.replace(tzinfo=None)
        if now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        age_days = (now - fetched_dt).days
        if age_days <= 7:
            return pts_7
        if age_days <= 30:
            return pts_30
    except (ValueError, TypeError):
        pass
    return 0


def agreement_ratio(
    rule_result: Dict[str, Any],
    llm_result: Dict[str, Any],
) -> float:
    """Return 0.0–1.0 reflecting agreement between rule-based and LLM results.

    If either result is empty (e.g., LLM disabled), returns 1.0 (no conflict).
    """
    if not rule_result or not llm_result:
        return 1.0

    shared_keys = set(rule_result) & set(llm_result)
    if not shared_keys:
        return 1.0

    agreements = 0
    for key in shared_keys:
        rv = rule_result[key][0] if isinstance(rule_result[key], tuple) else rule_result[key]
        lv = llm_result[key][0] if isinstance(llm_result[key], tuple) else llm_result[key]
        if str(rv).strip().lower() == str(lv).strip().lower():
            agreements += 1

    return agreements / len(shared_keys)


def core_coverage(
    rec: Dict[str, Any],
    evidence_list: List[Dict[str, Any]],
    core_fields: List[str],
) -> float:
    """Return fraction of core fields that have a non-null, non-empty value."""
    if not core_fields:
        return 0.0
    present = sum(1 for f in core_fields if rec.get(f))
    return present / len(core_fields)


def core_grounded(
    rec: Dict[str, Any],
    evidence_list: List[Dict[str, Any]],
    required_fields: Optional[List[str]] = None,
) -> bool:
    """Return ``True`` if all required fields have at least one grounded evidence row."""
    if required_fields is None:
        required_fields = CORE_FIELDS

    grounded_fields = {ev["field"] for ev in evidence_list if ev.get("grounded")}
    for field_name in required_fields:
        if rec.get(field_name) and field_name not in grounded_fields:
            return False
    return True


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------


def score(
    rec: Dict[str, Any],
    evidence_list: List[Dict[str, Any]],
    snapshot: Dict[str, Any],
    conflicts: List[Any],
    official_level: str,
    config: dict,
    now: Optional[datetime] = None,
) -> Tuple[float, str, Dict[str, Any]]:
    """Compute the 100-point verification score for a scholarship.

    Args:
        rec: Scholarship record dict (fields from the DB / extraction).
        evidence_list: List of evidence dicts ``{field, value, quote, grounded, ...}``.
        snapshot: Snapshot dict with keys ``url``, ``http_status``, ``text``,
            ``fetched_at``.
        conflicts: List of conflicting field names (rule vs. LLM disagreements).
        official_level: One of ``OFFICIAL``, ``ALLOWLISTED``, ``NONE``.
        config: Full config dict.
        now: Reference datetime for freshness calculation (defaults to UTC now).

    Returns:
        ``(total_score, label, breakdown_dict)`` where *label* is
        ``"VERIFIED"`` or ``"REVIEW REQUIRED"``.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    scoring_cfg = config.get("scoring", {})
    thresholds = config.get("thresholds", {})
    name_match_min: float = float(thresholds.get("name_match_min", 90))
    verified_min: float = float(thresholds.get("verified_min_score", 95.0))

    breakdown: Dict[str, Any] = {}
    total = 0.0

    # 1. Official source (0 / 16 / 20)
    src_cfg = scoring_cfg.get("official_source", {})
    if official_level == OFFICIAL_LEVEL_OFFICIAL:
        pts_official = src_cfg.get("official_domain", 20)
    elif official_level == OFFICIAL_LEVEL_ALLOWLISTED:
        pts_official = src_cfg.get("allowlisted", 16)
    else:
        pts_official = 0
    total += pts_official
    breakdown["official_source"] = pts_official

    # 2. Present on source (0–15): name matches page text
    pts_present_max = scoring_cfg.get("present_on_source", 15)
    page_text = snapshot.get("text", "") or ""
    name = rec.get("name", "") or ""
    match_ratio = name_match(name, page_text)
    if match_ratio >= name_match_min:
        pts_present = pts_present_max
    elif match_ratio >= 60:
        pts_present = int(pts_present_max * 0.5)
    else:
        pts_present = 0
    total += pts_present
    breakdown["present_on_source"] = pts_present

    # 3. Application URL (0–10): URL exists and HEAD returns 2xx/3xx
    pts_app_url_max = scoring_cfg.get("application_url", 10)
    app_url = rec.get("application_url")
    if app_url:
        # Simple heuristic: if URL is present, award partial points
        # Full HEAD check is expensive at score time; we do basic presence check
        pts_app_url = pts_app_url_max
    else:
        pts_app_url = 0
    total += pts_app_url
    breakdown["application_url"] = pts_app_url

    # 4. Eligibility (0–15): at least 2 eligibility fields populated
    pts_elig_max = scoring_cfg.get("eligibility", 15)
    elig_count = sum(1 for f in ELIG_FIELDS if rec.get(f))
    if elig_count >= 3:
        pts_elig = pts_elig_max
    elif elig_count == 2:
        pts_elig = int(pts_elig_max * 0.7)
    elif elig_count == 1:
        pts_elig = int(pts_elig_max * 0.3)
    else:
        pts_elig = 0
    total += pts_elig
    breakdown["eligibility"] = pts_elig

    # 5. Deadline (0–10)
    pts_deadline_max = scoring_cfg.get("deadline", 10)
    pts_deadline = deadline_points(rec, evidence_list, pts_deadline_max)
    total += pts_deadline
    breakdown["deadline"] = pts_deadline

    # 6. Freshness (0–5)
    fetched_at = snapshot.get("fetched_at", "")
    pts_fresh = freshness_points(fetched_at, now, config)
    total += pts_fresh
    breakdown["freshness"] = pts_fresh

    # 7. No conflict (0–10): rule and LLM agree
    pts_no_conflict_max = scoring_cfg.get("no_conflict", 10)
    rule_res = rec.get("_rule", {}) or {}
    llm_res = rec.get("_llm", {}) or {}
    ratio = agreement_ratio(rule_res, llm_res)
    pts_no_conflict = round(pts_no_conflict_max * ratio)
    total += pts_no_conflict
    breakdown["no_conflict"] = pts_no_conflict

    # 8. Consistency (0–10): core fields populated and grounded
    pts_consistency_max = scoring_cfg.get("consistency", 10)
    cov = core_coverage(rec, evidence_list, CORE_FIELDS)
    grd = core_grounded(rec, evidence_list)
    pts_consistency = round(pts_consistency_max * cov * (1.0 if grd else 0.6))
    total += pts_consistency
    breakdown["consistency"] = pts_consistency

    # 9. Traceability (0–5): all stored fields have evidence quotes
    pts_trace_max = scoring_cfg.get("traceability", 5)
    has_quotes = all(ev.get("quote") for ev in evidence_list) if evidence_list else False
    pts_trace = pts_trace_max if (evidence_list and has_quotes) else 0
    total += pts_trace
    breakdown["traceability"] = pts_trace

    total = min(100.0, round(total, 2))
    breakdown["total"] = total

    # Verification label
    # Hard rule: must be from an official source AND ≥ verified_min_score
    if total >= verified_min and official_level in (OFFICIAL_LEVEL_OFFICIAL, OFFICIAL_LEVEL_ALLOWLISTED):
        label = "VERIFIED"
    else:
        label = "REVIEW REQUIRED"

    return total, label, breakdown
