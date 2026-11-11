"""SQLite database helper module: schema init, CRUD, and run management."""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Resolve schema path relative to this file's location
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")


# ---------------------------------------------------------------------------
# Connection & initialisation
# ---------------------------------------------------------------------------


def get_connection(db_path: str) -> sqlite3.Connection:
    """Return a SQLite connection with row_factory and WAL mode enabled.

    Args:
        db_path: Absolute or relative path to the SQLite database file.
                 Parent directories are created automatically.

    Returns:
        A :class:`sqlite3.Connection` instance.
    """
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL journal for better concurrent read performance
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str) -> sqlite3.Connection:
    """Initialise the database schema (idempotent) and return the connection.

    Reads ``db/schema.sql`` and executes all CREATE TABLE IF NOT EXISTS
    statements so it is safe to call on an existing database.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        An open :class:`sqlite3.Connection`.
    """
    conn = get_connection(db_path)
    schema_path = os.path.normpath(_SCHEMA_PATH)
    try:
        with open(schema_path, "r", encoding="utf-8") as fh:
            schema_sql = fh.read()
        conn.executescript(schema_sql)
        conn.commit()
        logger.info("Database initialised at %s", db_path)
    except FileNotFoundError:
        logger.error("Schema file not found: %s", schema_path)
        raise
    except sqlite3.Error as exc:
        logger.error("Schema execution failed: %s", exc)
        raise
    return conn


# ---------------------------------------------------------------------------
# Run management
# ---------------------------------------------------------------------------


def start_run(conn: sqlite3.Connection) -> int:
    """Insert a new crawl_run record and return its ID.

    Args:
        conn: SQLite connection.

    Returns:
        Integer run ID.
    """
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO crawl_runs (started_at) VALUES (?)",
        (now,),
    )
    conn.commit()
    run_id = cur.lastrowid
    logger.info("Crawl run #%d started at %s", run_id, now)
    return run_id


def finish_run(conn: sqlite3.Connection, run_id: int, stats: Dict[str, int]) -> None:
    """Update a crawl_run record with completion time and statistics.

    Args:
        conn: SQLite connection.
        run_id: Run ID returned by :func:`start_run`.
        stats: Dict with keys ``discovered``, ``new``, ``changed``, ``expired``,
               and optionally ``notes``.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE crawl_runs
           SET finished_at = ?, discovered = ?, new = ?, changed = ?, expired = ?, notes = ?
           WHERE id = ?""",
        (
            now,
            stats.get("discovered", 0),
            stats.get("new", 0),
            stats.get("changed", 0),
            stats.get("expired", 0),
            stats.get("notes", ""),
            run_id,
        ),
    )
    conn.commit()
    logger.info("Crawl run #%d finished: %s", run_id, stats)


# ---------------------------------------------------------------------------
# Snapshot management
# ---------------------------------------------------------------------------


