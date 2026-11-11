"""Main crawl orchestrator for the Scholarship Intelligence Crawler."""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import yaml

# ---------------------------------------------------------------------------
# Logging setup (must happen before local imports that use logging)
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("crawl.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Project root resolution
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "scholarships.db")
SNAPSHOTS_DIR = os.path.join(PROJECT_ROOT, "data", "snapshots")
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.yaml")
REPLAY_DIR = os.path.join(PROJECT_ROOT, "data", "replay")

# ---------------------------------------------------------------------------
# Local imports (after root is on path)
# ---------------------------------------------------------------------------

sys.path.insert(0, PROJECT_ROOT)

from src import classify, changes, discover, extract, fetch, store, verify


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_config(path: str) -> dict:
    """Load and return the YAML configuration file.

    Args:
        path: Path to ``config.yaml``.

    Returns:
        Parsed config dict.
    """
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------------------
# Seed flattening
# ---------------------------------------------------------------------------


def flatten_seeds(seeds_cfg: dict) -> List[str]:
    """Flatten the nested seeds dict (categories → list) into a single list.

    Args:
        seeds_cfg: ``config["seeds"]`` dict.

    Returns:
        Flat list of seed URLs.
    """
    result: List[str] = []
    for category_urls in seeds_cfg.values():
        if isinstance(category_urls, list):
            result.extend(category_urls)
    return result


# ---------------------------------------------------------------------------
# Replay helpers
# ---------------------------------------------------------------------------


