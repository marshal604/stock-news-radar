"""Press-release wires (GlobeNewswire / PR Newswire / Business Wire) source.

Why this exists separately from google_news
-------------------------------------------
Google News RSS mixes first-party newswires (full-text, no SPA, no paywall)
with aggregators (MSN, Yahoo Finance) that obscure original publication date
and serve JS-only article pages. By restricting the search to wire publishers
via `site:` operator, every result is a primary press release we can reliably
extract body text from. confidence=high lets these flow to MEDIUM/HIGH alerts
on keyword match without going through the Google News REVIEW downgrade gate.

Per-ticker queries are derived from each ticker's company_aliases (no extra
config); we union the aliases with OR so 'Energy Fuels' and 'Energy Fuels Inc.'
both match.

URL resolution: Google News wraps result links in news.google.com redirects.
Decoding is deferred to the pipeline's per-item phase (after freshness +
C3 cap reduce the working set to ≤ 20) — same pattern as GoogleNewsSource.
Cross-run dedup still works because SeenStore.is_seen checks
(url_hash OR title_hash) and title_hash is publisher-normalized."""
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
WIRE_SITES = ("globenewswire.com", "prnewswire.com", "businesswire.com")


class PRWireSource(Source):
    name = "pr_newswire"
    confidence = "high"

    def fetch(self, ticker: str, ticker_meta: dict) -> List[NewsItem]:
        aliases = _collect_aliases(ticker, ticker_meta)
        if not aliases:
            return []

        # Combine aliases with OR; quote multi-word names. Cap at 3 aliases to
        # keep the query string under Google News' (undocumented) length cap.
        alias_clause = " OR ".join(f'"{a}"' for a in aliases[:3])
        site_clause = " OR ".join(f"site:{s}" for s in WIRE_SITES)
        query = f"({alias_clause}) ({site_clause})"
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
            logger.warning("pr_newswire fetch failed (ticker=%s): %s", ticker, e)
            return []

        feed = feedparser.parse(resp.content)
        items: List[NewsItem] = []
        seen_urls: set[str] = set()
        for entry in feed.entries:
            title = (entry.get("title") or "").strip()
            raw_link = (entry.get("link") or "").strip()
            if not title or not raw_link:
                continue
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
        logger.info("pr_newswire collected %d items for %s", len(items), ticker)
        return items


def _collect_aliases(ticker: str, ticker_meta: dict) -> List[str]:
    """Use company_name + company_aliases. Skip ticker symbol itself — wires
    rarely use the bare symbol in headlines, full company name has higher recall."""
    aliases: List[str] = []
    if name := ticker_meta.get("company_name"):
        aliases.append(name)
    for alias in ticker_meta.get("company_aliases") or []:
        if alias not in aliases:
            aliases.append(alias)
    return aliases


def _parse_pubdate(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def _extract_publisher(entry) -> str | None:
    source = entry.get("source")
    if isinstance(source, dict) and source.get("title"):
        return source["title"]
    if hasattr(source, "title") and source.title:
        return source.title
    return None
