from __future__ import annotations

import pytest

from src.oracles.schema import LLMVerdict
from src.oracles.substring import verify_quotes


def _make_verdict(ticker: str, quotes: list[str]) -> LLMVerdict:
    return LLMVerdict.model_validate(
        {
            "ticker_relevance": {
                ticker: {
                    "is_relevant": True,
                    "ticker_appears_verbatim": True,
                    "mention_quotes": quotes,
                    "relevance_type": "company-specific",
                    "confidence": 0.9,
                }
            },
            "publish_date_iso": "2026-04-28T08:00:00Z",
            "sentiment": "bullish",
            "category": "earnings",
            "should_alert": True,
            "alert_tier": "high",
            "chinese_summary": "測試摘要",
        }
    )


def test_verbatim_quote_passes():
    src = "Energy Fuels Inc. (NYSE: UUUU) reported Q1-2026 earnings."
    v = _make_verdict("UUUU", ["Energy Fuels Inc. (NYSE: UUUU) reported Q1-2026 earnings."])
    res = verify_quotes(v, src)
    assert res.ok is True
    assert res.failed_quotes == {}
    assert res.ticker_in_source["UUUU"] is True


def test_paraphrased_quote_fails():
    src = "Energy Fuels Inc. (NYSE: UUUU) reported Q1-2026 earnings."
    v = _make_verdict("UUUU", ["Energy Fuels reported Q1 earnings"])
    res = verify_quotes(v, src)
    assert res.ok is False
    assert "UUUU" in res.failed_quotes


def test_whitespace_normalization():
    src = "Energy Fuels reported  earnings\nthis morning."
    v = _make_verdict("UUUU", ["Energy Fuels reported earnings this morning"])
    res = verify_quotes(v, src)
    assert res.ok is True


def test_ticker_not_present_when_only_company_name():
    src = "Energy Fuels announces dividend."
    v = _make_verdict("UUUU", ["Energy Fuels announces dividend."])
    res = verify_quotes(v, src)
    assert res.ok is True
    assert res.ticker_in_source["UUUU"] is False


def test_ticker_word_boundary():
    src = "UUUUL is not UUUU."
    v = _make_verdict("UUUU", ["UUUUL is not UUUU."])
    res = verify_quotes(v, src)
    assert res.ticker_in_source["UUUU"] is True


def test_empty_mention_quotes():
    src = "Some text."
    v = _make_verdict("UUUU", [])
    res = verify_quotes(v, src)
    assert res.ok is True
