"""End-to-end of temporal harness on the Discord-render side.

format_alert receives a resolved event_date_iso (set by pipeline after
bounds-validating LLM index). Tests pin tag rendering for each temporal
class, including the no-event-date fallback (no tag prepended).

Also locks in pipeline._resolve_event_date bounds-check semantics: out-of-
range index → None (silent drop with QC counter), index-with-empty-list →
same. Fail loud via QC anomaly counter, never with a phantom date in the
alert."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.discord import format_alert
from src.oracles.date_extract import DateCandidate
from src.oracles.schema import LLMVerdict, TickerRelevance
from src.pipeline import _resolve_event_date
from src.qc import QCLogger
from src.sources.base import NewsItem


def _verdict() -> LLMVerdict:
    return LLMVerdict(
        ticker_relevance={
            "TEM": TickerRelevance(
                is_relevant=True,
                ticker_appears_verbatim=True,
                mention_quotes=["Tempus AI"],
                relevance_type="company-specific",
                confidence=0.9,
            )
        },
        publish_date_iso="2026-04-30T23:11:00Z",
        sentiment="bearish",
        category="regulatory",
        should_alert=True,
        alert_tier="medium",
        chinese_summary="CEO 售股摘要",
        impact_assessment="利空中度 — CEO 大額減持引發信心疑慮",
    )


def _item(*, body_status: str = "complete") -> NewsItem:
    return NewsItem(
        url="https://example.com/insider",
        title="Insider Selling: Tempus AI CEO Sells 166,250 Shares",
        raw_text="...",
        published_at=datetime(2026, 4, 30, 23, 11, 0, tzinfo=timezone.utc),
        source="finviz",
        source_confidence="high",
        ticker_hint="TEM",
        publisher="MarketBeat",
        body_fetch_status=body_status,
    )


# ── format_alert: temporal tag rendering ─────────────────────────────────


def test_no_event_date_means_no_tag():
    msg = format_alert(
        tier="MEDIUM",
        item=_item(),
        verdict=_verdict(),
        primary_ticker="TEM",
        event_date_iso=None,
    )
    assert "回顧" not in msg
    assert "陳舊" not in msg


def test_breaking_same_day_no_tag():
    """Event date == publish date is breaking; tag would be noise."""
    msg = format_alert(
        tier="MEDIUM",
        item=_item(),
        verdict=_verdict(),
        primary_ticker="TEM",
        event_date_iso="2026-04-30",
    )
    assert "回顧" not in msg
    assert "陳舊" not in msg


def test_2days_is_tagged_per_user_spec():
    """Per user '只要不是當天就是冷飯': 2-day-old event still tags retrospective.
    The MarketBeat TEM Form 4 (2-day filing window) is the canonical case."""
    msg = format_alert(
        tier="MEDIUM",
        item=_item(),
        verdict=_verdict(),
        primary_ticker="TEM",
        event_date_iso="2026-04-28",
    )
    assert "📅 [回顧 2 天前事件 · 2026-04-28]" in msg


def test_1day_is_tagged():
    msg = format_alert(
        tier="MEDIUM",
        item=_item(),
        verdict=_verdict(),
        primary_ticker="TEM",
        event_date_iso="2026-04-29",
    )
    assert "📅 [回顧 1 天前事件 · 2026-04-29]" in msg


def test_retrospective_7days_tagged():
    """The TEM CEO sale case: event 7 days before publish."""
    msg = format_alert(
        tier="MEDIUM",
        item=_item(),
        verdict=_verdict(),
        primary_ticker="TEM",
        event_date_iso="2026-04-23",
    )
    assert "📅 [回顧 7 天前事件 · 2026-04-23]" in msg


def test_stale_30days_tagged():
    msg = format_alert(
        tier="MEDIUM",
        item=_item(),
        verdict=_verdict(),
        primary_ticker="TEM",
        event_date_iso="2026-03-25",
    )
    assert "📅 [陳舊 36 天前事件 · 2026-03-25]" in msg


def test_future_event_no_tag():
    """'Q2 results to be announced May 15' — scheduled, not retrospective."""
    msg = format_alert(
        tier="MEDIUM",
        item=_item(),
        verdict=_verdict(),
        primary_ticker="TEM",
        event_date_iso="2026-05-15",
    )
    assert "回顧" not in msg
    assert "陳舊" not in msg


# ── _resolve_event_date: bounds + fail-loud counter ──────────────────────


@pytest.fixture
def qc(tmp_path):
    logger = QCLogger(processed_log_dir=tmp_path, daily_report_dir=tmp_path)
    yield logger
    logger.close()


def _candidates() -> list[DateCandidate]:
    return [
        DateCandidate(iso_date="2026-04-23", surface_form="April 23, 2026", char_offset=0),
        DateCandidate(iso_date="2026-04-30", surface_form="4/30/26", char_offset=50),
    ]


def test_resolve_in_bounds_returns_iso(qc):
    iso = _resolve_event_date(0, _candidates(), "https://x.test", qc)
    assert iso == "2026-04-23"


def test_resolve_null_index_returns_none(qc):
    """LLM said null = no clear event date; correct behavior."""
    iso = _resolve_event_date(None, _candidates(), "https://x.test", qc)
    assert iso is None
    assert "anomaly:event_date_index_out_of_bounds" not in qc._counters
    assert "anomaly:event_date_index_with_empty_candidates" not in qc._counters


def test_resolve_out_of_bounds_drops_and_counts(qc):
    """LLM index >= len(candidates) — fail loud via counter, drop the field."""
    iso = _resolve_event_date(5, _candidates(), "https://x.test", qc)
    assert iso is None
    assert qc._counters["anomaly:event_date_index_out_of_bounds"] == 1


def test_resolve_index_with_empty_candidates_counts(qc):
    """LLM picked an index when no candidates were offered — also fail loud."""
    iso = _resolve_event_date(0, [], "https://x.test", qc)
    assert iso is None
    assert qc._counters["anomaly:event_date_index_with_empty_candidates"] == 1
