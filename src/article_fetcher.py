"""Best-effort article body fetcher (M1).

Fetches HTML from a publisher URL and extracts the main content using
readability-lxml. Falls back gracefully on bot detection, paywall, timeout,
or any HTTP error — caller treats `None` as 'no body, use title only'.

Why this matters
----------------
LLM raw_text was previously title (Finviz, ~60 chars) or title + RSS summary
(Google News, ~200 chars). LLM judging relevance_type / sentiment from that
thin signal explains a chunk of the schema_gap_suspicious_veto pattern: when
LLM can't tell from a title whether 'Energy Fuels Q1 Update' is breaking news
or a recap, it leaks the judgment via should_alert=false.

Body enrichment doesn't fix every case (paywalled sites still block us) but
turns the 60-200 char window into 1500-4000 chars when we can get it.

In-memory LRU cache prevents redundant fetches within a run (same article
URL appearing across multiple Google News queries).
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

import httpx
from bs4 import BeautifulSoup
from readability import Document

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
TIMEOUT_SEC = 5.0
MAX_BODY_CHARS = 4000
# Min length to be considered useful — short responses are often paywall stubs
# or 'subscribe to read' interstitials, not real article content.
MIN_BODY_CHARS = 200


@lru_cache(maxsize=512)
def fetch_article_body(url: str) -> Optional[str]:
    """Fetch and extract main content from URL. None on any failure or paywall stub.

    Cached for the lifetime of the Python process — same URL across multiple
    Google News queries within one pipeline run only fetches once."""
    if not url or not url.startswith(("http://", "https://")):
        return None
    try:
        with httpx.Client(
            timeout=TIMEOUT_SEC,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        ) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            logger.debug("fetch %s status=%s", url[:80], resp.status_code)
            return None
        if not resp.text:
            return None

        # Readability extracts the main content block. Heuristic-based; works
        # well on news sites, can fail on JS-rendered SPA articles.
        doc = Document(resp.text)
        html_body = doc.summary()
        text = BeautifulSoup(html_body, "lxml").get_text(separator="\n", strip=True)
        text = " ".join(text.split())  # collapse whitespace runs

        if len(text) < MIN_BODY_CHARS:
            # Likely a paywall stub or "subscribe" page — not useful context
            logger.debug("body too short for %s: %d chars", url[:80], len(text))
            return None

        result = text[:MAX_BODY_CHARS]
        logger.info("body fetched: %d chars from %s", len(result), url[:80])
        return result
    except Exception as e:
        logger.info("article fetch failed for %s: %s", url[:80], e)
        return None


def clear_cache() -> None:
    """Reset the LRU cache. Used by tests; not needed in production."""
    fetch_article_body.cache_clear()
