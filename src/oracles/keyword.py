"""Layer 2-A oracle: deterministic keyword/regex scoring.

Path A in differential testing. High recall, lower precision. Pairs with LLM
oracle (path B). Both must agree for HIGH-tier alerts; disagreement → REVIEW."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass(frozen=True)
class KeywordScore:
    ticker: str
    passed: bool
    score: float
    must_have_hits: List[str] = field(default_factory=list)
    should_have_hits: Dict[str, List[str]] = field(default_factory=dict)
    exclude_hits: List[str] = field(default_factory=list)
    disambiguation_collisions: List[str] = field(default_factory=list)


def score_keywords(text: str, ticker: str, ticker_keywords: dict) -> KeywordScore:
    """Score `text` against keyword config for one ticker.

    Returns KeywordScore. `passed` is True iff:
      - At least one must_have_one_of term hits, AND
      - score >= min_score_to_pass after exclude penalty, AND
      - No disambiguation collision present without ticker explicit context.
    """
    must_have = ticker_keywords.get("must_have_one_of", [])
    should_have_groups = ticker_keywords.get("should_have", {})
    excludes = ticker_keywords.get("exclude_strict", [])
    disambig = ticker_keywords.get("ticker_disambiguation_required", [])
    scoring = ticker_keywords.get("scoring", {})

    must_weight = scoring.get("must_have_weight", 1.0)
    should_weight = scoring.get("should_have_weight", 0.2)
    exclude_penalty = scoring.get("exclude_penalty", -1.5)
    min_score = scoring.get("min_score_to_pass", 1.0)

    must_hits = [term for term in must_have if _ci_contains(text, term)]
    should_hits: Dict[str, List[str]] = {}
    for group, terms in should_have_groups.items():
        hits = [t for t in terms if _ci_contains(text, t)]
        if hits:
            should_hits[group] = hits

    exclude_hits = [term for term in excludes if _ci_contains(text, term)]
    collisions = [term for term in disambig if _ci_contains(text, term)]

    score = (
        must_weight * len(must_hits)
        + should_weight * sum(len(v) for v in should_hits.values())
        + exclude_penalty * len(exclude_hits)
    )

    passed = (
        len(must_hits) >= 1
        and score >= min_score
        and not collisions
    )

    return KeywordScore(
        ticker=ticker,
        passed=passed,
        score=round(score, 3),
        must_have_hits=must_hits,
        should_have_hits=should_hits,
        exclude_hits=exclude_hits,
        disambiguation_collisions=collisions,
    )


def _ci_contains(haystack: str, needle: str) -> bool:
    """Case-insensitive substring with word-boundary for short ALL-CAPS tokens."""
    if not needle:
        return False
    if needle.isupper() and len(needle) <= 5:
        # Word-boundary match for short symbol-like tokens to avoid UUUU matching UUUUL
        pattern = re.compile(r"\b" + re.escape(needle) + r"\b")
        return bool(pattern.search(haystack))
    return needle.lower() in haystack.lower()
