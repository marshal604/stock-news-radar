"""Shared text utilities. Word-boundary aware membership for ticker symbols.

Used by both substring oracle (post-LLM verification) and pipeline candidate
matching (pre-LLM gate). Same logic in both places — avoids drift."""
from __future__ import annotations

import re


def term_present(term: str, text: str) -> bool:
    """Case-insensitive presence check with word boundary for short ALL-CAPS tokens.

    Short uppercase tokens (≤5 chars, all alpha) are matched with \\b regex so that
    'TEM' does not collide with 'system', 'stem', 'item'. Longer phrases use plain
    case-insensitive substring."""
    if not term:
        return False
    if len(term) <= 5 and term.isupper() and term.isalpha():
        pattern = re.compile(r"\b" + re.escape(term) + r"\b")
        return bool(pattern.search(text))
    return term.lower() in text.lower()
