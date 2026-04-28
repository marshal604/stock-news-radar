"""Layer 1 oracle: verify LLM-supplied mention_quotes are verbatim substrings of source.

This is the magenta-key check. If LLM hallucinates a quote (paraphrases or invents),
we catch it deterministically here. CRITICAL gate — failure means do not alert."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from ..text_utils import term_present
from .schema import LLMVerdict


@dataclass(frozen=True)
class SubstringResult:
    ok: bool
    failed_quotes: Dict[str, List[str]]      # ticker -> quotes not found in source
    ticker_in_source: Dict[str, bool]        # ticker symbol verbatim presence
    total_quotes: Dict[str, int]             # ticker -> total quote count submitted

    def all_failed_for(self, ticker: str) -> bool:
        """True only if the ticker had quotes AND every one of them hallucinated.

        Used by pipeline to distinguish 'partial_quote_failure' (REVIEW) from
        'total_quote_failure' (DROP)."""
        total = self.total_quotes.get(ticker, 0)
        if total == 0:
            return False
        return len(self.failed_quotes.get(ticker, [])) == total


def verify_quotes(verdict: LLMVerdict, raw_text: str) -> SubstringResult:
    """Return ok=True only if every mention_quote is a substring of raw_text."""
    norm_source = _normalize_ws(raw_text)
    failed: Dict[str, List[str]] = {}
    ticker_in_source: Dict[str, bool] = {}
    total_quotes: Dict[str, int] = {}

    for ticker, relevance in verdict.ticker_relevance.items():
        ticker_in_source[ticker] = term_present(ticker, norm_source)
        non_empty_quotes = [q for q in relevance.mention_quotes if q]
        total_quotes[ticker] = len(non_empty_quotes)

        bad: List[str] = []
        for quote in non_empty_quotes:
            if _normalize_ws(quote) not in norm_source:
                bad.append(quote)
        if bad:
            failed[ticker] = bad

    return SubstringResult(
        ok=not failed,
        failed_quotes=failed,
        ticker_in_source=ticker_in_source,
        total_quotes=total_quotes,
    )


def _normalize_ws(s: str) -> str:
    return " ".join(s.split())
