"""B9: deterministic tests for decide_tier(). No LLM calls.

Constructs LLMVerdict / KeywordScore / SubstringResult inputs directly and asserts
the pure function maps them to expected tier per alert-rules.md. Covers HIGH /
MEDIUM / REVIEW / DROP paths including the new partial_quote_failure (B5) and
differential_disagreement REVIEW exits.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from src.oracles.keyword import KeywordScore
from src.oracles.schema import LLMVerdict
from src.oracles.substring import SubstringResult
from src.pipeline import decide_tier


def _verdict(
    ticker: str,
    *,
    is_relevant: bool = True,
    relevance_type: str = "company-specific",
    should_alert: bool = True,
    mention_quotes: Optional[List[str]] = None,
) -> LLMVerdict:
    return LLMVerdict.model_validate(
        {
            "ticker_relevance": {
                ticker: {
                    "is_relevant": is_relevant,
                    "ticker_appears_verbatim": True,
                    "mention_quotes": mention_quotes or [],
                    "relevance_type": relevance_type,
                    "confidence": 0.9,
                }
            },
            "publish_date_iso": "2026-04-28T08:00:00Z",
            "sentiment": "bullish",
            "category": "earnings",
            "should_alert": should_alert,
            "alert_tier": "high",
            "chinese_summary": "test",
        }
    )


def _sub(
    ok: bool = True,
    *,
    total: Optional[Dict[str, int]] = None,
    failed: Optional[Dict[str, List[str]]] = None,
) -> SubstringResult:
    return SubstringResult(
        ok=ok,
        failed_quotes=failed or {},
        ticker_in_source={},
        total_quotes=total or {},
    )


def _kw(ticker: str, passed: bool) -> KeywordScore:
    return KeywordScore(ticker=ticker, passed=passed, score=1.0 if passed else 0.0)


# ── HIGH path ────────────────────────────────────────────────────────────


def test_high_tier_when_kw_and_llm_agree():
    d = decide_tier(
        source_confidence="high",
        primary_verdict=_verdict("UUUU"),
        keyword_results={"UUUU": _kw("UUUU", True)},
        substring_result=_sub(),
    )
    assert d.tier == "HIGH"
    assert d.primary_ticker == "UUUU"
    assert d.reasons == []


# ── MEDIUM path ──────────────────────────────────────────────────────────


def test_medium_tier_for_keyword_source():
    d = decide_tier(
        source_confidence="medium",
        primary_verdict=_verdict("TEM"),
        keyword_results={"TEM": _kw("TEM", True)},
        substring_result=_sub(),
    )
    assert d.tier == "MEDIUM"


# ── REVIEW paths ─────────────────────────────────────────────────────────


def test_review_high_source_kw_disagrees():
    """Differential disagreement: high source, kw=no, LLM=yes → REVIEW."""
    d = decide_tier(
        source_confidence="high",
        primary_verdict=_verdict("UUUU"),
        keyword_results={"UUUU": _kw("UUUU", False)},
        substring_result=_sub(),
    )
    assert d.tier == "REVIEW"
    assert any("differential_disagreement" in r for r in d.reasons)


def test_review_medium_source_kw_disagrees():
    d = decide_tier(
        source_confidence="medium",
        primary_verdict=_verdict("TEM"),
        keyword_results={"TEM": _kw("TEM", False)},
        substring_result=_sub(),
    )
    assert d.tier == "REVIEW"
    assert "medium_source_no_keyword_match" in d.reasons


def test_review_partial_quote_failure(): # B5
    """Some mention_quotes verbatim, some hallucinated → REVIEW not DROP."""
    d = decide_tier(
        source_confidence="high",
        primary_verdict=_verdict("UUUU", mention_quotes=["good", "fake"]),
        keyword_results={"UUUU": _kw("UUUU", True)},
        substring_result=_sub(
            ok=False,
            total={"UUUU": 2},
            failed={"UUUU": ["fake"]},  # 1 of 2 hallucinated
        ),
    )
    assert d.tier == "REVIEW"
    assert "partial_quote_failure" in d.reasons


# ── DROP paths ───────────────────────────────────────────────────────────


def test_drop_when_buzzword_list_only():
    d = decide_tier(
        source_confidence="high",
        primary_verdict=_verdict("UUUU", relevance_type="buzzword-list-only", should_alert=False),
        keyword_results={"UUUU": _kw("UUUU", True)},
        substring_result=_sub(),
    )
    assert d.tier == "DROP"


def test_drop_when_llm_says_dont_alert_suspicious():
    """company-specific + should_alert=false is the schema-gap pattern.
    Both reasons should fire so QC counter tracks the suspicious sub-case."""
    d = decide_tier(
        source_confidence="high",
        primary_verdict=_verdict("UUUU", should_alert=False),  # default relevance_type=company-specific
        keyword_results={"UUUU": _kw("UUUU", True)},
        substring_result=_sub(),
    )
    assert d.tier == "DROP"
    assert "llm_should_not_alert" in d.reasons
    assert "schema_gap_suspicious_veto" in d.reasons


def test_drop_when_llm_says_dont_alert_legitimate():
    """macro-tangential + should_alert=false is not suspicious — LLM has a clean
    schema bucket to express 'not really about this ticker'."""
    d = decide_tier(
        source_confidence="high",
        primary_verdict=_verdict("UUUU", relevance_type="macro-tangential", should_alert=False),
        keyword_results={"UUUU": _kw("UUUU", True)},
        substring_result=_sub(),
    )
    assert d.tier == "DROP"
    # macro-tangential filters to llm_no_relevant_or_buzzword_only path,
    # before reaching should_alert. Either way, suspicious flag must NOT fire.
    assert "schema_gap_suspicious_veto" not in d.reasons


def test_drop_total_quote_failure():
    """All quotes hallucinated → DROP (distinguished from partial)."""
    d = decide_tier(
        source_confidence="high",
        primary_verdict=_verdict("UUUU", mention_quotes=["fake1", "fake2"]),
        keyword_results={"UUUU": _kw("UUUU", True)},
        substring_result=_sub(
            ok=False,
            total={"UUUU": 2},
            failed={"UUUU": ["fake1", "fake2"]},
        ),
    )
    assert d.tier == "DROP"
    assert "quote_not_in_source" in d.reasons


def test_drop_no_relevant_ticker():
    d = decide_tier(
        source_confidence="high",
        primary_verdict=_verdict("UUUU", is_relevant=False, relevance_type="macro-tangential"),
        keyword_results={"UUUU": _kw("UUUU", True)},
        substring_result=_sub(),
    )
    assert d.tier == "DROP"
    assert "llm_no_relevant_or_buzzword_only" in d.reasons
