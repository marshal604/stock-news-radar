"""Items demoted to REVIEW because we couldn't analyze them must not block
a body-rich re-publication of the same story.

Failure mode this guards: Google News surfaces an MSN re-syndication
(title_only or aggregator gate) → REVIEW + mark_seen by both url_hash AND
title_hash. Four hours later PR Newswire publishes the original press
release with full body. Different URL → url_hash differs, but normalized
title_hash matches → is_seen returns True via the title_hash branch → DROP
'already_sent' → user never sees the body-backed alert.

Fix: mark_seen(dedup_by_title=False) writes a unique-per-url sentinel as
title_hash so only url_hash counts for dedup on these items.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.sources.base import NewsItem
from src.state import SeenStore


def _item(url: str, title: str, source: str = "google_news") -> NewsItem:
    return NewsItem(
        url=url,
        title=title,
        raw_text=title,
        published_at=datetime.now(timezone.utc),
        source=source,
        source_confidence="medium",
    )


@pytest.fixture
def store(tmp_path):
    s = SeenStore(tmp_path / "seen.sqlite")
    yield s
    s.close()


def test_normal_dedup_blocks_same_title_different_url(store):
    """Default (analyzable) behavior: title_hash dedup catches cross-source
    duplicates. Two articles with the same normalized title from different
    URLs are deduped — this is the cross-publisher dedup feature."""
    a = _item("https://msn.com/a", "Energy Fuels Reports Q1 - MSN")
    b = _item("https://prnewswire.com/b", "Energy Fuels Reports Q1 - PR Newswire")

    store.mark_seen(a)  # default: dedup_by_title=True
    assert store.is_seen(b) is True


def test_unanalyzable_review_does_not_block_body_backed_republication(store):
    """When we mark an item seen with dedup_by_title=False (the title-only
    REVIEW path), a different URL with the same title MUST pass through —
    that's the body-backed re-publication we're waiting for."""
    title_only_item = _item(
        "https://msn.com/energy-fuels-stale",
        "Energy Fuels Reports Q1 - MSN",
    )
    body_backed_item = _item(
        "https://prnewswire.com/energy-fuels-q1-results",
        "Energy Fuels Reports Q1 - PR Newswire",
        source="pr_newswire",
    )

    store.mark_seen(title_only_item, dedup_by_title=False)
    assert store.is_seen(body_backed_item) is False


def test_unanalyzable_review_still_blocks_same_url(store):
    """We must still skip the SAME URL on next run — otherwise we'd re-LLM
    every Google News redirect each cron tick. url_hash dedup still works."""
    item = _item(
        "https://msn.com/energy-fuels-stale",
        "Energy Fuels Reports Q1",
    )
    store.mark_seen(item, dedup_by_title=False)
    assert store.is_seen(item) is True
