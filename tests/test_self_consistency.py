"""Item #2: tier_match should NOT affect `consistent`.

LLM-emitted alert_tier is a hint, not ground truth — Opus and Sonnet have
different internal thresholds. Real tier comes from pipeline.decide_tier(). If
we let tier_match veto consistency, most HIGH alerts would silently downgrade."""
from __future__ import annotations

from src.oracles.schema import LLMVerdict
from src.oracles.self_consistency import check_consistency


def _verdict(should_alert: bool, alert_tier: str, is_relevant: bool = True) -> LLMVerdict:
    return LLMVerdict.model_validate(
        {
            "ticker_relevance": {
                "UUUU": {
                    "is_relevant": is_relevant,
                    "ticker_appears_verbatim": True,
                    "mention_quotes": [],
                    "relevance_type": "company-specific" if is_relevant else "macro-tangential",
                    "confidence": 0.9,
                }
            },
            "publish_date_iso": "2026-04-28T08:00:00Z",
            "sentiment": "bullish",
            "category": "earnings",
            "should_alert": should_alert,
            "alert_tier": alert_tier,
            "chinese_summary": "x",
        }
    )


def test_tier_difference_alone_does_not_break_consistency():
    """Opus says high, Sonnet says medium — but both agree should_alert + is_relevant."""
    primary = _verdict(should_alert=True, alert_tier="high")
    auditor = _verdict(should_alert=True, alert_tier="medium")
    res = check_consistency(primary, auditor)
    assert res.consistent is True
    assert res.tier_diff is True  # diagnostic surfaces the difference


def test_should_alert_disagreement_breaks_consistency():
    primary = _verdict(should_alert=True, alert_tier="high")
    auditor = _verdict(should_alert=False, alert_tier="low")
    res = check_consistency(primary, auditor)
    assert res.consistent is False


def test_relevance_disagreement_breaks_consistency():
    primary = _verdict(should_alert=True, alert_tier="high", is_relevant=True)
    auditor = _verdict(should_alert=True, alert_tier="high", is_relevant=False)
    res = check_consistency(primary, auditor)
    assert res.consistent is False
    assert "UUUU" in res.relevance_diff


def test_full_agreement_consistent():
    primary = _verdict(should_alert=True, alert_tier="high")
    auditor = _verdict(should_alert=True, alert_tier="high")
    res = check_consistency(primary, auditor)
    assert res.consistent is True
    assert res.tier_diff is False
