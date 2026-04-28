"""Best-effort article body fetcher with explicit completeness signal (T1+T2).

Per harness reframe: extraction failure is a SYSTEM REALITY, not a bug to fix
with bigger libraries. Crystallize it as a typed status the downstream tier
decision and Discord formatter can react to. Don't fight unwinnable extraction
battles (MSN SPA, paywalls); admit them and let the user know.

Strategies in priority order:
  1. trafilatura — primary article extractor
  2. readability-lxml — fallback
  3. og:description / twitter:description meta tags — partial info; 90% of
     publishers serve these even when article body is JS-rendered
  4. r.jina.ai Reader API — last-ditch JS-rendering proxy

Quality gates run AFTER extraction (any one fail → reject):
  - boilerplate_marker: 'Welcome to our dedicated page', 'Subscribe to read'
  - title_phrase_check: 2+ consecutive significant title words must appear
    consecutively in body (catches landing-page redirects)
  - aggressive_redirect: 30x dropped enough path to land on a section page

Returns (text, status). Caller updates NewsItem.body_fetch_status — decide_tier
caps tier at MEDIUM for title_only items, format_alert annotates the Discord
message with '[僅依標題判斷]' so user can recalibrate."""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup
from readability import Document

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
TIMEOUT_SEC = 5.0
JINA_TIMEOUT_SEC = 15.0
MAX_BODY_CHARS = 4000
MIN_BODY_CHARS = 200
COMPLETE_BODY_CHARS = 500
META_MIN_CHARS = 50

_BOILERPLATE_MARKERS = (
    "welcome to our dedicated page",
    "subscribe to read",
    "sign up for our",
    "create a free account",
    "this content is exclusive to subscribers",
    "you must be a subscriber",
    "log in to continue reading",
)


@lru_cache(maxsize=512)
def fetch_article_body(
    url: str, *, title: Optional[str] = None
) -> Tuple[Optional[str], str]:
    """Returns (text or None, body_fetch_status).

    status ∈ {'complete', 'partial', 'title_only'}. Caller stores status on
    NewsItem so decide_tier and format_alert can react to info completeness.
    Note: 'summary_only' is the constructor default — fetcher never returns it
    (means 'never tried'). After this call, status is always one of the three."""
    if not url or not url.startswith(("http://", "https://")):
        return None, "title_only"

    # Strategy 1+2: local extractors
    html, final_url = _fetch_html(url)
    if html and not _redirect_dropped_slug(url, final_url):
        text = _extract_trafilatura(html, url) or _extract_readability(html)
        status = _classify_body(text, title)
        if status != "title_only":
            return text[:MAX_BODY_CHARS], status

        # Strategy 3: meta-tag fallback (og:description / twitter:description)
        meta_text = _extract_meta_description(html)
        if meta_text and len(meta_text) >= META_MIN_CHARS:
            return meta_text, "partial"

    # Strategy 4: r.jina.ai server-side renderer (handles MSN-class SPAs)
    jina_text = _extract_jina(url)
    status = _classify_body(jina_text, title)
    if status != "title_only":
        return jina_text[:MAX_BODY_CHARS], status

    logger.info("body fetch: title_only (all strategies failed) for %s", url[:80])
    return None, "title_only"


def _classify_body(text: Optional[str], title: Optional[str]) -> str:
    """Map extracted text → one of {complete, partial, title_only} after gates."""
    if not text:
        return "title_only"
    if len(text) < MIN_BODY_CHARS:
        return "title_only"
    if _has_boilerplate_marker(text):
        return "title_only"
    if title and not _title_phrase_in_body(title, text):
        return "title_only"
    if len(text) >= COMPLETE_BODY_CHARS:
        return "complete"
    return "partial"


# ── Strategies ────────────────────────────────────────────────────────


def _fetch_html(url: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        with httpx.Client(
            timeout=TIMEOUT_SEC,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
        ) as client:
            resp = client.get(url)
        if resp.status_code != 200 or not resp.text:
            logger.info("article fetch: status=%s url=%s", resp.status_code, url[:80])
            return None, str(resp.url)
        return resp.text, str(resp.url)
    except Exception as e:
        logger.info("article fetch: HTTP error for %s: %s", url[:80], e)
        return None, None


def _extract_trafilatura(html: str, url: str) -> Optional[str]:
    try:
        text = trafilatura.extract(
            html, url=url,
            include_comments=False, include_tables=False,
            favor_recall=True, no_fallback=False,
        )
    except Exception:
        return None
    return " ".join(text.split()) if text else None


def _extract_readability(html: str) -> Optional[str]:
    try:
        doc = Document(html)
        body_html = doc.summary()
        text = BeautifulSoup(body_html, "lxml").get_text(separator="\n", strip=True)
    except Exception:
        return None
    return " ".join(text.split()) if text else None


def _extract_meta_description(html: str) -> Optional[str]:
    """og:description / twitter:description / description meta tags.

    Even when article body requires JS, meta tags are often server-rendered.
    100-300 chars typically — partial info, but better than title alone."""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return None
    candidates = [
        soup.find("meta", property="og:description"),
        soup.find("meta", attrs={"name": "twitter:description"}),
        soup.find("meta", attrs={"name": "description"}),
    ]
    for tag in candidates:
        if tag and tag.get("content"):
            content = tag["content"].strip()
            if content:
                return content
    return None


def _extract_jina(url: str) -> Optional[str]:
    """r.jina.ai Reader API — server-side headless browser renders JS pages."""
    try:
        with httpx.Client(timeout=JINA_TIMEOUT_SEC, follow_redirects=True) as client:
            resp = client.get(f"https://r.jina.ai/{url}")
        if resp.status_code != 200 or not resp.text:
            return None
    except Exception:
        return None
    text = resp.text
    marker = "Markdown Content:"
    idx = text.find(marker)
    if idx != -1:
        text = text[idx + len(marker):]
    return " ".join(text.split())


# ── Quality gates ─────────────────────────────────────────────────────


def _has_boilerplate_marker(text: str) -> bool:
    text_lower = text.lower()
    return any(m in text_lower for m in _BOILERPLATE_MARKERS)


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "into", "over", "stock",
    "stocks", "company", "report", "today", "latest", "summary", "ticker",
    "investor", "investors", "trading", "market", "markets", "update", "news",
    "year", "month", "week", "amid", "after", "before", "during", "are", "was",
    "his", "her", "its", "say", "said", "can", "may", "will", "should",
})


def _title_phrase_in_body(title: str, body: str) -> bool:
    significant = [
        w.lower() for w in _TOKEN_RE.findall(title)
        if len(w) >= 4 and w.lower() not in _STOPWORDS
    ]
    if len(significant) < 2:
        return True
    body_lower = body.lower()
    for i in range(len(significant) - 1):
        if f"{significant[i]} {significant[i+1]}" in body_lower:
            return True
    return False


def _redirect_dropped_slug(original: str, final: Optional[str]) -> bool:
    if not final or final == original:
        return False
    orig_path = urlparse(original).path or ""
    final_path = urlparse(final).path or ""
    if len(orig_path) - len(final_path) > 20:
        logger.info(
            "article fetch: redirect dropped slug (%s → %s)",
            orig_path[:60], final_path[:60],
        )
        return True
    return False


def clear_cache() -> None:
    fetch_article_body.cache_clear()
