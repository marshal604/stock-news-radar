"""N8: NewsItem must reject naive datetime at construction.
C2: title_hash must collapse cross-source title variations to the same value."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.sources.base import NewsItem, normalize_title_for_dedup


def _item(**overrides) -> NewsItem:
    base = dict(
        url="https://example.com/x",
        title="Sample title",
        raw_text="Sample title",
        published_at=datetime.now(timezone.utc),
        source="finviz",
        source_confidence="high",
    )
    base.update(overrides)
    return NewsItem(**base)


# ── N8: tzinfo guard ─────────────────────────────────────────────────────


def test_naive_datetime_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        NewsItem(
            url="https://example.com/x",
            title="t",
            raw_text="t",
            published_at=datetime.now(),  # naive
            source="finviz",
            source_confidence="high",
        )


def test_aware_datetime_accepted():
    item = _item()  # uses datetime.now(timezone.utc)
    assert item.published_at.tzinfo is not None


# ── C2: title normalization ──────────────────────────────────────────────


def test_normalize_strips_trailing_publisher():
    assert normalize_title_for_dedup("Energy Fuels Reports Q1 - Reuters") == "energy fuels reports q1"
    assert normalize_title_for_dedup("Energy Fuels Reports Q1 — 24/7 Wall St.") == "energy fuels reports q1"
    assert normalize_title_for_dedup("Tempus AI Stake Cut by Renaissance Capital LLC - MarketBeat") == "tempus ai stake cut by renaissance capital llc"


def test_normalize_strips_ellipsis():
    assert normalize_title_for_dedup("Energy Fuels reports Q1 ...") == "energy fuels reports q1"
    assert normalize_title_for_dedup("Energy Fuels reports Q1…") == "energy fuels reports q1"


def test_normalize_strips_punctuation():
    assert normalize_title_for_dedup("Q1: Earnings Beat!") == "q1 earnings beat"


def test_normalize_collapses_whitespace():
    assert normalize_title_for_dedup("Energy   Fuels\n  reports") == "energy fuels reports"


def test_cross_source_same_hash():
    """Same article from different aggregators dedupes correctly."""
    a = _item(title="Energy Fuels Reports Q1 - Reuters")
    b = _item(title="Energy Fuels reports Q1 ...", url="https://other.example.com/y")
    c = _item(title="Energy Fuels Reports Q1 — 24/7 Wall St.", url="https://third.example.com/z")
    assert a.title_hash() == b.title_hash() == c.title_hash()


def test_genuinely_different_titles_have_different_hash():
    a = _item(title="Energy Fuels Reports Q1")
    b = _item(title="Energy Fuels Announces CEO Change")
    assert a.title_hash() != b.title_hash()


def test_empty_title_safe():
    assert normalize_title_for_dedup("") == ""
