from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

import yfinance as yf

from .base import NewsItem, Source

logger = logging.getLogger(__name__)


class YahooSource(Source):
    name = "yahoo"
    confidence = "high"

    def fetch(self, ticker: str, ticker_meta: dict) -> List[NewsItem]:
        try:
            raw = yf.Ticker(ticker).news or []
        except Exception as e:
            logger.warning("yahoo fetch failed for %s: %s", ticker, e)
            return []

        items: List[NewsItem] = []
        for entry in raw:
            normalized = _normalize_entry(entry)
            if normalized is None:
                continue
            title, link, publisher, published, summary = normalized
            items.append(
                NewsItem(
                    url=link,
                    title=title,
                    raw_text=f"{title}\n\n{summary}".strip(),
                    published_at=published,
                    source=self.name,
                    source_confidence=self.confidence,
                    ticker_hint=ticker,
                    publisher=publisher,
                )
            )
        return items


def _normalize_entry(entry: dict):
    """yfinance schema has shifted across versions. Handle both legacy and new shapes."""
    if not isinstance(entry, dict):
        return None

    content = entry.get("content")
    if isinstance(content, dict):
        title = (content.get("title") or "").strip()
        canonical = content.get("canonicalUrl") or {}
        link = (canonical.get("url") if isinstance(canonical, dict) else None) or ""
        link = link.strip()
        provider = content.get("provider") or {}
        publisher = (provider.get("displayName") if isinstance(provider, dict) else None) or ""
        pub_date = content.get("pubDate") or content.get("displayTime")
        published = _parse_iso(pub_date)
        summary = (content.get("summary") or content.get("description") or "").strip()
    else:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        publisher = (entry.get("publisher") or "").strip()
        ts = entry.get("providerPublishTime")
        published = (
            datetime.fromtimestamp(ts, tz=timezone.utc)
            if isinstance(ts, (int, float))
            else None
        )
        summary = (entry.get("summary") or "").strip()

    if not title or not link or published is None:
        return None
    return title, link, publisher, published, summary


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        from dateutil import parser

        dt = parser.isoparse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None
