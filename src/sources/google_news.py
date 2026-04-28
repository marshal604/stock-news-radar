from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List
from urllib.parse import quote_plus

import feedparser
import httpx

from .base import NewsItem, Source

logger = logging.getLogger(__name__)

URL_TEMPLATE = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
USER_AGENT = "Mozilla/5.0 stock-news-radar/0.1"
TIMEOUT_SEC = 10.0


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
                link = (entry.get("link") or "").strip()
                if not title or not link or link in seen_urls:
                    continue
                seen_urls.add(link)
                published = _parse_pubdate(entry)
                if published is None:
                    continue
                summary = (entry.get("summary") or "").strip()
                publisher = _extract_publisher(entry)
                items.append(
                    NewsItem(
                        url=link,
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