def save_snapshot(
    conn: sqlite3.Connection,
    url: str,
    http_status: int,
    content_hash: str,
    text_path: str,
    run_id: int,
) -> int:
    """Insert a snapshot record and return its ID.

    Args:
        conn: SQLite connection.
        url: Fetched URL.
        http_status: HTTP response status code.
        content_hash: SHA-256 hex digest of the page text.
        text_path: File path where the snapshot text was saved.
        run_id: Current crawl run ID.

    Returns:
        Integer snapshot ID.
    """
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO snapshots (url, fetched_at, http_status, content_hash, text_path, run_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (url, now, http_status, content_hash, text_path, run_id),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Scholarship CRUD
# ---------------------------------------------------------------------------


def get_by_fingerprint(conn: sqlite3.Connection, fp: str) -> Optional[Dict[str, Any]]:
    """Return a scholarship record dict by fingerprint, or ``None`` if not found.

    Args:
        conn: SQLite connection.
        fp: SHA-1 fingerprint string.

    Returns:
        Dict of column → value, or ``None``.
    """
    cur = conn.execute("SELECT * FROM scholarships WHERE fingerprint = ?", (fp,))
    row = cur.fetchone()
    if row is None:
        return None
    return dict(row)


def insert_scholarship(
    conn: sqlite3.Connection,
    fields: Dict[str, Any],
    snap_id: Optional[int],
    cls: str,
) -> int:
    """Insert a new scholarship record and return its row ID.

    Args:
        conn: SQLite connection.
        fields: Mapping of column name → value.
        snap_id: Snapshot ID (unused in insert, stored via evidence).
        cls: Source classification string (stored as ``source_type``).

    Returns:
        Integer row ID of the inserted record.
    """
    now = datetime.now(timezone.utc).isoformat()
    allowed_cols = {
        "fingerprint", "name", "provider", "official_url", "application_url",
        "source_type", "amount_text", "amount_min", "amount_max", "benefit_type",
        "eligibility_text", "education_level", "courses", "academic_req",
        "income_limit", "age_min", "age_max", "gender", "categories",
        "domicile_states", "institution_req", "documents_required",
        "selection_process", "renewal_req", "opening_date", "closing_date",
        "status", "verification_label", "confidence", "score_breakdown",
        "first_seen", "last_seen", "last_verified", "content_hash",
    }
    data = {k: v for k, v in fields.items() if k in allowed_cols}
    data.setdefault("source_type", cls)
    data.setdefault("first_seen", now)
    data.setdefault("last_seen", now)
    data.setdefault("status", "ACTIVE")

    cols = ", ".join(data.keys())
    placeholders = ", ".join("?" for _ in data)
    cur = conn.execute(
        f"INSERT INTO scholarships ({cols}) VALUES ({placeholders})",
        list(data.values()),
    )
    conn.commit()
    return cur.lastrowid


def update_scholarship(
    conn: sqlite3.Connection,
    scholarship_id: int,
    fields: Dict[str, Any],
) -> None:
    """Update specific columns on an existing scholarship record.

    Args:
        conn: SQLite connection.
        scholarship_id: Row ID.
        fields: Mapping of column name → new value.
    """
    allowed_cols = {
        "name", "provider", "official_url", "application_url",
        "source_type", "amount_text", "amount_min", "amount_max", "benefit_type",
        "eligibility_text", "education_level", "courses", "academic_req",
        "income_limit", "age_min", "age_max", "gender", "categories",
        "domicile_states", "institution_req", "documents_required",
        "selection_process", "renewal_req", "opening_date", "closing_date",
        "status", "verification_label", "confidence", "score_breakdown",
        "last_seen", "last_verified", "content_hash",
    }
    data = {k: v for k, v in fields.items() if k in allowed_cols}
    if not data:
        return

    now = datetime.now(timezone.utc).isoformat()
    data["last_seen"] = now

    set_clause = ", ".join(f"{k} = ?" for k in data)
    values = list(data.values()) + [scholarship_id]
    conn.execute(
        f"UPDATE scholarships SET {set_clause} WHERE id = ?",
        values,
    )
    conn.commit()


def save_evidence(
    conn: sqlite3.Connection,
    scholarship_id: int,
    field: str,
    value: Optional[str],
    quote: Optional[str],
    source_url: str,
    snapshot_id: Optional[int],
    extractor: str,
    grounded: int,
) -> None:
    """Insert an evidence row for a single extracted field.

    Args:
        conn: SQLite connection.
        scholarship_id: Parent scholarship row ID.
        field: Field name (e.g. ``"closing_date"``).
        value: Extracted value as a string.
        quote: Exact supporting sentence from the page.
        source_url: URL the evidence was drawn from.
        snapshot_id: Snapshot ID (may be ``None``).
        extractor: ``"rule"`` or ``"llm"``.
        grounded: 1 if the quote was grounded-verified, 0 otherwise.
    """
    conn.execute(
        """INSERT INTO evidence
           (scholarship_id, field, value, quote, source_url, snapshot_id, extractor, grounded)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (scholarship_id, field, str(value) if value is not None else None,
         quote, source_url, snapshot_id, extractor, grounded),
    )
    conn.commit()


def log_change(
    conn: sqlite3.Connection,
    scholarship_id: int,
    field: str,
    old_val: Any,
    new_val: Any,
    source_url: str,
    quote: Optional[str],
    change_type: str,
) -> None:
    """Record a detected field change in the change_log table.

    Args:
        conn: SQLite connection.
        scholarship_id: Row ID in ``scholarships``.
        field: Field name that changed.
        old_val: Previous value.
        new_val: New value.
        source_url: URL where the change was detected.
        quote: Supporting evidence quote (may be ``None``).
        change_type: Category string, e.g. ``"UPDATE"``, ``"MISSING"``.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO change_log
           (scholarship_id, field, old_value, new_value, detected_at, source_url, evidence_quote, change_type)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            scholarship_id,
            field,
            str(old_val) if old_val is not None else None,
            str(new_val) if new_val is not None else None,
            now,
            source_url,
            quote,
            change_type,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------


def get_all_active(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """Return all scholarship records with status ACTIVE or EXPIRING_SOON.

    Args:
        conn: SQLite connection.

    Returns:
        List of dicts.
    """
    cur = conn.execute(
        "SELECT * FROM scholarships WHERE status IN ('ACTIVE', 'EXPIRING_SOON')"
    )
    return [dict(row) for row in cur.fetchall()]


def mark_missing(
    conn: sqlite3.Connection,
    scholarship_id: int,
    run_count_missing: int,
) -> None:
    """Transition scholarship status when absent from a crawl run.

    Delegates to :func:`src.changes.mark_scholarship_missing` for
    lifecycle-consistent transitions.

    Args:
        conn: SQLite connection.
        scholarship_id: Row ID.
        run_count_missing: How many consecutive runs it was absent.
    """
    from src.changes import mark_scholarship_missing  # avoid circular import

    mark_scholarship_missing(conn, scholarship_id, run_count_missing)


def apply_date_rules(conn: sqlite3.Connection) -> None:
    """Apply date-based lifecycle transitions (EXPIRING_SOON / EXPIRED).

    Delegates to :func:`src.changes.apply_lifecycle_rules`.

    Args:
        conn: SQLite connection.
    """
    from src.changes import apply_lifecycle_rules  # avoid circular import

    apply_lifecycle_rules(conn)
