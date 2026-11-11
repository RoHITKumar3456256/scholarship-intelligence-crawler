"""Source type classification for scholarship URLs."""

import logging
import tldextract
from typing import Dict, Tuple

logger = logging.getLogger(__name__)

# TLD suffixes indicating government and educational domains
GOV_SUFFIXES = (".gov.in", ".nic.in", ".gov")
EDU_SUFFIXES = (".ac.in", ".edu.in", ".edu")


def classify(
    url: str,
    aggregators: list,
    allowlist: Dict[str, str],
) -> Tuple[str, bool]:
    """Classify a URL by its source type and determine if it is official.

    Args:
        url: The full URL to classify.
        aggregators: List of registered-domain strings that are aggregator sites
            (e.g. ``["buddy4study.com", "medium.com"]``).
        allowlist: Mapping of registered domain → source type label for
            known foundation / CSR / international domains.

    Returns:
        A tuple ``(source_type, is_official)`` where *source_type* is one of:
        ``GOVERNMENT``, ``UNIVERSITY``, ``CORPORATE_CSR``, ``FOUNDATION``,
        ``INTERNATIONAL``, ``AGGREGATOR``, or ``UNKNOWN``, and *is_official*
        is ``True`` for trustworthy primary sources.
    """
    try:
        ext = tldextract.extract(url)
    except Exception as exc:
        logger.warning("tldextract failed for %s: %s", url, exc)
        return "UNKNOWN", False

    # Reconstruct full host (subdomain.domain.suffix) and registered domain
    host = ".".join(p for p in (ext.subdomain, ext.domain, ext.suffix) if p)
    reg_domain = f"{ext.domain}.{ext.suffix}"

    # 1. Aggregators / noise sites → not official
    if reg_domain in aggregators:
        logger.debug("Classified %s as AGGREGATOR", url)
        return "AGGREGATOR", False

    # 2. Government domains
    if any(host.endswith(s) for s in GOV_SUFFIXES):
        logger.debug("Classified %s as GOVERNMENT", url)
        return "GOVERNMENT", True

    # 3. Educational / university domains
    if any(host.endswith(s) for s in EDU_SUFFIXES):
        logger.debug("Classified %s as UNIVERSITY", url)
        return "UNIVERSITY", True

    # 4. Known foundation / CSR / international allowlist
    if reg_domain in allowlist:
        source_type = allowlist[reg_domain]
        logger.debug("Classified %s as %s (allowlisted)", url, source_type)
        return source_type, True

    logger.debug("Classified %s as UNKNOWN", url)
    return "UNKNOWN", False
