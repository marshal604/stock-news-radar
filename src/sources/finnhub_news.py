"""Finnhub /company-news source.

Finnhub indexes ticker-tagged news from major financial publishers and exposes
a clean JSON endpoint with {headline, summary, url, datetime, source}. The
summary field (~200-500 chars) is enough context for the LLM classifier to
make a decision without us scraping the article body — bypassing the entire
SPA/paywall extraction battle.

confidence='high' because:
  - Ticker-bound at the API level (no name-collision risk)
  - Editorial filter (Finnhub aggregates from Reuters/Bloomberg/SeekingAlpha
    and excludes most aggregator noise)
  - Summary always present → body_fetch_status starts at 'partial' so
    decide_tier doesn't apply the title_only cap

Reuses FINNHUB_API_KEY env var (already used by earnings_calendar). If the
key is missing at build time, build_sources() skips registering this source —
fail-loud-but-don't-crash."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import List

import httpx

from .base import NewsItem, Source

logger = logging.getLogger(__name__)

FINNHUB_URL = "https://finnhub.io/api/v1/company-news"
TIMEOUT_SEC = 10.0
LOOKBACK_DAYS = 2  # Pipeline freshness cutoff is 24h, but ask for 48h to absorb
                   # boundary jitter and one-run-skipped scenarios.


class FinnhubNewsSource(Source):
    name = "finnhub_news"
    confidence = "high"

    def __init__(self, api_key: str):
        self._api_key = api_key

    def fetch(self, ticker: str, ticker_meta: dict) -> List[NewsItem]:
        today = datetime.now(timezone.utc).date()
        from_date = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
        to_date = today.isoformat()

        try:
            with httpx.Client(timeout=TIMEOUT_SEC) as client:
                resp = client.get(
                    FINNHUB_URL,
                    params={
                        "symbol": ticker,
                        "from": from_date,
                        "to": to_date,
                        "token": self._api_key,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning("finnhub_news fetch failed for %s: %s", ticker, e)
            return []

        if not isinstance(data, list):
            logger.warning("finnhub_news unexpected response shape for %s: %r", ticker, type(data))
            return []

        items: List[NewsItem] = []
        seen_urls: set[str] = set()
        for entry in data:
            try:
                url = (entry.get("url") or "").strip()
                title = (entry.get("headline") or "").strip()
                summary = (entry.get("summary") or "").strip()
                ts = entry.get("datetime")
                publisher = (entry.get("source") or "").strip() or None
                if not url or not title or ts is None:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                published = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            except (TypeError, ValueError) as e:
                logger.debug("malformed finnhub entry: %s — %s", entry, e)
                continue

            # Summary is reliable body context (200-500 chars typical). Mark
            # body_fetch_status='partial' upfront so decide_tier doesn't apply
            # the title_only cap and format_alert annotates correctly. The
            # downstream _enrich_with_body call still runs and may upgrade to
            # 'complete' if a full scrape succeeds.
            raw_text = f"{title}\n\n{summary}" if summary else title
            initial_status = "partial" if summary else "summary_only"
            items.append(
                NewsItem(
                    url=url,
                    title=title,
                    raw_text=raw_text,
                    published_at=published,
                    source=self.name,
                    source_confidence=self.confidence,
                    ticker_hint=ticker,
                    publisher=publisher,
                    body_fetch_status=initial_status,
                )
            )
        logger.info("finnhub_news collected %d items for %s", len(items), ticker)
        return items


# Re-export dataclasses so __init__.py / pipeline don't need to import twice.
__all__ = ["FinnhubNewsSource"]
