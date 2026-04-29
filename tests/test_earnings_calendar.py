"""Tests for the Finnhub earnings calendar reminder."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

import src.earnings_calendar as ec
from src.earnings_calendar import EarningsEvent, fetch_upcoming, format_alert


def _patch_finnhub(monkeypatch, payload: dict, status_code: int = 200):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client.get.return_value = mock_resp

    monkeypatch.setattr(ec.httpx, "Client", MagicMock(return_value=mock_client))


# ── fetch_upcoming ───────────────────────────────────────────────────


def test_fetch_parses_finnhub_response(monkeypatch):
    _patch_finnhub(monkeypatch, {
        "earningsCalendar": [
            {
                "symbol": "TEM",
                "date": "2026-05-05",
                "hour": "bmo",
                "quarter": 1,
                "year": 2026,
                "epsEstimate": 0.45,
                "revenueEstimate": 156_000_000,
            }
        ]
    })
    events = fetch_upcoming(["TEM"], api_key="fake", today=date(2026, 4, 29))
    assert len(events) == 1
    e = events[0]
    assert e.ticker == "TEM"
    assert e.report_date == date(2026, 5, 5)
    assert e.hour == "bmo"
    assert e.quarter == 1
    assert e.eps_estimate == 0.45
    assert e.revenue_estimate == 156_000_000


def test_fetch_handles_empty_calendar(monkeypatch):
    _patch_finnhub(monkeypatch, {"earningsCalendar": []})
    assert fetch_upcoming(["TEM"], api_key="fake") == []


def test_fetch_handles_missing_estimates(monkeypatch):
    _patch_finnhub(monkeypatch, {
        "earningsCalendar": [
            {"symbol": "UUUU", "date": "2026-05-05", "quarter": 1, "year": 2026}
        ]
    })
    events = fetch_upcoming(["UUUU"], api_key="fake")
    assert len(events) == 1
    assert events[0].eps_estimate is None
    assert events[0].revenue_estimate is None
    assert events[0].hour == ""


def test_fetch_swallows_per_ticker_errors(monkeypatch):
    """One ticker failing shouldn't drop the others."""
    call_count = {"n": 0}

    def factory(**kw):
        call_count["n"] += 1
        m = MagicMock()
        m.__enter__.return_value = m
        m.__exit__.return_value = False
        if call_count["n"] == 1:
            m.get.side_effect = TimeoutError("fake timeout")
        else:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"earningsCalendar": [
                {"symbol": "TEM", "date": "2026-05-05", "quarter": 1, "year": 2026}
            ]}
            resp.raise_for_status = MagicMock()
            m.get.return_value = resp
        return m

    monkeypatch.setattr(ec.httpx, "Client", factory)
    events = fetch_upcoming(["UUUU", "TEM"], api_key="fake")
    assert len(events) == 1
    assert events[0].ticker == "TEM"


def test_fetch_handles_malformed_entry(monkeypatch):
    """Single bad entry doesn't break the rest in the same response."""
    _patch_finnhub(monkeypatch, {
        "earningsCalendar": [
            {"symbol": "TEM", "date": "not-a-date", "quarter": 1, "year": 2026},
            {"symbol": "TEM", "date": "2026-05-05", "quarter": 1, "year": 2026},
        ]
    })
    events = fetch_upcoming(["TEM"], api_key="fake")
    assert len(events) == 1
    assert events[0].report_date.isoformat() == "2026-05-05"


# ── format_alert ─────────────────────────────────────────────────────


def _event(report_date: date, **overrides) -> EarningsEvent:
    base = dict(
        ticker="TEM",
        report_date=report_date,
        hour="bmo",
        quarter=1,
        year=2026,
        eps_estimate=0.45,
        revenue_estimate=156_000_000,
    )
    base.update(overrides)
    return EarningsEvent(**base)


def test_format_today():
    e = _event(date(2026, 4, 29))
    out = format_alert(e, today=date(2026, 4, 29))
    assert "今天" in out


def test_format_tomorrow():
    e = _event(date(2026, 4, 30))
    out = format_alert(e, today=date(2026, 4, 29))
    assert "明天" in out


def test_format_n_days_out():
    e = _event(date(2026, 5, 5))
    out = format_alert(e, today=date(2026, 4, 29))
    assert "6 天後" in out


def test_format_includes_eps_and_revenue():
    e = _event(date(2026, 5, 5), eps_estimate=0.45, revenue_estimate=156_000_000)
    out = format_alert(e, today=date(2026, 4, 29))
    assert "$0.45" in out
    assert "$156.0M" in out


def test_format_revenue_in_billions():
    e = _event(date(2026, 5, 5), revenue_estimate=2_500_000_000)
    out = format_alert(e, today=date(2026, 4, 29))
    assert "$2.50B" in out


def test_format_no_estimates():
    e = _event(date(2026, 5, 5), eps_estimate=None, revenue_estimate=None)
    out = format_alert(e, today=date(2026, 4, 29))
    assert "無分析師預估" in out


def test_format_hour_label():
    e_bmo = _event(date(2026, 5, 5), hour="bmo")
    e_amc = _event(date(2026, 5, 5), hour="amc")
    e_unknown = _event(date(2026, 5, 5), hour="")
    assert "盤前" in format_alert(e_bmo, today=date(2026, 4, 29))
    assert "盤後" in format_alert(e_amc, today=date(2026, 4, 29))
    assert "時段未定" in format_alert(e_unknown, today=date(2026, 4, 29))
