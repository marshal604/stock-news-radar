"""FinnhubNewsSource: REST API ticker-bound news with summary in raw_text."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.sources.finnhub_news import FinnhubNewsSource


SAMPLE_RESPONSE = [
    {
        "category": "company news",
        "datetime": 1762732800,  # 2025-11-10 00:00:00 UTC
        "headline": "Energy Fuels Announces New Uranium Contract",
        "id": 12345,
        "image": "",
        "related": "UUUU",
        "source": "Reuters",
        "summary": (
            "Energy Fuels Inc. announced today that it has entered into a "
            "long-term uranium supply agreement with a major U.S. utility. "
            "The contract spans five years and represents a significant "
            "milestone for the company's commercial uranium business."
        ),
        "url": "https://www.reuters.com/business/energy/energy-fuels-contract",
    }
]


def test_fetch_parses_finnhub_response():
    src = FinnhubNewsSource(api_key="test-key")
    mock_resp = MagicMock()
    mock_resp.json = MagicMock(return_value=SAMPLE_RESPONSE)
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=mock_resp)

    with patch("src.sources.finnhub_news.httpx.Client", return_value=mock_client):
        items = src.fetch("UUUU", {})

    assert len(items) == 1
    item = items[0]
    assert item.source == "finnhub_news"
    assert item.source_confidence == "high"
    assert item.ticker_hint == "UUUU"
    assert item.publisher == "Reuters"
    assert "long-term uranium supply" in item.raw_text
    assert item.body_fetch_status == "partial"  # summary present → tagged partial
    assert item.published_at.tzinfo is not None


def test_fetch_skips_entries_without_summary_status():
    """When summary is empty, status drops to summary_only (constructor default)."""
    src = FinnhubNewsSource(api_key="test-key")
    payload = [{**SAMPLE_RESPONSE[0], "summary": ""}]
    mock_resp = MagicMock()
    mock_resp.json = MagicMock(return_value=payload)
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=mock_resp)

    with patch("src.sources.finnhub_news.httpx.Client", return_value=mock_client):
        items = src.fetch("UUUU", {})

    assert len(items) == 1
    assert items[0].body_fetch_status == "summary_only"


def test_fetch_returns_empty_on_http_error():
    src = FinnhubNewsSource(api_key="test-key")
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(side_effect=Exception("network down"))

    with patch("src.sources.finnhub_news.httpx.Client", return_value=mock_client):
        items = src.fetch("UUUU", {})

    assert items == []


def test_fetch_dedupes_by_url():
    src = FinnhubNewsSource(api_key="test-key")
    dup = [SAMPLE_RESPONSE[0], SAMPLE_RESPONSE[0]]
    mock_resp = MagicMock()
    mock_resp.json = MagicMock(return_value=dup)
    mock_resp.raise_for_status = MagicMock()
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=mock_resp)

    with patch("src.sources.finnhub_news.httpx.Client", return_value=mock_client):
        items = src.fetch("UUUU", {})

    assert len(items) == 1
