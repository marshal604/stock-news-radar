from __future__ import annotations

import logging
import time
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
# M3: 8-K is a critical signal; one-off SEC infra hiccup shouldn't lose the alert.
# Retry once with a small backoff before raising — pipeline catches and counts it
# as a source_anomaly so silent zero-fetches stop being silent.
RETRY_BACKOFF_SEC = 2.0


class EdgarSource(Source):
    name = "edgar"
    confidence = "critical"

    def fetch(self, ticker: str, ticker_meta: dict) -> List[NewsItem]:
        cik = ticker_meta.get("cik")
        if not cik:
            logger.warning("edgar: %s has no CIK in tickers.json", ticker)
            return []

        url = EDGAR_URL_TEMPLATE.format(cik=cik.lstrip("0").zfill(10))
        last_err: Exception | None = None
        resp = None
        success = False
        for attempt in range(2):
            try:
                resp = httpx.get(
                    url,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml"},
                    timeout=TIMEOUT_SEC,
                )
                resp.raise_for_status()
                success = True
                break
            except Exception as e:
                last_err = e
                if attempt == 0:
                    logger.warning(
                        "edgar fetch %s attempt 1 failed (%s) — retrying in %.1fs",
                        ticker, e, RETRY_BACKOFF_SEC,
                    )
                    time.sleep(RETRY_BACKOFF_SEC)
        if not success:
            # Raise so pipeline records source_anomaly. Silent return [] would
            # have allowed an 8-K to go undetected.
            raise RuntimeError(
                f"edgar fetch failed for {ticker} after retry: {last_err}"
            ) from last_err

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