def load_replay_snapshots(replay_dir: str) -> Dict[str, Dict[str, Any]]:
    """Load saved snapshot text files from *replay_dir* for offline testing.

    Expects files named ``<hash>.txt`` in sub-directories corresponding to
    previous run IDs (or directly in ``replay_dir``).

    Args:
        replay_dir: Path to the replay snapshot directory.

    Returns:
        Dict mapping URL (read from a companion ``.url`` file or derived from
        filename) to ``{"text": ..., "http_status": 200, "content_hash": ...}``.
    """
    snapshots: Dict[str, Dict[str, Any]] = {}
    if not os.path.isdir(replay_dir):
        logger.warning("Replay directory not found: %s", replay_dir)
        return snapshots

    for root, _dirs, files in os.walk(replay_dir):
        for fname in files:
            if not fname.endswith(".txt"):
                continue
            txt_path = os.path.join(root, fname)
            url_path = txt_path.replace(".txt", ".url")
            if os.path.exists(url_path):
                with open(url_path, "r", encoding="utf-8") as fh:
                    url = fh.read().strip()
            else:
                # Use filename as a stub URL
                url = f"replay://{fname}"

            try:
                with open(txt_path, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue

            import hashlib
            ch = hashlib.sha256(text.encode()).hexdigest()
            snapshots[url] = {
                "text": text,
                "http_status": 200,
                "content_hash": ch,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }

    logger.info("Loaded %d replay snapshots from %s", len(snapshots), replay_dir)
    return snapshots


# ---------------------------------------------------------------------------
# Per-page processing
# ---------------------------------------------------------------------------


def process_page(
    url: str,
    text: str,
    http_status: int,
    content_hash: str,
    fetched_at: str,
    snapshot_id: Optional[int],
    source_type: str,
    is_official: bool,
    conn,
    run_id: int,
    config: dict,
    llm_enabled: bool,
) -> Optional[int]:
    """Extract, fingerprint, store (or update), score, and log a scholarship page.

    Args:
        url: Source URL.
        text: Cleaned page text.
        http_status: HTTP status code.
        content_hash: SHA-256 of *text*.
        fetched_at: ISO-8601 fetch timestamp.
        snapshot_id: Snapshot row ID (may be ``None`` in replay mode).
        source_type: Classified source type string.
        is_official: Whether source is considered official.
        conn: SQLite connection.
        run_id: Current run ID.
        config: Full config dict.
        llm_enabled: Whether Ollama LLM should be called.

    Returns:
        Scholarship row ID if saved, else ``None``.
    """
    llm_cfg = config.get("llm", {})

    # 1. Rule-based extraction
    rule_result = extract.rule_extract(text)

    # 2. Optional LLM extraction
    llm_result: Dict[str, Tuple[Any, Optional[str]]] = {}
    if llm_enabled and llm_cfg.get("enabled", False):
        max_chars = int(llm_cfg.get("max_text_chars", 12000))
        llm_result = extract.llm_extract(
            text[:max_chars],
            model=llm_cfg.get("model", "llama3.1:8b"),
            base_url=llm_cfg.get("base_url", "http://localhost:11434"),
            temperature=float(llm_cfg.get("temperature", 0)),
        )

    # 3. Merge with grounding
    merged = extract.merge(rule_result, llm_result, text)

    # 4. Name / provider extraction
    name_from_text, provider_from_text = extract.extract_name_provider(text, url)

    # Build field dict from merged results
    fields: Dict[str, Any] = {}
    for field_name, (value, _quote) in merged.items():
        fields[field_name] = value

    # Ensure we have a name (required for fingerprint)
    name = fields.get("name") or name_from_text
    if not name:
        # Use URL path as fallback name
        from urllib.parse import urlparse
        path = urlparse(url).path.strip("/").split("/")[-1]
        name = path.replace("-", " ").replace("_", " ").title() or "Unknown Scholarship"

    provider = fields.get("provider") or provider_from_text
    fields["name"] = name
    fields["provider"] = provider
    fields["official_url"] = url
    fields["source_type"] = source_type
    fields["content_hash"] = content_hash

    # 5. Fingerprint
    fp = changes.fingerprint(name, provider, url)
    fields["fingerprint"] = fp

    # 6. Check if existing record
    existing = store.get_by_fingerprint(conn, fp)
    is_new = existing is None

    # Determine official level for scoring
    if source_type in ("GOVERNMENT", "UNIVERSITY"):
        official_level = verify.OFFICIAL_LEVEL_OFFICIAL
    elif source_type in ("FOUNDATION", "CORPORATE_CSR", "INTERNATIONAL"):
        official_level = verify.OFFICIAL_LEVEL_ALLOWLISTED
    else:
        official_level = verify.OFFICIAL_LEVEL_NONE

    # Gather evidence list (for scoring – will be empty for brand-new records)
    evidence_list: List[Dict[str, Any]] = []
    for field_name, (value, quote) in merged.items():
        is_grounded = extract.grounded(quote, text) if quote else False
        evidence_list.append({
            "field": field_name,
            "value": value,
            "quote": quote,
            "grounded": int(is_grounded),
        })

    # 7. Score
    snapshot_dict = {
        "url": url,
        "http_status": http_status,
        "text": text,
        "fetched_at": fetched_at,
    }
    total_score, label, breakdown = verify.score(
        {**fields, "_rule": rule_result, "_llm": llm_result},
        evidence_list,
        snapshot_dict,
        conflicts=[],
        official_level=official_level,
        config=config,
    )

    fields["verification_label"] = label
    fields["confidence"] = total_score
    fields["score_breakdown"] = json.dumps(breakdown)

    # 8. Store / update
    if is_new:
        scholarship_id = store.insert_scholarship(conn, fields, snapshot_id, source_type)
        logger.info("NEW scholarship #%d: %s (score=%.1f)", scholarship_id, name[:60], total_score)
    else:
        scholarship_id = existing["id"]
        # Detect changes
        diffs = changes.diff_fields(existing, fields, changes.TRACKED_FIELDS)
        for diff_field, old_val, new_val in diffs:
            # Find quote from evidence if available
            ev_quote = next(
                (ev["quote"] for ev in evidence_list if ev["field"] == diff_field),
                None,
            )
            store.log_change(conn, scholarship_id, diff_field, old_val, new_val, url, ev_quote, "UPDATE")
            logger.info("CHANGE in #%d %s: %s → %s", scholarship_id, diff_field, old_val, new_val)

        store.update_scholarship(conn, scholarship_id, fields)

    # 9. Save evidence rows
    for ev in evidence_list:
        store.save_evidence(
            conn,
            scholarship_id=scholarship_id,
            field=ev["field"],
            value=ev["value"],
            quote=ev["quote"],
            source_url=url,
            snapshot_id=snapshot_id,
            extractor="rule" if ev["field"] in rule_result else "llm",
            grounded=ev["grounded"],
        )

    return scholarship_id


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Parse arguments and run the full crawl pipeline."""
    parser = argparse.ArgumentParser(description="Scholarship Intelligence Crawler")
    parser.add_argument("--replay", action="store_true", help="Use saved snapshots instead of live fetch")
    parser.add_argument("--max-pages", type=int, default=None, help="Override max pages per run")
    parser.add_argument("--no-llm", action="store_true", help="Disable LLM extraction")
    args = parser.parse_args()

    # Load config
    config = load_config(CONFIG_PATH)
    crawler_cfg = config.get("crawler", {})
    thresholds = config.get("thresholds", {})

    max_pages: int = args.max_pages or crawler_cfg.get("max_pages_per_run", 200)
    user_agent: str = crawler_cfg.get("user_agent", "ScholarshipCrawler/1.0")
    delay: float = float(thresholds.get("request_delay_seconds", 2.5))
    timeout: float = float(thresholds.get("request_timeout_seconds", 15))
    max_retries: int = int(thresholds.get("max_retries", 3))
    llm_enabled: bool = not args.no_llm

    aggregators: list = config.get("aggregators", [])
    allowlist: dict = config.get("foundation_allowlist", {})

    # Classify function closure
    def classify_fn(url: str) -> Tuple[str, bool]:
        return classify.classify(url, aggregators, allowlist)

    # Initialise DB
    logger.info("Initialising database at %s", DB_PATH)
    conn = store.init_db(DB_PATH)
    run_id = store.start_run(conn)
    logger.info("=== Crawl run #%d started ===", run_id)

    stats = {"discovered": 0, "new": 0, "changed": 0, "expired": 0}

    # --- Discovery phase ---
    if args.replay:
        logger.info("REPLAY MODE: loading snapshots from %s", REPLAY_DIR)
        replay_snapshots = load_replay_snapshots(REPLAY_DIR)
        candidates: List[Tuple[str, str, bool]] = [
            (url, classify_fn(url)[0], classify_fn(url)[1])
            for url in replay_snapshots.keys()
        ]
    else:
        seeds = flatten_seeds(config.get("seeds", {}))
        search_queries = config.get("search_queries", [])
        keywords = config.get("keywords", {})
        candidates = discover.discover_candidates(
            seeds=seeds,
            search_queries=search_queries,
            keywords=keywords,
            conn=conn,
            run_id=run_id,
            classify_fn=classify_fn,
            config=config,
        )

    stats["discovered"] = len(candidates)
    logger.info("Discovered %d candidates", len(candidates))

    # --- Fetch & process phase ---
    pages_processed = 0
    seen_fingerprints_before = set(
        row[0]
        for row in conn.execute("SELECT fingerprint FROM scholarships").fetchall()
    )
    seen_this_run: set = set()

    for url, source_type, is_official in candidates:
        if pages_processed >= max_pages:
            logger.info("Reached max_pages limit (%d) – stopping fetch phase", max_pages)
            break

        logger.info("[%d/%d] Processing: %s", pages_processed + 1, min(max_pages, len(candidates)), url)

        # Fetch or replay
        if args.replay:
            snap_data = replay_snapshots.get(url)
            if not snap_data:
                continue
            text = snap_data["text"]
            http_status = snap_data["http_status"]
            content_hash = snap_data["content_hash"]
            fetched_at = snap_data["fetched_at"]
            snapshot_id: Optional[int] = None
        else:
            result = fetch.fetch_page(
                url,
                conn,
                run_id,
                user_agent=user_agent,
                delay=delay,
                timeout=timeout,
                max_retries=max_retries,
                snapshots_dir=SNAPSHOTS_DIR,
            )
            if result is None:
                logger.info("Skipping %s (fetch failed or disallowed)", url)
                pages_processed += 1
                continue

            text = result.text
            http_status = result.http_status
            content_hash = result.content_hash
            fetched_at = result.fetched_at
            snapshot_id = result.snapshot_id

        # Check if page looks like a scholarship
        if not discover.is_scholarship_page(text):
            logger.debug("Not a scholarship page: %s", url)
            pages_processed += 1
            continue

        # Process
        prev_count = conn.execute("SELECT COUNT(*) FROM scholarships").fetchone()[0]

        scholarship_id = process_page(
            url=url,
            text=text,
            http_status=http_status,
            content_hash=content_hash,
            fetched_at=fetched_at,
            snapshot_id=snapshot_id,
            source_type=source_type,
            is_official=is_official,
            conn=conn,
            run_id=run_id,
            config=config,
            llm_enabled=llm_enabled,
        )

        if scholarship_id is not None:
            new_count = conn.execute("SELECT COUNT(*) FROM scholarships").fetchone()[0]
            if new_count > prev_count:
                stats["new"] += 1
            seen_this_run.add(scholarship_id)

        pages_processed += 1

    # --- Mark missing scholarships ---
    previously_active_fps = {
        row["fingerprint"]: row["id"]
        for row in conn.execute(
            "SELECT fingerprint, id FROM scholarships WHERE status IN ('ACTIVE','EXPIRING_SOON')"
        ).fetchall()
    }
    all_seen_ids = seen_this_run
    for fp, sch_id in previously_active_fps.items():
        if sch_id not in all_seen_ids:
            missing_count = changes.get_missing_runs(conn, sch_id) + 1
            store.mark_missing(conn, sch_id, missing_count)
            logger.info("Scholarship #%d marked missing (run #%d absent)", sch_id, missing_count)

    # --- Lifecycle date rules ---
    store.apply_date_rules(conn)
    expired_count = conn.execute(
        "SELECT COUNT(*) FROM scholarships WHERE status = 'EXPIRED'"
    ).fetchone()[0]
    stats["expired"] = expired_count

    # --- Count changed ---
    changed_count = conn.execute(
        "SELECT COUNT(DISTINCT scholarship_id) FROM change_log WHERE change_type = 'UPDATE' AND id > 0"
    ).fetchone()[0]
    stats["changed"] = changed_count

    # --- Finish run ---
    store.finish_run(conn, run_id, stats)
    conn.close()

    # --- Print summary ---
    print("\n" + "=" * 60)
    print(f"  CRAWL RUN #{run_id} COMPLETE")
    print("=" * 60)
    print(f"  Pages processed : {pages_processed}")
    print(f"  Candidates found: {stats['discovered']}")
    print(f"  New scholarships: {stats['new']}")
    print(f"  Changed records : {stats['changed']}")
    print(f"  Expired records : {stats['expired']}")
    print("=" * 60)
    print(f"  Database: {DB_PATH}")
    print(f"  Dashboard: streamlit run app/dashboard.py")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
