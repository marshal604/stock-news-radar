from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List

import feedparser
import httpx

from .base import NewsItem, Source

logger = logging.getLogger(__name__)

EDGAR_URL_TEMPLATE = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcompany&CIK={cik}&type=8-K&dateb=&owner=include&count=20&output=atom"
)
USER_AGENT = "stock-news-radar marshal604@gmail.com"
TIMEOUT_SEC = 10.0


class EdgarSource(Source):
    name = "edgar"
    confidence = "critical"

    def fetch(self, ticker: str, ticker_meta: dict) -> List[NewsItem]:
        cik = ticker_meta.get("cik")
        if not cik:
            logger.warning("edgar: %s has no CIK in tickers.json", ticker)
            return []

        url = EDGAR_URL_TEMPLATE.format(cik=cik.lstrip("0").zfill(10))
        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml"},
                timeout=TIMEOUT_SEC,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.warning("edgar fetch failed for %s: %s", ticker, e)
            return []

        feed = feedparser.parse(resp.content)
        items: List[NewsItem] = []
        for entry in feed.entries:
            published = _parse_atom_time(entry)
            if published is None:
                continue
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            summary = (entry.get("summary") or "").strip()
            if not link or not title:
                continue
            items.append(
                NewsItem(
                    url=link,
                    title=title,
                    raw_text=f"{title}\n\n{summary}",
                    published_at=published,
                    source=self.name,
                    source_confidence=self.confidence,
                    ticker_hint=ticker,
                    publisher="SEC EDGAR",
                )
            )
        return items


def _parse_atom_time(entry) -> datetime | None:
    parsed = entry.get("updated_parsed") or entry.get("published_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)
