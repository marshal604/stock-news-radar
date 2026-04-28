from __future__ import annotations

import pytest

from src.oracles.keyword import score_keywords


def test_uuuu_positive_company_news(keywords_config):
    text = (
        "Energy Fuels Inc. (NYSE: UUUU) reported strong uranium production "
        "and rare earth output for Q1-2026."
    )
    r = score_keywords(text, "UUUU", keywords_config["UUUU"])
    assert r.passed is True
    assert "UUUU" in r.must_have_hits
    assert "Energy Fuels" in r.must_have_hits


def test_uuuu_buzzword_excluded(keywords_config):
    text = "Top 10 uranium stocks to buy in 2026: CCJ, UUUU, UEC, DNN."
    r = score_keywords(text, "UUUU", keywords_config["UUUU"])
    assert r.passed is False
    assert "Top 10" in r.exclude_hits


def test_tem_positive_partnership(keywords_config):
    text = "Tempus AI announced a strategic collaboration with Merck for oncology research."
    r = score_keywords(text, "TEM", keywords_config["TEM"])
    assert r.passed is True
    assert any(t in r.must_have_hits for t in ("Tempus AI",))
    assert "Merck" in r.should_have_hits.get("partners", [])


def test_tem_ticker_collision_blocked(keywords_config):
    text = "Templeton Emerging Markets Income Fund declared a quarterly distribution."
    r = score_keywords(text, "TEM", keywords_config["TEM"])
    assert r.passed is False
    assert "Templeton Emerging Markets" in r.disambiguation_collisions


def test_uuuu_competitor_news_no_must_have(keywords_config):
    """About Cameco (CCJ), not Energy Fuels — must_have_one_of fails."""
    text = "Cameco Corporation (NYSE: CCJ) inks $1.9B uranium supply deal with India."
    r = score_keywords(text, "UUUU", keywords_config["UUUU"])
    assert r.passed is False
    assert r.must_have_hits == []


def test_golden_positive_all_pass(keywords_config, golden_positive):
    """Every positive golden-set sample's keyword_pass expectation matches reality."""
    for sample in golden_positive:
        text = f"{sample['title']}\n\n{sample['raw_text']}"
        r = score_keywords(
            text, sample["ticker_target"], keywords_config[sample["ticker_target"]]
        )
        expected = sample["expected"]["keyword_pass"]
        assert r.passed is expected, (
            f"{sample['id']}: expected keyword_pass={expected} got {r.passed} "
            f"(score={r.score}, excludes={r.exclude_hits}, must={r.must_have_hits})"
        )


def test_golden_negative_all_fail(keywords_config, golden_negative):
    """Every negative golden-set sample's keyword_pass expectation matches reality."""
    for sample in golden_negative:
        text = f"{sample['title']}\n\n{sample['raw_text']}"
        r = score_keywords(
            text, sample["ticker_target"], keywords_config[sample["ticker_target"]]
        )
        expected = sample["expected"]["keyword_pass"]
        assert r.passed is expected, (
            f"{sample['id']}: expected keyword_pass={expected} got {r.passed} "
            f"(score={r.score}, excludes={r.exclude_hits}, must={r.must_have_hits}, "
            f"collisions={r.disambiguation_collisions})"
        )
