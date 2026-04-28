"""T1: items from competitor_finviz must short-circuit to REVIEW.

This gate runs before _candidate_tickers / keyword / LLM, so:
  - never burns LLM quota on competitor noise
  - always accumulates in processed-log for later analysis
  - never reaches Discord (REVIEW is silent)
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.pipeline import PipelineConfig, _process_item
from src.sources.base import NewsItem


def _competitor_item() -> NewsItem:
    return NewsItem(
        url="https://example.com/ccj-uranium-deal",
        title="Cameco Inks $1.9B Long-Term Uranium Supply Deal With India",
        raw_text="Cameco Corporation (NYSE: CCJ) announced a major uranium supply agreement.",
        published_at=datetime.now(timezone.utc),
        source="competitor_finviz",
        source_confidence="medium",
        ticker_hint="UUUU",
        publisher="Reuters (competitor:CCJ)",
    )


def _config() -> PipelineConfig:
    return PipelineConfig(
        tickers={
            "UUUU": {"competitor_tickers": ["CCJ", "UEC"]},
            "TEM": {},
        },
        keywords={},
        sources_config={},
        state_db=Path("/tmp/stock-news-radar-test.sqlite"),
        processed_log=Path("/tmp/stock-news-radar-test.ndjson"),
        daily_report_dir=Path("/tmp"),
    )


def test_competitor_source_routes_to_review_without_llm():
    decision, verdict = _process_item(item=_competitor_item(), config=_config())
    assert decision.tier == "REVIEW"
    assert decision.primary_ticker == "UUUU"
    assert "competitor_signal_data_collection" in decision.reasons
    # No LLM call → no verdict
    assert verdict is None


def test_competitor_gate_runs_before_candidate_match():
    """Article doesn't even need to mention UUUU — gate fires on source name alone."""
    item = _competitor_item()
    # Strip any UUUU mention from text just to be sure
    item = NewsItem(**{**item.__dict__, "raw_text": "Cameco news, no other tickers."})
    decision, verdict = _process_item(item=item, config=_config())
    assert decision.tier == "REVIEW"
    assert verdict is None
