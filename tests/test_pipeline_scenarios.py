"""Scenario corpus runner.

Tests deterministic oracles (keyword + substring) against golden-set fixtures.
LLM oracle tests are gated behind `pytest -m llm` because they consume
subscription quota and are non-deterministic.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.oracles import classify_with_llm, score_keywords, verify_quotes


def _has_claude_cli() -> bool:
    import shutil
    return shutil.which("claude") is not None


@pytest.mark.llm
@pytest.mark.skipif(
    not _has_claude_cli(),
    reason="claude CLI not in PATH (install: npm install -g @anthropic-ai/claude-code)",
)
def test_golden_positive_llm_says_relevant(golden_positive):
    """LLM should mark every positive sample as is_relevant=true. Burns subscription quota."""
    for sample in golden_positive:
        verdict = classify_with_llm(
            tickers=[sample["ticker_target"]],
            url=sample["url"],
            title=sample["title"],
            raw_text=sample["raw_text"],
            published=datetime.now(timezone.utc).isoformat(),
            source=sample["source"],
            publisher=sample["publisher"],
        )
        rel = verdict.ticker_relevance[sample["ticker_target"]]
        assert rel.is_relevant is True, f"{sample['id']}: LLM said not relevant"
        assert rel.relevance_type == sample["expected"]["relevance_type"], (
            f"{sample['id']}: relevance_type {rel.relevance_type} != expected {sample['expected']['relevance_type']}"
        )

        sub = verify_quotes(verdict, sample["raw_text"])
        assert sub.ok, f"{sample['id']}: substring oracle failed - {sub.failed_quotes}"


@pytest.mark.llm
@pytest.mark.skipif(
    not _has_claude_cli(),
    reason="claude CLI not in PATH (install: npm install -g @anthropic-ai/claude-code)",
)
def test_golden_negative_llm_drops_or_buzzwords(golden_negative):
    """LLM should mark negatives as not relevant or buzzword-list-only."""
    for sample in golden_negative:
        verdict = classify_with_llm(
            tickers=[sample["ticker_target"]],
            url=sample["url"],
            title=sample["title"],
            raw_text=sample["raw_text"],
            published=datetime.now(timezone.utc).isoformat(),
            source=sample["source"],
            publisher=sample["publisher"],
        )
        rel = verdict.ticker_relevance[sample["ticker_target"]]
        is_dropped = (not rel.is_relevant) or rel.relevance_type == "buzzword-list-only"
        assert is_dropped, (
            f"{sample['id']}: LLM did not drop. is_relevant={rel.is_relevant}, "
            f"relevance_type={rel.relevance_type}"
        )
