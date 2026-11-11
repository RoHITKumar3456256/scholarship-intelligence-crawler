"""Change detection module: fingerprinting, field diffing, and lifecycle rules."""

import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Fields tracked for change detection across runs
TRACKED_FIELDS = [
    "closing_date",
    "opening_date",
    "amount_text",
    "amount_min",
    "amount_max",
    "income_limit",
    "eligibility_text",
    "status",
    "application_url",
    "official_url",
]


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------


def fingerprint(name: str, provider: Optional[str], official_url: Optional[str]) -> str:
    """Generate a stable SHA-1 identity fingerprint for a scholarship.

    The fingerprint is based on the scholarship name, provider, and official
    URL after normalisation. It is used to match the same scholarship across
    crawl runs regardless of minor text changes.

    Args:
        name: Scholarship name.
        provider: Organisation providing the scholarship (may be ``None``).
        official_url: Primary URL for the scholarship page (may be ``None``).

    Returns:
        A 40-character hex SHA-1 digest.
    """
    parts = [
        (name or "").strip().lower(),
        (provider or "").strip().lower(),
        (official_url or "").strip().lower(),
    ]
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Field diffing
# ---------------------------------------------------------------------------


def diff_fields(
    prev_rec: Dict[str, Any],
    new_rec: Dict[str, Any],
    tracked_fields: List[str],
) -> List[Tuple[str, Any, Any]]:
    """Identify fields that changed between two scholarship records.

    Args:
        prev_rec: Previous scholarship record (dict).
        new_rec: New scholarship record (dict).
        tracked_fields: Field names to compare.

    Returns:
        List of ``(field_name, old_value, new_value)`` tuples for changed fields.
    """
    diffs: List[Tuple[str, Any, Any]] = []
    for field_name in tracked_fields:
        old_val = prev_rec.get(field_name)
        new_val = new_rec.get(field_name)

        # Normalise to strings for comparison
        old_str = str(old_val).strip() if old_val is not None else ""
        new_str = str(new_val).strip() if new_val is not None else ""

        if old_str != new_str:
            diffs.append((field_name, old_val, new_val))

    return diffs


# ---------------------------------------------------------------------------
# Missing-run tracking
# ---------------------------------------------------------------------------


def get_missing_runs(conn: sqlite3.Connection, scholarship_id: int) -> int:
    """Return how many consecutive crawl runs a scholarship has been absent.

    Counts change_log entries where ``change_type = 'MISSING'`` after the
    most recent non-MISSING change_log entry.

    Args:
        conn: SQLite connection.
        scholarship_id: Row ID in ``scholarships`` table.

    Returns:
        Integer count of consecutive missing runs (0 if never marked missing).
    """
    try:
        cur = conn.execute(
            """SELECT COUNT(*) FROM change_log
               WHERE scholarship_id = ?
                 AND change_type = 'MISSING'
                 AND id > COALESCE(
                       (SELECT MAX(id) FROM change_log
                        WHERE scholarship_id = ?
                          AND change_type != 'MISSING'),
                       0)""",
            (scholarship_id, scholarship_id),
        )
        row = cur.fetchone()
        return row[0] if row else 0
    except sqlite3.Error as exc:
        logger.error("get_missing_runs query failed: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Lifecycle rules
# ---------------------------------------------------------------------------


def apply_lifecycle_rules(conn: sqlite3.Connection, now: Optional[datetime] = None) -> None:
    """Transition scholarship statuses based on deadline proximity.

    Rules:
    - ``closing_date`` is in the past → ``EXPIRED``
    - ``closing_date`` is within ``expiring_soon_days`` → ``EXPIRING_SOON``
    - Otherwise leave as ``ACTIVE``

    Args:
        conn: SQLite connection.
        now: Reference datetime (defaults to UTC now).
    """
    if now is None:
        now = datetime.now(timezone.utc)

    today = now.date().isoformat()

    try:
        # Mark expired
        conn.execute(
            """UPDATE scholarships
               SET status = 'EXPIRED'
               WHERE closing_date IS NOT NULL
                 AND closing_date < ?
                 AND status NOT IN ('EXPIRED', 'ARCHIVED')""",
            (today,),
        )

        # Mark expiring soon (within ~15 days)
        from datetime import timedelta

        expiring_threshold = (now.date() + timedelta(days=15)).isoformat()
        conn.execute(
            """UPDATE scholarships
               SET status = 'EXPIRING_SOON'
               WHERE closing_date IS NOT NULL
                 AND closing_date >= ?
                 AND closing_date <= ?
                 AND status NOT IN ('EXPIRED', 'ARCHIVED', 'EXPIRING_SOON')""",
            (today, expiring_threshold),
        )

        conn.commit()
        logger.info("Lifecycle rules applied (reference date: %s)", today)
    except sqlite3.Error as exc:
        logger.error("apply_lifecycle_rules failed: %s", exc)


def mark_scholarship_missing(
    conn: sqlite3.Connection,
    scholarship_id: int,
    run_count: int,
) -> None:
    """Update scholarship status when it has been missing for one or more runs.

    Transitions:
    - Any run_count ≥ 1 → adds a MISSING change_log entry.
    - run_count ≥ ``stale_runs`` threshold → sets status to ``NO_LONGER_VERIFIABLE``.

    Args:
        conn: SQLite connection.
        scholarship_id: Row ID.
        run_count: Consecutive run count the scholarship has been absent.
    """
    stale_threshold = 2  # from default config
    now_str = datetime.now(timezone.utc).isoformat()

    try:
        # Log the missing event
        conn.execute(
            """INSERT INTO change_log
               (scholarship_id, field, old_value, new_value, detected_at, change_type)
               VALUES (?, 'status', 'ACTIVE', 'MISSING', ?, 'MISSING')""",
            (scholarship_id, now_str),
        )

        if run_count >= stale_threshold:
            conn.execute(
                """UPDATE scholarships SET status = 'NO_LONGER_VERIFIABLE'
                   WHERE id = ? AND status NOT IN ('EXPIRED', 'ARCHIVED')""",
                (scholarship_id,),
            )
            logger.info(
                "Scholarship %d marked NO_LONGER_VERIFIABLE after %d missing runs",
                scholarship_id,
                run_count,
            )

        conn.commit()
    except sqlite3.Error as exc:
        logger.error("mark_scholarship_missing failed for id %d: %s", scholarship_id, exc)
