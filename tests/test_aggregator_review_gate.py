"""Google News + aggregator publisher → forced REVIEW (no LLM, no Discord).

Defends against the MSN re-syndication problem: aggregators copy other
publishers' articles weeks/months later with a fresh-looking pubDate. We can't
trust the timestamp and the SPA pages defeat body extraction, so the only
correct response is to capture in processed-log without firing an alert.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.pipeline import PipelineConfig, _is_aggregator, _process_item
from src.qc import QCLogger
from src.sources.base import NewsItem


@pytest.fixture
def qc(tmp_path):
    logger = QCLogger(processed_log_dir=tmp_path, daily_report_dir=tmp_path)
    yield logger
    logger.close()


def _config() -> PipelineConfig:
    return PipelineConfig(
        tickers={
            "UUUU": {
                "company_name": "Energy Fuels",
                "ticker_aliases": ["UUUU"],
                "company_aliases": ["Energy Fuels Inc"],
            },
        },
        keywords={"UUUU": {"include_high": [], "include_medium": [], "exclude_strict": []}},
        sources_config={},
        state_db=Path("/tmp/stock-news-radar-test.sqlite"),
        processed_log_dir=Path("/tmp"),
        daily_report_dir=Path("/tmp"),
    )


def _msn_item() -> NewsItem:
    return NewsItem(
        url="http://www.msn.com/en-us/money/markets/energy-fuels-produces-first-us-heavy-rare-earth/ar-AA1ZnnAF",
        title="Energy Fuels produces first US heavy rare earth terbium oxide at Utah mill",
        raw_text="Energy Fuels produces first US heavy rare earth terbium oxide at Utah mill\n\nMSN",
        published_at=datetime.now(timezone.utc),
        source="google_news",
        source_confidence="medium",
        ticker_hint="UUUU",
        publisher="MSN",
    )


def test_msn_publisher_routes_to_review(qc):
    decision, verdict, _item = _process_item(item=_msn_item(), config=_config(), qc=qc)
    assert decision.tier == "REVIEW"
    assert decision.primary_ticker == "UUUU"
    assert any(r.startswith("aggregator_publisher:") for r in decision.reasons)
    assert verdict is None
    assert "llm_call:primary" not in qc._counters


def test_yahoo_finance_url_routes_to_review(qc):
    item = NewsItem(
        url="https://finance.yahoo.com/news/uuuu-something",
        title="UUUU news",
        raw_text="Energy Fuels (UUUU) ...",
        published_at=datetime.now(timezone.utc),
        source="google_news",
        source_confidence="medium",
        ticker_hint="UUUU",
        publisher="Yahoo Finance",
    )
    decision, verdict, _item = _process_item(item=item, config=_config(), qc=qc)
    assert decision.tier == "REVIEW"
    assert verdict is None


def test_non_aggregator_google_news_passes_gate():
    """Reuters / GlobeNewswire entries on Google News should NOT be gated."""
    item = NewsItem(
        url="https://www.reuters.com/markets/commodities/uranium-something",
        title="Uranium prices climb",
        raw_text="Uranium prices climb. Energy Fuels (UUUU) ...",
        published_at=datetime.now(timezone.utc),
        source="google_news",
        source_confidence="medium",
        ticker_hint="UUUU",
        publisher="Reuters",
    )
    assert _is_aggregator(item) is False


def test_pr_newswire_source_not_gated():
    """pr_newswire is a separate source with high confidence; aggregator gate
    only fires on source=='google_news'."""
    item = NewsItem(
        url="https://www.globenewswire.com/news-release/2026/...",
        title="Energy Fuels Reports Q1 2026 Results",
        raw_text="Energy Fuels Inc (NYSE: UUUU) today reported ...",
        published_at=datetime.now(timezone.utc),
        source="pr_newswire",
        source_confidence="high",
        ticker_hint="UUUU",
        publisher="globenewswire.com",
    )
    # _is_aggregator could match if URL contained 'msn'; here it shouldn't.
    assert _is_aggregator(item) is False
