"""T1 data collection: pull Finviz news for each ticker's competitors.

Items are tagged with the *target* ticker as ticker_hint and routed to REVIEW
tier in the pipeline (no LLM call, no Discord post). This collects a week's
worth of competitor news in `data/processed-log.ndjson` so we can decide
data-driven whether to promote any patterns to actual alerts (T2 rule
crystallization in `competitor-impact-rules.json`).

Per harness 'production data > theoretical refinement': don't guess what's
relevant — measure."""
from __future__ import annotations

import dataclasses
import logging
from typing import List

from .base import NewsItem, Source
from .finviz import FinvizSource

logger = logging.getLogger(__name__)


class CompetitorFinvizSource(Source):
    name = "competitor_finviz"
    confidence = "medium"  # nominal; pipeline early-gates to REVIEW regardless

    def __init__(self, finviz: FinvizSource | None = None):
        self._finviz = finviz or FinvizSource()

    def fetch(self, ticker: str, ticker_meta: dict) -> List[NewsItem]:
        competitors = ticker_meta.get("competitor_tickers") or []
        if not competitors:
            return []

        out: List[NewsItem] = []
        for comp in competitors:
            try:
                comp_items = self._finviz.fetch(comp, {"ticker_aliases": [comp]})
            except Exception as e:
                logger.warning("competitor_finviz fetch %s failed: %s", comp, e)
                continue
            for item in comp_items:
                # Re-tag: this article is a SIGNAL CANDIDATE for `ticker`, even though
                # it's literally about `comp`. The pipeline reads source=='competitor_finviz'
                # and routes to REVIEW without keyword/LLM. ticker_hint records the target.
                publisher = (
                    f"{item.publisher} (competitor:{comp})" if item.publisher else f"competitor:{comp}"
                )
                out.append(
                    dataclasses.replace(
                        item,
                        source=self.name,
                        source_confidence=self.confidence,
                        ticker_hint=ticker,
                        publisher=publisher,
                    )
                )
        logger.info("competitor_finviz collected %d items for %s", len(out), ticker)
        return out
