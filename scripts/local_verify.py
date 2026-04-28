"""Local verification of routine-governance + impact_assessment behavior.

Runs synthetic NewsItems modeled on user's 3 historical cases through
_process_item without going through full pipeline (no Discord post, no
mark_seen, no QC flush). Prints decision + reasoning so we can verify
the new prompt + schema before burning CI quota.

Cases:
  1. TEM Proxy via Google News redirect (will resolve via gnewsdecoder)
  2. MSN Energy Fuels via Google News redirect (body unfetchable — JS SPA)
  3. TEM Proxy direct Quartr URL (body fetcher will succeed — full filing)
"""
from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")

from src.pipeline import PipelineConfig, _process_item
from src.qc import QCLogger
from src.sources.base import NewsItem
from src.sources.google_news import decode_google_news_url

REPO = Path(__file__).resolve().parent.parent
with open(REPO / "config" / "tickers.json") as f:
    TICKERS = json.load(f)
with open(REPO / "config" / "keywords.json") as f:
    KEYWORDS = json.load(f)
    KEYWORDS.pop("_comment", None)
with open(REPO / "config" / "sources.json") as f:
    SOURCES = json.load(f)
    SOURCES.pop("_comment", None)

config = PipelineConfig(
    tickers=TICKERS,
    keywords=KEYWORDS,
    sources_config=SOURCES,
    state_db=Path("/tmp/local_verify.sqlite"),
    processed_log_dir=Path("/tmp"),
    daily_report_dir=Path("/tmp"),
    dry_run=True,
)

cases = [
    NewsItem(
        url="https://news.google.com/rss/articles/CBMic0FVX3lxTE90UnpQZ0ItUGVPZDI3dmpUWGc5UVJVOVVLWVdDaW5XcWZYd05TV2JCQ3d0TUd2Q3g1MmF3RWI4TGNQZ21iSDBINDNUS0ZIWHBiellNMjZnS0c5cFJSUXd2ZHdtVWRTRkVFRzhPR1BZY2ozZWc?oc=5",
        title="Tempus AI (TEM) Proxy filing Summary - Quartr",
        raw_text="Tempus AI (TEM) Proxy filing Summary - Quartr",
        published_at=datetime(2026, 4, 27, 17, 3, tzinfo=timezone.utc),
        source="google_news",
        source_confidence="medium",
        ticker_hint="TEM",
        publisher="Quartr",
    ),
    NewsItem(
        url="https://news.google.com/rss/articles/CBMi6wJBVV95cUxQZHgtcF9yMVFBYzFnZGFYc3ZUWmVONWRBdjdqcGg0Qy14Rm1NTXY1VzdiTzEwQ3c4M2NfY1BtZGRBMXFEM2pkbmJJQTNwNl8zTlRWOV9QdVNBeVp6cXU0RlVaYngtZGNvVDhiY2ZfbWVHcXFORkozR2JKZS1Yald5U2FobGpLOWtSVWFNZjI4Rk5rXzFaTWZGTkFpUzRMcURPSkNBMV83SGUzMk1WNlBMTGR4M09GcjJZdWFVd3NJWmRhQzZNSGgxM1BuT1NCcGIzRTliSjl3ZVFkN0ZHRDJ5Z1ZtdWVHOVREREFhSFNHMUtKZ2J4NDBwdUt4UGhoWHNqM3ZUWWR6clR0R2NTM2Rzb0lmTS01aWpFcW1NLUYyR3BOYVdPWDIzT094MHBPdFM1ZmxaQWNndEt3cEpzMzctVTBOOS0wc3pUTHBOUWFBQkF0UVpEVE9XVkJDWXpVdzRsUTRrWlFZcENOVFE?oc=5",
        title="Can Energy Fuels Lead America's Drive for Rare Earth Independence? - MSN",
        raw_text="Can Energy Fuels Lead America's Drive for Rare Earth Independence? - MSN",
        published_at=datetime(2026, 4, 28, 2, 6, tzinfo=timezone.utc),
        source="google_news",
        source_confidence="medium",
        ticker_hint="UUUU",
        publisher="MSN",
    ),
    NewsItem(
        url="https://quartr.com/events/tempus-ai-inc-tem-proxy-filing_F3j4v0TE",
        title="Tempus AI (TEM) Proxy filing Summary - Quartr",
        raw_text="Tempus AI (TEM) Proxy filing Summary - Quartr",
        published_at=datetime(2026, 4, 27, 17, 3, tzinfo=timezone.utc),
        source="google_news",
        source_confidence="medium",
        ticker_hint="TEM",
        publisher="Quartr",
    ),
]


qc = QCLogger(processed_log_dir=Path("/tmp"), daily_report_dir=Path("/tmp"))
try:
    for i, item in enumerate(cases, 1):
        print(f"\n{'═'*70}\nCASE {i}: {item.title}\n  source URL: {item.url[:90]}\n{'═'*70}")

        # Mirror pipeline.run's outer-loop URL resolution
        if item.source == "google_news" and "news.google.com" in item.url:
            resolved = decode_google_news_url(item.url)
            if resolved != item.url:
                print(f"  → resolved URL: {resolved[:90]}")
                item = dataclasses.replace(item, url=resolved)

        decision, verdict, item = _process_item(item=item, config=config, qc=qc)

        print(f"\n  RESULT")
        print(f"    tier         : {decision.tier}")
        print(f"    reasons      : {decision.reasons}")
        print(f"    body_status  : {item.body_fetch_status}")
        if verdict:
            print(f"    should_alert : {verdict.should_alert}")
            print(f"    relevance    : {verdict.ticker_relevance}")
            print(f"    sentiment    : {verdict.sentiment} | category: {verdict.category}")
            print(f"    chinese_summary  : {verdict.chinese_summary}")
            print(f"    impact_assessment: {verdict.impact_assessment}")
        else:
            print(f"    verdict: <no LLM call> ({decision.reasons})")
finally:
    qc.close()
