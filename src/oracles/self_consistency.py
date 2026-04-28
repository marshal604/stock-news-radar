"""Layer 3 oracle: self-consistency check across two independent LLM passes.

Cross-model differential (Opus primary vs Sonnet auditor). Consistent iff:
  - should_alert agrees
  - is_relevant agrees per ticker

`alert_tier` is intentionally NOT compared. It is an LLM-emitted hint, not ground
truth — Opus and Sonnet have different internal thresholds for high vs medium.
The real tier is derived by pipeline.decide_tier() from source confidence + kw +
substring + relevance, which is deterministic. Comparing LLM tier labels would
manufacture false disagreements (most HIGH would silently demote)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from .schema import LLMVerdict


@dataclass(frozen=True)
class ConsistencyResult:
    consistent: bool
    primary_alert: bool
    primary_tier: str       # diagnostic only; not used to compute consistent
    auditor_alert: bool
    auditor_tier: str       # diagnostic only
    tier_diff: bool         # diagnostic only — does NOT affect consistent
    relevance_diff: Dict[str, str]


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
    consistent = alert_match and not relevance_diff

    return ConsistencyResult(
        consistent=consistent,
        primary_alert=primary.should_alert,
        primary_tier=primary.alert_tier,
        auditor_alert=auditor.should_alert,
        auditor_tier=auditor.alert_tier,
        tier_diff=primary.alert_tier != auditor.alert_tier,
        relevance_diff=relevance_diff,
    )
