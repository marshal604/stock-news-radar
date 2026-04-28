from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import List
from urllib.parse import quote_plus

import feedparser
import httpx
from bs4 import BeautifulSoup

from .base import NewsItem, Source

logger = logging.getLogger(__name__)

URL_TEMPLATE = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
USER_AGENT = "Mozilla/5.0 stock-news-radar/0.1"
TIMEOUT_SEC = 10.0
# M2: HEAD-resolve Google News redirect URLs to canonical publisher URLs.
# Two queries returning the same article via different Google redirects would
# otherwise give different url_hash → cross-query dedup misses. Also gives
# users a single click-through instead of two-hop.
RESOLVE_TIMEOUT_SEC = 3.0


class GoogleNewsSource(Source):
    name = "google_news"
    confidence = "medium"

    def __init__(self, queries_by_ticker: dict[str, list[str]]):
        self.queries_by_ticker = queries_by_ticker

    def fetch(self, ticker: str, ticker_meta: dict) -> List[NewsItem]:
        queries = self.queries_by_ticker.get(ticker, [])
        if not queries:
            return []

        items: List[NewsItem] = []
        seen_urls: set[str] = set()
        for query in queries:
            url = URL_TEMPLATE.format(query=quote_plus(query))
            try:
                resp = httpx.get(
                    url,
                    headers={"User-Agent": USER_AGENT},
                    timeout=TIMEOUT_SEC,
                    follow_redirects=True,
                )
                resp.raise_for_status()
            except Exception as e:
                logger.warning("google_news fetch failed (q=%s): %s", query, e)
                continue

            feed = feedparser.parse(resp.content)
            for entry in feed.entries:
                title = (entry.get("title") or "").strip()
                raw_link = (entry.get("link") or "").strip()
                if not title or not raw_link:
                    continue
                # Resolve Google News redirect to canonical publisher URL (M2).
                final_link = _resolve_redirect(raw_link)
                if final_link in seen_urls:
                    continue
                seen_urls.add(final_link)
                published = _parse_pubdate(entry)
                if published is None:
                    continue
                summary = (entry.get("summary") or "").strip()
                publisher = _extract_publisher(entry)
                items.append(
                    NewsItem(
                        url=final_link,
                        title=title,
                        raw_text=f"{title}\n\n{summary}",
                        published_at=published,
                        source=self.name,
                        source_confidence=self.confidence,
                        ticker_hint=ticker,
                        publisher=publisher,
                    )
                )
        return items


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
_META_REFRESH_RE = re.compile(
    r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+url=([^"\'>\s]+)',
    re.IGNORECASE,
)


def _resolve_redirect(url: str) -> str:
    """Resolve Google News interstitial → publisher URL.

    HEAD only follows server-side redirects, but Google News uses a JS-based
    redirect — HEAD lands on the interstitial, not the publisher. We GET the
    page and extract the publisher URL from HTML hints in priority order:
      1. <meta http-equiv="refresh" url=...>
      2. <link rel="canonical">
      3. <meta property="og:url">
      4. First external <a href> that's not on google.com

    Falls back to the original URL on any failure — never blocks ingestion."""
    if "news.google.com" not in url:
        return url
    try:
        with httpx.Client(
            timeout=RESOLVE_TIMEOUT_SEC,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            resp = client.get(url)
        if resp.status_code != 200 or not resp.text:
            return url

        # Pattern 1: meta refresh (most reliable for Google News interstitial)
        m = _META_REFRESH_RE.search(resp.text)
        if m:
            candidate = m.group(1).strip()
            if candidate and "google.com" not in candidate:
                return candidate

        soup = BeautifulSoup(resp.text, "lxml")

        # Pattern 2: <link rel="canonical">
        canonical = soup.find("link", rel="canonical")
        if canonical and canonical.get("href") and "google.com" not in canonical["href"]:
            return canonical["href"]

        # Pattern 3: <meta property="og:url">
        og = soup.find("meta", attrs={"property": "og:url"})
        if og and og.get("content") and "google.com" not in og["content"]:
            return og["content"]

        # Pattern 4: First external publisher link
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("http") and "google.com" not in href:
                return href

        return url
    except Exception as e:
        logger.debug("redirect resolve failed for %s: %s", url[:60], e)
        return url


def _parse_pubdate(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def _extract_publisher(entry) -> str | None:
    source = entry.get("source")
    if isinstance(source, dict):
        return source.get("title")
    if hasattr(source, "title"):
        return source.title
    return None
