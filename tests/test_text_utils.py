"""B3: term_present must use word boundaries for short ALL-CAPS tokens.

Without word-boundary matching, 'TEM' matches 'system'/'stem'/'Templeton' and
'UUUU' matches 'UUUUL', wasting LLM quota and creating noisy candidate sets.
"""
from __future__ import annotations

from src.text_utils import term_present


def test_short_caps_token_uses_word_boundary():
    assert term_present("TEM", "Tempus AI (NASDAQ: TEM) reports") is True
    assert term_present("TEM", "the system reports") is False
    assert term_present("TEM", "Templeton Emerging Markets") is False
    assert term_present("TEM", "system stem item") is False


def test_uuuu_word_boundary():
    assert term_present("UUUU", "(UUUU) earnings") is True
    assert term_present("UUUU", "UUUUL is different") is False


def test_long_phrase_substring():
    assert term_present("Energy Fuels", "Energy Fuels Inc. reports") is True
    assert term_present("Energy Fuels", "energy fuels inc.") is True


def test_empty_term():
    assert term_present("", "anything") is False
