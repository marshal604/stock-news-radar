"""Layer 3 oracle: self-consistency check via differential prompt phrasing.

Run the LLM oracle twice with different system prompts (classifier vs auditor framing).
If both converge on should_alert and alert_tier, judgment is trustworthy. Divergence
signals prompt-sensitivity rather than article-grounded reasoning → downgrade or drop."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from .schema import LLMVerdict


@dataclass(frozen=True)
class ConsistencyResult:
    consistent: bool
    primary_alert: bool
    primary_tier: str
    auditor_alert: bool
    auditor_tier: str
    relevance_diff: Dict[str, str]  # ticker -> reason if mismatch


def check_consistency(primary: LLMVerdict, auditor: LLMVerdict) -> ConsistencyResult:
    relevance_diff: Dict[str, str] = {}
    common_tickers = set(primary.ticker_relevance.keys()) & set(auditor.ticker_relevance.keys())
    for ticker in common_tickers:
        a = primary.ticker_relevance[ticker]
        b = auditor.ticker_relevance[ticker]
        if a.is_relevant != b.is_relevant:
            relevance_diff[ticker] = (
                f"primary.is_relevant={a.is_relevant} vs auditor.is_relevant={b.is_relevant}"
            )

    alert_match = primary.should_alert == auditor.should_alert
    tier_match = primary.alert_tier == auditor.alert_tier
    consistent = alert_match and tier_match and not relevance_diff

    return ConsistencyResult(
        consistent=consistent,
        primary_alert=primary.should_alert,
        primary_tier=primary.alert_tier,
        auditor_alert=auditor.should_alert,
        auditor_tier=auditor.alert_tier,
        relevance_diff=relevance_diff,
    )
