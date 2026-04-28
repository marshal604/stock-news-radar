"""Numeric guardrail blocks LLM digit hallucinations in critical-path translations.

Set-based check: every digit in the translation must come from the source. Reordering
or repetition does not matter (digits, not words)."""
from __future__ import annotations

from src.oracles.llm import numeric_guardrail_pass


def test_no_digits_either_way_passes():
    assert numeric_guardrail_pass(
        "Energy Fuels announces strategic CEO succession",
        "能源燃料宣布戰略性執行長交接",
    ) is True


def test_digit_in_source_used_in_translation_passes():
    assert numeric_guardrail_pass(
        "Energy Fuels Q1 2026 earnings call",
        "能源燃料 Q1 2026 財報電話會議",
    ) is True


def test_digit_invented_in_translation_fails():
    """LLM hallucinates $200M figure not present in source title."""
    assert numeric_guardrail_pass(
        "Energy Fuels announces capital raise",
        "能源燃料宣布 $200M 融資計畫",
    ) is False


def test_digit_dropped_in_translation_passes():
    """Set semantics: missing digits OK; only invented digits fail."""
    assert numeric_guardrail_pass(
        "Energy Fuels reports Q1 2026 results on May 5",
        "能源燃料公佈財報",
    ) is True


def test_subset_check_is_per_digit_not_per_number():
    """'200' shares digits {2,0} with '20'. Both 2 and 0 appear in source so pass.

    This is the conservative trade-off: we accept some false negatives (won't catch
    every '2 → 200' hallucination) in exchange for zero false positives on genuine
    re-orderings ('Q1 2026' ↔ '2026 Q1')."""
    assert numeric_guardrail_pass("$20 dividend", "$200 dividend") is True
