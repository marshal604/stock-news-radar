"""M1: article body fetcher must fail-soft.

LLM context enrichment is best-effort. Failures (paywall, timeout, bot detection,
HTTP error, JS-only SPA) all return None so caller falls back to title+summary."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import src.article_fetcher as fetcher
from src.article_fetcher import fetch_article_body


@pytest.fixture(autouse=True)
def _clear_cache():
    """LRU cache is process-wide; clear between tests so they don't bleed."""
    fetcher.clear_cache()
    yield
    fetcher.clear_cache()


def _patch_get(monkeypatch, status_code: int, text: str = ""):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.text = text

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client.get.return_value = mock_resp

    monkeypatch.setattr(fetcher.httpx, "Client", MagicMock(return_value=mock_client))


_REAL_ARTICLE_HTML = """
<html><head><title>Energy Fuels Q1 2026 Report</title></head>
<body>
<header>nav stuff</header>
<article>
<h1>Energy Fuels Reports Strong Q1 2026 Earnings</h1>
<p>Energy Fuels Inc. (NYSE: UUUU) today announced first quarter 2026 results,
with revenue increasing 45% year-over-year driven by strong uranium and rare
earth production. The company maintained its full-year guidance of $200 million
in revenue and reiterated its commitment to expanding rare earth processing
capacity at White Mesa Mill.</p>
<p>CEO Ross Bhappu commented on the results, noting that the strategic
collaboration with the Department of Energy is progressing on schedule.</p>
</article>
<footer>copyright stuff</footer>
</body></html>
"""


def test_extracts_main_content_from_html(monkeypatch):
    _patch_get(monkeypatch, 200, _REAL_ARTICLE_HTML)
    body = fetch_article_body("https://example.com/article")
    assert body is not None
    # Main content present
    assert "Energy Fuels Inc" in body
    assert "Ross Bhappu" in body
    # Boilerplate stripped (readability + bs4 should drop nav/footer)
    assert "nav stuff" not in body
    assert "copyright stuff" not in body


def test_404_returns_none(monkeypatch):
    _patch_get(monkeypatch, 404)
    assert fetch_article_body("https://example.com/missing") is None


def test_short_paywall_stub_returns_none(monkeypatch):
    """Subscribe-to-read interstitials are usually < 200 chars of meaningful text."""
    paywall = "<html><body><h1>Subscribe to read</h1><p>Sign up for $5/month</p></body></html>"
    _patch_get(monkeypatch, 200, paywall)
    assert fetch_article_body("https://example.com/paywalled") is None


def test_empty_response_returns_none(monkeypatch):
    _patch_get(monkeypatch, 200, "")
    assert fetch_article_body("https://example.com/empty") is None


def test_invalid_url_returns_none():
    assert fetch_article_body("") is None
    assert fetch_article_body("not-a-url") is None
    assert fetch_article_body("ftp://example.com/x") is None


def test_network_error_returns_none(monkeypatch):
    """Timeout or connection error must not crash the pipeline."""
    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.__exit__.return_value = False
    mock_client.get.side_effect = TimeoutError("simulated")

    monkeypatch.setattr(fetcher.httpx, "Client", MagicMock(return_value=mock_client))
    assert fetch_article_body("https://example.com/slow") is None


def test_lru_cache_dedupes_same_url(monkeypatch):
    """Same URL across multiple Google News queries should fetch only once."""
    call_count = {"n": 0}

    def mock_client_factory(**kw):
        call_count["n"] += 1
        m = MagicMock()
        m.__enter__.return_value = m
        m.__exit__.return_value = False
        m.get.return_value = MagicMock(status_code=200, text=_REAL_ARTICLE_HTML)
        return m

    monkeypatch.setattr(fetcher.httpx, "Client", mock_client_factory)

    fetch_article_body("https://example.com/same")
    fetch_article_body("https://example.com/same")
    fetch_article_body("https://example.com/same")

    assert call_count["n"] == 1, "Cache should have prevented redundant fetches"
