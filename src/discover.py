"""Discovery module: link scoring, page discovery, search, and candidate collection."""

import logging
import re
import sqlite3
import time
import urllib3
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Suppress SSL warnings for govt sites with self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Regex to strip common noise query parameters
_NOISE_QS = re.compile(r"[?&](utm_|sessionid|sid=|PHPSESSID)", re.I)

# ---------------------------------------------------------------------------
# Link scoring
# ---------------------------------------------------------------------------


def link_score(anchor_text: str, url: str, keywords: Dict[str, int]) -> int:
    """Score a hyperlink by keyword matches in its text and URL path.

    Args:
        anchor_text: The visible text of the ``<a>`` element.
        url: The href target.
        keywords: Mapping of keyword → point value.

    Returns:
        Total integer score (0 or more).
    """
    combined = f"{anchor_text} {url}".lower()
    total = 0
    for kw, pts in keywords.items():
        if kw.lower() in combined:
            total += pts
    return total


# ---------------------------------------------------------------------------
# Page-level discovery
# ---------------------------------------------------------------------------


def discover_from_page(
    html: str,
    base_url: str,
    keywords: Dict[str, int],
    min_score: int = 3,
    max_depth: int = 2,
    current_depth: int = 0,
) -> List[Tuple[str, int]]:
    """Extract scored links from an HTML page.

    Args:
        html: Raw HTML content of the page.
        base_url: URL of the page (used to resolve relative links).
        keywords: Keyword → point mapping for scoring.
        min_score: Minimum score threshold to include a link.
        max_depth: Maximum crawl depth allowed.
        current_depth: Current crawl depth (links at ``max_depth`` are not followed further).

    Returns:
        List of ``(absolute_url, score)`` tuples, only for links meeting *min_score*.
    """
    if current_depth >= max_depth:
        return []

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    results: List[Tuple[str, int]] = []
    seen: set = set()

    parsed_base = urlparse(base_url)

    for a_tag in soup.find_all("a", href=True):
        href: str = a_tag["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue

        abs_url = urljoin(base_url, href)
        # Normalise – drop fragment
        abs_url = abs_url.split("#")[0]

        # Only follow http/https
        parsed = urlparse(abs_url)
        if parsed.scheme not in ("http", "https"):
            continue

        if abs_url in seen:
            continue
        seen.add(abs_url)

        anchor_text = a_tag.get_text(separator=" ", strip=True)
        sc = link_score(anchor_text, abs_url, keywords)
        if sc >= min_score:
            results.append((abs_url, sc))

    return results


# ---------------------------------------------------------------------------
# Scholarship page detection
# ---------------------------------------------------------------------------


def is_scholarship_page(text: str) -> bool:
    """Return ``True`` if the page text looks like a scholarship page.

    Heuristic: must contain 'scholarship' or 'fellowship', AND at least one of
    'eligibility', 'amount', 'apply', 'deadline', 'stipend'.
    """
    low = text.lower()
    primary = any(w in low for w in ("scholarship", "fellowship", "stipend"))
    secondary = any(w in low for w in ("eligibility", "amount", "apply", "deadline", "last date", "notification"))
    return primary and secondary


# ---------------------------------------------------------------------------
# DuckDuckGo search
# ---------------------------------------------------------------------------


def search_duckduckgo(
    query: str,
    user_agent: str = "ScholarshipCrawler/1.0",
    timeout: float = 15.0,
) -> List[str]:
    """Query DuckDuckGo HTML endpoint and return a list of result URLs.

    Failures are caught and logged; an empty list is returned on error so the
    crawler can continue with seed URLs alone.

    Args:
        query: Search query string.
        user_agent: HTTP User-Agent header value.
        timeout: Request timeout in seconds.

    Returns:
        List of discovered URLs (may be empty).
    """
    endpoint = "https://html.duckduckgo.com/html/"
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html",
    }
    params = {"q": query}

    try:
        resp = requests.post(endpoint, data=params, headers=headers, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("DuckDuckGo search failed for query %r: %s", query, exc)
        return []

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        logger.warning("Failed to parse DuckDuckGo results: %s", exc)
        return []

    urls: List[str] = []

    # Try multiple CSS selectors across DDG HTML layouts
    for sel in (".result__url", ".result__a", "a.result__url", ".results_links .result__a"):
        for tag in soup.select(sel):
            # Result URLs can be in href or in text content
            href = tag.get("href", "")
            if href and href.startswith("http"):
                urls.append(href)
            else:
                txt = tag.get_text(strip=True)
                if txt.startswith("http"):
                    urls.append(txt)

    # Fallback: scan all <a> for uddg (DDG redirect) or direct http links
    if not urls:
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "uddg=" in href:
                # DDG redirect URL — extract the actual target
                from urllib.parse import parse_qs, urlparse as _up
                qs = parse_qs(_up(href).query)
                target = qs.get("uddg", [None])[0]
                if target and target.startswith("http"):
                    urls.append(target)
            elif href.startswith("http") and "duckduckgo.com" not in href:
                urls.append(href)

    # Deduplicate while preserving order
    seen: set = set()
    unique: List[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)

    logger.debug("DuckDuckGo returned %d results for %r", len(unique), query)
    return unique


# ---------------------------------------------------------------------------
# Full discovery pipeline
# ---------------------------------------------------------------------------


def discover_candidates(
    seeds: List[str],
    search_queries: List[str],
    keywords: Dict[str, int],
    conn: sqlite3.Connection,
    run_id: int,
    classify_fn: Callable[[str], Tuple[str, bool]],
    config: dict,
) -> List[Tuple[str, str, bool]]:
    """Full discovery pipeline: seeds + DuckDuckGo + link-following.

    1. All seed URLs are added as PENDING candidates.
    2. DuckDuckGo is queried for each search query; results are scored and
       added if they meet ``link_relevance_min``.
    3. Each seed page is fetched (lightly) and outgoing links are scored;
       promising links are added to the candidate pool.

    Args:
        seeds: Flat list of seed URLs.
        search_queries: Search query strings.
        keywords: Keyword → point mapping.
        conn: SQLite connection.
        run_id: Current run ID.
        classify_fn: Function ``(url) -> (source_type, is_official)``.
        config: Full config dict.

    Returns:
        List of ``(url, source_class, is_official)`` tuples for all candidates.
    """
    thresholds = config.get("thresholds", {})
    min_score: int = thresholds.get("link_relevance_min", 3)
    max_depth: int = thresholds.get("max_discovery_depth", 2)
    max_links: int = thresholds.get("max_links_per_page", 50)
    delay: float = float(thresholds.get("request_delay_seconds", 2.5))
    timeout: float = float(thresholds.get("request_timeout_seconds", 15))
    user_agent: str = config.get("crawler", {}).get("user_agent", "ScholarshipCrawler/1.0")

    candidates: Dict[str, Tuple[str, bool]] = {}  # url -> (source_type, is_official)

    def _add(url: str, via: str) -> None:
        if url in candidates:
            return
        src_type, is_off = classify_fn(url)
        candidates[url] = (src_type, is_off)
        try:
            conn.execute(
                """INSERT OR IGNORE INTO candidates
                   (url, discovered_via, source_class, score, status, run_id)
                   VALUES (?, ?, ?, 0, 'PENDING', ?)""",
                (url, via, src_type, run_id),
            )
            conn.commit()
        except sqlite3.Error as exc:
            logger.error("Failed to insert candidate %s: %s", url, exc)

    # 1. Seed URLs
    for url in seeds:
        _add(url, "seed")

    # 2. DuckDuckGo search
    for query in search_queries:
        logger.info("Searching DuckDuckGo: %r", query)
        time.sleep(delay)
        results = search_duckduckgo(query, user_agent=user_agent, timeout=timeout)
        for url in results:
            sc = link_score("", url, keywords)
            if sc >= min_score:
                _add(url, f"search:{query[:40]}")

    # 3. Fetch seed pages and follow promising links
    headers = {"User-Agent": user_agent}
    seed_urls_to_crawl = seeds[:20]  # limit seed crawl for discovery phase

    _GOV_SSL_RELAXED = (".nic.in", ".gov.in", ".gov")

    for seed_url in seed_urls_to_crawl:
        logger.info("Discovering links from seed: %s", seed_url)
        time.sleep(delay)
        domain = urlparse(seed_url).netloc
        verify_ssl = not any(domain.endswith(s) for s in _GOV_SSL_RELAXED)
        try:
            resp = requests.get(seed_url, headers=headers, timeout=timeout,
                                allow_redirects=True, verify=verify_ssl)
            html = resp.text
        except requests.RequestException as exc:
            logger.warning("Could not fetch seed %s: %s", seed_url, exc)
            continue

        links = discover_from_page(
            html,
            seed_url,
            keywords,
            min_score=min_score,
            max_depth=max_depth,
            current_depth=0,
        )

        # Sort by score descending, take top N
        links.sort(key=lambda x: x[1], reverse=True)
        for url, sc in links[:max_links]:
            _add(url, f"link:{seed_url[:60]}")

    logger.info("Discovery complete: %d candidates found", len(candidates))
    return [(url, st, io_) for url, (st, io_) in candidates.items()]
