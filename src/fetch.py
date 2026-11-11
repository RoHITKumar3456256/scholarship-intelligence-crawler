"""HTTP fetching module with robots.txt compliance, rate-limiting, and snapshotting."""

import hashlib
import io
import logging
import os
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional
from urllib.parse import urlparse

import urllib3
import requests
from bs4 import BeautifulSoup

# Suppress SSL warnings for govt sites with self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FetchResult:
    """Holds the result of a single page fetch."""

    url: str
    http_status: int
    text: str
    content_hash: str
    snapshot_id: Optional[int]
    fetched_at: str  # ISO-8601 UTC string


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

# Per-domain last-request timestamps for rate-limiting
_domain_last_fetch: Dict[str, float] = {}

# Per-domain robots.txt parsers (cached)
_robots_cache: Dict[str, urllib.robotparser.RobotFileParser] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_domain(url: str) -> str:
    """Return the netloc (host) portion of *url*."""
    return urlparse(url).netloc


def _sha256(text: str) -> str:
    """Return the SHA-256 hex digest of *text* (UTF-8 encoded)."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _rate_limit(domain: str, delay: float) -> None:
    """Block until at least *delay* seconds have elapsed since the last fetch for *domain*."""
    last = _domain_last_fetch.get(domain, 0.0)
    elapsed = time.monotonic() - last
    if elapsed < delay:
        time.sleep(delay - elapsed)
    _domain_last_fetch[domain] = time.monotonic()


def _robots_allowed(url: str, user_agent: str) -> bool:
    """Return ``True`` if *user_agent* is allowed to fetch *url* per robots.txt."""
    parsed = urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

    if robots_url not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(robots_url)
        try:
            rp.read()
        except Exception as exc:
            logger.debug("Could not read robots.txt for %s: %s", robots_url, exc)
            # On failure, assume allowed
            rp = None  # type: ignore[assignment]
        _robots_cache[robots_url] = rp  # type: ignore[assignment]

    rp = _robots_cache.get(robots_url)
    if rp is None:
        return True
    try:
        return rp.can_fetch(user_agent, url)
    except Exception:
        return True


def _clean_html(html: str) -> str:
    """Remove boilerplate tags and return clean body text."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # Remove noise tags
    for tag in soup.find_all(["nav", "footer", "header", "script", "style", "noscript", "aside"]):
        tag.decompose()

    return soup.get_text(separator=" ", strip=True)


def _extract_pdf_text(raw_bytes: bytes) -> str:
    """Extract text from PDF bytes using pdfplumber."""
    try:
        import pdfplumber  # lazy import – not all fetches need PDF

        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            pages_text = []
            for page in pdf.pages:
                txt = page.extract_text()
                if txt:
                    pages_text.append(txt)
        return "\n".join(pages_text)
    except Exception as exc:
        logger.warning("PDF extraction failed: %s", exc)
        return ""


def _save_snapshot_text(text: str, run_id: int, content_hash: str, snapshots_dir: str) -> str:
    """Write page text to disk and return the relative file path."""
    run_dir = os.path.join(snapshots_dir, str(run_id))
    os.makedirs(run_dir, exist_ok=True)
    filename = f"{content_hash[:16]}.txt"
    file_path = os.path.join(run_dir, filename)
    try:
        with open(file_path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(text)
    except OSError as exc:
        logger.error("Could not save snapshot to %s: %s", file_path, exc)
    return file_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_page(
    url: str,
    conn,
    run_id: int,
    *,
    user_agent: str = "ScholarshipCrawler/1.0",
    delay: float = 2.5,
    timeout: float = 15.0,
    max_retries: int = 3,
    snapshots_dir: str = "data/snapshots",
) -> Optional[FetchResult]:
    """Fetch a URL, clean the content, save a snapshot, and return a :class:`FetchResult`.

    Args:
        url: Target URL.
        conn: SQLite connection (used to persist snapshot metadata).
        run_id: Current crawl run ID.
        user_agent: HTTP User-Agent string.
        delay: Minimum seconds between requests to the same domain.
        timeout: HTTP request timeout in seconds.
        max_retries: Maximum retry attempts with exponential back-off.
        snapshots_dir: Root directory for snapshot files.

    Returns:
        A :class:`FetchResult` on success, or ``None`` if robots.txt disallows
        or all retries are exhausted.
    """
    from src.store import save_snapshot  # avoid circular import at module level

    domain = _get_domain(url)

    # Robots.txt check
    if not _robots_allowed(url, user_agent):
        logger.info("robots.txt disallows %s – skipping", url)
        return None

    headers = {"User-Agent": user_agent}
    last_exc: Optional[Exception] = None
    http_status = 0
    raw_bytes = b""
    content_type = ""

    # Some Indian govt domains (.nic.in, .gov.in) have self-signed / intermediate
    # SSL certs that fail strict verification. We disable verify for those only.
    _GOV_SSL_RELAXED = (".nic.in", ".gov.in", ".gov")
    verify_ssl = not any(domain.endswith(s) for s in _GOV_SSL_RELAXED)

    for attempt in range(1, max_retries + 1):
        _rate_limit(domain, delay)
        try:
            resp = requests.get(url, headers=headers, timeout=timeout,
                                allow_redirects=True, verify=verify_ssl)
            http_status = resp.status_code
            raw_bytes = resp.content
            content_type = resp.headers.get("Content-Type", "")
            last_exc = None
            break
        except requests.RequestException as exc:
            last_exc = exc
            wait = 2 ** attempt
            logger.warning(
                "Fetch attempt %d/%d failed for %s: %s – retrying in %ds",
                attempt,
                max_retries,
                url,
                exc,
                wait,
            )
            time.sleep(wait)

    if last_exc is not None:
        logger.error("All retries exhausted for %s: %s", url, last_exc)
        return None

    # Extract text
    is_pdf = "application/pdf" in content_type or url.lower().endswith(".pdf")
    if is_pdf:
        text = _extract_pdf_text(raw_bytes)
    else:
        try:
            text = _clean_html(raw_bytes.decode("utf-8", errors="replace"))
        except Exception as exc:
            logger.warning("HTML cleaning failed for %s: %s", url, exc)
            text = raw_bytes.decode("utf-8", errors="replace")

    content_hash = _sha256(text)
    fetched_at = datetime.now(timezone.utc).isoformat()

    # Save to disk
    text_path = _save_snapshot_text(text, run_id, content_hash, snapshots_dir)

    # Persist snapshot record
    snapshot_id: Optional[int] = None
    try:
        snapshot_id = save_snapshot(conn, url, http_status, content_hash, text_path, run_id)
    except Exception as exc:
        logger.error("Failed to save snapshot record for %s: %s", url, exc)

    return FetchResult(
        url=url,
        http_status=http_status,
        text=text,
        content_hash=content_hash,
        snapshot_id=snapshot_id,
        fetched_at=fetched_at,
    )
