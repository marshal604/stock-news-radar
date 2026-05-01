"""Layer 1 of date harness: deterministic regex extractor.

Independence from LLM is the design goal — these tests pin down regex
behavior so any regression is caught before it propagates to misclassified
event dates downstream."""
from __future__ import annotations

from datetime import date

from src.oracles.date_extract import (
    DateCandidate,
    classify_temporal,
    event_lag_days,
    extract_date_candidates,
)


def _isos(text: str, year: int = 2026) -> list[str]:
    return [c.iso_date for c in extract_date_candidates(text, reference_year=year)]


# ── Surface form variants ────────────────────────────────────────────────


def test_iso_date():
    assert _isos("filed on 2026-04-23") == ["2026-04-23"]


def test_us_long_with_year():
    assert _isos("On April 23, 2026 the company sold shares.") == ["2026-04-23"]


def test_us_long_with_ordinal():
    assert _isos("Filed April 23rd, 2026 by the issuer") == ["2026-04-23"]


def test_us_short_month():
    assert _isos("Apr 23, 2026 was the trade date") == ["2026-04-23"]


def test_us_no_year_uses_reference():
    assert _isos("Effective April 23 per the filing", year=2026) == ["2026-04-23"]


def test_day_month_format():
    assert _isos("transaction on 23 April 2026") == ["2026-04-23"]


def test_numeric_slash_with_2digit_year():
    assert _isos("filed 4/23/26 with SEC") == ["2026-04-23"]


def test_numeric_slash_with_4digit_year():
    assert _isos("dated 04/23/2026") == ["2026-04-23"]


def test_numeric_dash_format():
    # 04-23-26 — but ambiguous w/ ISO-like; ensure not parsed as ISO
    assert _isos("on 04-23-26 trade") == ["2026-04-23"]


def test_numeric_no_year_uses_reference():
    assert _isos("trade 4/23 was material", year=2026) == ["2026-04-23"]


# ── Order + dedup ────────────────────────────────────────────────────────


def test_dedup_keeps_first_occurrence():
    text = "April 23, 2026 ... later referred to as 4/23/26 ..."
    cands = extract_date_candidates(text, reference_year=2026)
    assert len(cands) == 1
    assert cands[0].iso_date == "2026-04-23"
    assert cands[0].surface_form == "April 23, 2026"  # first form wins


def test_multiple_distinct_dates_in_order():
    text = "On April 23, 2026 traded; on 2026-04-30 filed; on May 1, 2026 published."
    isos = _isos(text)
    assert isos == ["2026-04-23", "2026-04-30", "2026-05-01"]


# ── Invalid / edge cases ─────────────────────────────────────────────────


def test_invalid_date_dropped():
    # Feb 30 doesn't exist
    assert _isos("Feb 30, 2026 is fake") == []


def test_no_dates_returns_empty():
    assert _isos("This article contains no date references at all.") == []


def test_year_only_not_extracted():
    # Bare year without month/day shouldn't produce a candidate
    assert _isos("In 2026, things happened.") == []


def test_post_init_rejects_bad_iso():
    """DateCandidate constructor enforces ISO validity (fail loud)."""
    import pytest
    with pytest.raises(ValueError):
        DateCandidate(iso_date="2026-13-99", surface_form="bad", char_offset=0)


# ── Temporal classification ──────────────────────────────────────────────


def test_classify_temporal_breaking_same_day():
    """Per user spec: only same-day = breaking. Anything else = 冷飯."""
    assert classify_temporal("2026-04-30", "2026-04-30T15:00:00Z") == "breaking"


def test_classify_temporal_1day_is_retrospective():
    """1 day = 冷飯 per user spec (no 'recent' bucket anymore)."""
    assert classify_temporal("2026-04-29", "2026-04-30T15:00:00Z") == "retrospective"


def test_classify_temporal_2days_is_retrospective():
    """The MarketBeat TEM Form 4 case: 2-day filing window = still 冷飯."""
    assert classify_temporal("2026-04-28", "2026-04-30T15:00:00Z") == "retrospective"


def test_classify_temporal_retrospective_7days():
    assert classify_temporal("2026-04-23", "2026-04-30T15:00:00Z") == "retrospective"


def test_classify_temporal_stale_30days():
    assert classify_temporal("2026-03-25", "2026-04-30T15:00:00Z") == "stale"


def test_classify_temporal_future_when_event_later():
    """Scheduled future event ('Q2 results to be announced May 15')."""
    assert classify_temporal("2026-05-15", "2026-04-30T15:00:00Z") == "future"


def test_event_lag_days_sign():
    assert event_lag_days("2026-04-23", "2026-04-30T15:00:00Z") == 7
    assert event_lag_days("2026-05-15", "2026-04-30T15:00:00Z") == -15
