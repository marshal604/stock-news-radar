"""PRWireSource: Google News RSS with site: filter for newswire publishers."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.sources.pr_newswire import PRWireSource, _collect_aliases


def test_collect_aliases_uses_company_name_and_aliases():
    aliases = _collect_aliases(
        "UUUU",
        {
            "company_name": "Energy Fuels",
            "company_aliases": ["Energy Fuels Inc", "Energy Fuels Inc."],
        },
    )
    assert aliases == ["Energy Fuels", "Energy Fuels Inc", "Energy Fuels Inc."]


def test_collect_aliases_dedups():
    aliases = _collect_aliases(
        "X",
        {
            "company_name": "Acme",
            "company_aliases": ["Acme", "Acme Corp"],
        },
    )
    assert aliases == ["Acme", "Acme Corp"]


def test_collect_aliases_skips_when_empty():
    aliases = _collect_aliases("X", {})
    assert aliases == []


SAMPLE_RSS = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Energy Fuels Reports Q1 2026 Results - GlobeNewswire</title>
      <link>https://news.google.com/rss/articles/CBMi-encoded-redirect-link</link>
      <pubDate>Wed, 30 Apr 2026 06:03:00 GMT</pubDate>
      <description>Energy Fuels Inc. (NYSE: UUUU) today reported first quarter 2026 results.</description>
      <source url="https://www.globenewswire.com">GlobeNewswire</source>
    </item>
  </channel>
</rss>"""


def test_fetch_returns_news_items_without_decoding_url():
    """pr_newswire keeps the Google News redirect URL at fetch time;
    the pipeline decodes lazily after freshness + cap, just like google_news."""
    src = PRWireSource()
    mock_resp = MagicMock()
    mock_resp.content = SAMPLE_RSS
    mock_resp.raise_for_status = MagicMock()

    with patch("src.sources.pr_newswire.httpx.get", return_value=mock_resp):
        items = src.fetch(
            "UUUU",
            {"company_name": "Energy Fuels", "company_aliases": ["Energy Fuels Inc"]},
        )

    assert len(items) == 1
    item = items[0]
    assert item.source == "pr_newswire"
    assert item.source_confidence == "high"
    assert item.ticker_hint == "UUUU"
    # URL is the unresolved Google News redirect — pipeline decodes later.
    assert "news.google.com" in item.url
    assert item.publisher == "GlobeNewswire"
    assert item.published_at.tzinfo is not None


def test_fetch_returns_empty_when_no_aliases():
    src = PRWireSource()
    items = src.fetch("XYZ", {})
    assert items == []
