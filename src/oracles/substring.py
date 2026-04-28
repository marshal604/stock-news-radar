"""Layer 1 oracle: verify LLM-supplied mention_quotes are verbatim substrings of source.

This is the magenta-key check. If LLM hallucinates a quote (paraphrases or invents),
we catch it deterministically here. CRITICAL gate — failure means do not alert."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from .schema import LLMVerdict


@dataclass(frozen=True)
class SubstringResult:
    ok: bool
    failed_quotes: Dict[str, List[str]]  # ticker -> list of quotes not found in source
    ticker_in_source: Dict[str, bool]    # ticker symbol verbatim presence in raw_text


def verify_quotes(verdict: LLMVerdict, raw_text: str) -> SubstringResult:
    """Return ok=True only if every mention_quote is a substring of raw_text.

    Also independently checks whether the ticker symbol appears verbatim. The LLM's
    `ticker_appears_verbatim` flag is cross-checked against this."""
    # Normalize whitespace for both source and quotes — LLM may collapse whitespace.
    norm_source = _normalize_ws(raw_text)
    failed: Dict[str, List[str]] = {}
    ticker_in_source: Dict[str, bool] = {}

    for ticker, relevance in verdict.ticker_relevance.items():
        ticker_in_source[ticker] = _ticker_present(ticker, norm_source)

        bad: List[str] = []
        for quote in relevance.mention_quotes:
            if not quote:
                continue
            if _normalize_ws(quote) not in norm_source:
                bad.append(quote)
        if bad:
            failed[ticker] = bad

    ok = not failed
    return SubstringResult(ok=ok, failed_quotes=failed, ticker_in_source=ticker_in_source)


def _normalize_ws(s: str) -> str:
    return " ".join(s.split())


def _ticker_present(ticker: str, normalized_source: str) -> bool:
    """Check if ticker token appears as a standalone word."""
    upper = ticker.upper()
    src_upper = normalized_source.upper()
    if upper not in src_upper:
        return False
    # Word-boundary check: ensure not part of a longer token (e.g. UUUUL or TEMPLE)
    idx = src_upper.find(upper)
    while idx != -1:
        before_ok = idx == 0 or not src_upper[idx - 1].isalnum()
        end = idx + len(upper)
        after_ok = end == len(src_upper) or not src_upper[end].isalnum()
        if before_ok and after_ok:
            return True
        idx = src_upper.find(upper, idx + 1)
    return False
