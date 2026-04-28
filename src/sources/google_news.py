from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import List
from urllib.parse import quote_plus

import feedparser
import httpx
from googlenewsdecoder import gnewsdecoder

from .base import NewsItem, Source

logger = logging.getLogger(__name__)

URL_TEMPLATE = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
USER_AGENT = "Mozilla/5.0 stock-news-radar/0.1"
TIMEOUT_SEC = 10.0
# M2 v2: actual publisher URL resolution via googlenewsdecoder. Google News RSS
# intentionally hides publisher URLs in its <link> field — only the base64-encoded
# article ID is exposed. The library calls Google's internal batchexecute API to
# resolve. ~1-2s per URL, so we only resolve at LLM phase (after keyword gate),
# not for every fetched article. NewsItem.url stays as redirect URL at fetch time
# (consistent for cross-run url_hash dedup); resolution happens in pipeline._process_item.
DECODER_INTERVAL_SEC = 1


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
                # Keep the Google News redirect URL at fetch time. Decoded at
                # LLM phase via decode_google_news_url() — too expensive to
                # run for ~100 articles per fetch.
                if raw_link in seen_urls:
                    continue
                seen_urls.add(raw_link)
                published = _parse_pubdate(entry)
                if published is None:
                    continue
                summary = (entry.get("summary") or "").strip()
                publisher = _extract_publisher(entry)
                items.append(
                    NewsItem(
                        url=raw_link,
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


@lru_cache(maxsize=256)
def decode_google_news_url(url: str) -> str:
    """Resolve a Google News redirect URL to the actual publisher article URL.

    Uses googlenewsdecoder which calls Google's internal batchexecute endpoint
    (the same API that powers the JS-based interstitial redirect). Static HTML
    parsing was attempted first but Google News pages are pure JS — no canonical
    or og:url to extract. The base64 protobuf in the URL itself contains only
    the article ID, not the publisher URL.

    ~1-1.5s per call. Cached so the same URL across queries within a process
    only resolves once. Falls back to the original URL on any failure — caller
    treats this as 'best-effort canonicalization', never blocks ingestion."""
    if "news.google.com" not in url:
        return url
    try:
        result = gnewsdecoder(url, interval=DECODER_INTERVAL_SEC)
        if result.get("status") and result.get("decoded_url"):
            decoded = result["decoded_url"]
            logger.info("decoded Google News URL → %s", decoded[:80])
            return decoded
    except Exception as e:
        logger.debug("gnewsdecoder failed for %s: %s", url[:60], e)
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
