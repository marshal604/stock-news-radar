"""Local verification: run real historical NewsItems through current pipeline.

Burns ~10 LLM calls but exhaustively covers the decision matrix:
  - Material news (earnings beat, exec change, M&A, FDA, milestone) → SENT
  - Routine governance (proxy, scheduled call, 13F, IR conference) → DROP
  - Buzzword lists (Top 10, Investor Radars) → DROP via keyword gate
  - Competitor articles (Cameco, ILMN) → DROP via must_have_one_of
  - Body-fetchable (Quartr, Business Wire) vs body-unfetchable (MSN SPA)

Cases sourced from items that actually surfaced in production runs over the
past day. Run before committing prompt/schema changes to confirm the matrix
hasn't regressed.

Usage:
    PYTHONPATH=. .venv/bin/python scripts/local_verify.py
"""
from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format="%(message)s")  # quiet httpx noise

from src.pipeline import PipelineConfig, _process_item
from src.qc import QCLogger
from src.sources.base import NewsItem
from src.sources.google_news import decode_google_news_url

REPO = Path(__file__).resolve().parent.parent
with open(REPO / "config" / "tickers.json") as f:
    TICKERS = json.load(f)
with open(REPO / "config" / "keywords.json") as f:
    KEYWORDS = json.load(f)
    KEYWORDS.pop("_comment", None)
with open(REPO / "config" / "sources.json") as f:
    SOURCES = json.load(f)
    SOURCES.pop("_comment", None)


def _cfg() -> PipelineConfig:
    return PipelineConfig(
        tickers=TICKERS,
        keywords=KEYWORDS,
        sources_config=SOURCES,
        state_db=Path("/tmp/local_verify.sqlite"),
        processed_log_dir=Path("/tmp"),
        daily_report_dir=Path("/tmp"),
        dry_run=True,
    )


# Matrix of historical / canonical cases. (id, expected_outcome, item)
# expected_outcome:
#   "SEND"           — should reach Discord (HIGH/MEDIUM tier)
#   "DROP_keyword"   — should DROP at keyword gate (no LLM call)
#   "DROP_routine"   — should DROP because LLM judges as routine
CASES = [
    # ── Material news (expect SEND) ───────────────────────────────────
    (
        "uuuu-ceo-change",
        "SEND",
        NewsItem(
            url="https://www.prnewswire.com/news-releases/ross-bhappu-ceo-energy-fuels",
            title="Ross Bhappu to Take Over as CEO of Energy Fuels",
            raw_text=(
                "Ross Bhappu to Take Over as CEO of Energy Fuels\n\n"
                "Energy Fuels Inc. (NYSE: UUUU) announced today that the Board of Directors "
                "appointed Ross Bhappu, an industry veteran with three decades of mining and "
                "metals experience, as Chief Executive Officer effective May 1, 2026. He "
                "succeeds Mark Chalmers, who is retiring after eight years leading the company "
                "through its uranium-and-rare-earth diversification."
            ),
            published_at=datetime(2026, 4, 28, 13, 0, tzinfo=timezone.utc),
            source="finviz",
            source_confidence="high",
            ticker_hint="UUUU",
            publisher="PR Newswire",
        ),
    ),
    (
        "tem-merck-partnership",
        "SEND",
        NewsItem(
            url="https://www.businesswire.com/news/tempus-merck-precision-medicine-2026",
            title="Tempus Announces Strategic Collaboration with Merck to Accelerate AI-Driven Precision Medicine",
            raw_text=(
                "Tempus Announces Strategic Collaboration with Merck to Accelerate AI-Driven Precision Medicine\n\n"
                "Tempus AI, Inc. (NASDAQ: TEM) today announced a multi-year strategic "
                "collaboration with Merck & Co. The agreement, valued at up to $200 million "
                "in milestone payments plus royalties, covers five oncology programs spanning "
                "targeted therapy and immunotherapy. Tempus will leverage its real-world "
                "genomic data and AI models to identify patient cohorts and biomarkers."
            ),
            published_at=datetime(2026, 4, 28, 13, 30, tzinfo=timezone.utc),
            source="finviz",
            source_confidence="high",
            ticker_hint="TEM",
            publisher="Business Wire",
        ),
    ),
    (
        "uuuu-rare-earth-production-first",
        "SEND",
        NewsItem(
            url="https://www.prnewswire.com/uuuu-heavy-rare-earth-2026",
            title="Energy Fuels Announces First U.S. Primary Production of Critical 'Heavy' Rare Earth Material in Decades",
            raw_text=(
                "Energy Fuels Announces First U.S. Primary Production of Critical 'Heavy' Rare Earth Material in Decades\n\n"
                "Energy Fuels Inc. (NYSE: UUUU) reported first commercial-scale production of "
                "dysprosium oxide at its White Mesa Mill, marking the first primary U.S. "
                "production of heavy rare earth material in decades. The Department of "
                "Energy and Department of Defense have identified dysprosium as critical "
                "for permanent magnets in defense and clean-energy supply chains."
            ),
            published_at=datetime(2026, 4, 28, 14, 0, tzinfo=timezone.utc),
            source="finviz",
            source_confidence="high",
            ticker_hint="UUUU",
            publisher="PR Newswire",
        ),
    ),
    # ── Routine governance (expect DROP via new rule) ─────────────────
    (
        "uuuu-q1-earnings-call-scheduled",
        "DROP_routine",
        NewsItem(
            url="https://www.prnewswire.com/uuuu-q1-2026-earnings-call",
            title="Energy Fuels Announces Call Details for Q1-2026 Earnings",
            raw_text=(
                "Energy Fuels Announces Call Details for Q1-2026 Earnings\n\n"
                "Energy Fuels Inc. (NYSE: UUUU) (TSX: EFR) announced today that it will "
                "release its first quarter 2026 financial results before market open on "
                "May 5, 2026. The Company will host a conference call at 11:00 AM ET that "
                "day to discuss the results."
            ),
            published_at=datetime(2026, 4, 28, 11, 0, tzinfo=timezone.utc),
            source="finviz",
            source_confidence="high",
            ticker_hint="UUUU",
            publisher="PR Newswire",
        ),
    ),
    (
        "tem-stake-cut-13f",
        "DROP_routine",
        NewsItem(
            url="https://www.marketbeat.com/instant-alerts/filing-tempus-ai-inc-tem-stake-cut",
            title="Tempus AI, Inc. $TEM Stake Cut by Renaissance Capital LLC",
            raw_text=(
                "Tempus AI, Inc. $TEM Stake Cut by Renaissance Capital LLC\n\n"
                "Renaissance Capital LLC reduced its position in Tempus AI, Inc. (NASDAQ: TEM) "
                "by 11.4% during the first quarter, according to its most recent 13F filing "
                "with the Securities and Exchange Commission. The institutional investor owned "
                "324,521 shares of the company's stock after selling 41,800 shares during the "
                "quarter, bringing its position to about 0.20% of Tempus AI's total holdings."
            ),
            published_at=datetime(2026, 4, 28, 11, 50, tzinfo=timezone.utc),
            source="google_news",
            source_confidence="medium",
            ticker_hint="TEM",
            publisher="MarketBeat",
        ),
    ),
    # ── Buzzword DROP (keyword gate, no LLM call) ─────────────────────
    (
        "tem-investors-radar-list",
        "DROP_keyword",
        NewsItem(
            url="https://www.benzinga.com/markets/investors-radars-today",
            title="AMD, Zeta Global, MercadoLibre, Tempus AI And Workday on Investors' Radars Today",
            raw_text=(
                "AMD, Zeta Global, MercadoLibre, Tempus AI And Workday on Investors' Radars Today\n\n"
                "Five names are catching investor attention this morning: AMD, Zeta Global, "
                "MercadoLibre, Tempus AI, and Workday."
            ),
            published_at=datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc),
            source="finviz",
            source_confidence="high",
            ticker_hint="TEM",
            publisher="Benzinga",
        ),
    ),
    (
        "uuuu-top-10-uranium-stocks",
        "DROP_keyword",
        NewsItem(
            url="https://example.com/top-10-uranium-stocks-2026",
            title="Top 10 Uranium Stocks to Buy in 2026",
            raw_text=(
                "Top 10 Uranium Stocks to Buy in 2026\n\n"
                "The uranium bull market continues. Here are the top 10 uranium stocks: "
                "CCJ, UUUU, UEC, DNN, NXE, URG, LEU, EU, FCUUF, BHP. Pick from this watchlist "
                "for diversified exposure."
            ),
            published_at=datetime(2026, 4, 28, 10, 0, tzinfo=timezone.utc),
            source="finviz",
            source_confidence="high",
            ticker_hint="UUUU",
            publisher="Synthetic",
        ),
    ),
    # ── Competitor article (expect DROP via must_have_one_of) ─────────
    (
        "uuuu-cameco-india-deal",
        "DROP_keyword",
        NewsItem(
            url="https://www.zacks.com/stock/news/cameco-india-uranium-deal",
            title="Cameco Inks $1.9B Long-Term Uranium Supply Deal With India",
            raw_text=(
                "Cameco Inks $1.9B Long-Term Uranium Supply Deal With India\n\n"
                "Cameco Corporation (NYSE: CCJ) announced a $1.9 billion uranium supply "
                "agreement with India's Department of Atomic Energy, strengthening its "
                "global commercial footprint in nuclear fuel markets."
            ),
            published_at=datetime(2026, 4, 28, 9, 0, tzinfo=timezone.utc),
            source="finviz",
            source_confidence="high",
            ticker_hint="UUUU",
            publisher="Zacks",
        ),
    ),
]


def _run_one(case_id: str, expected: str, item: NewsItem, qc: QCLogger) -> dict:
    if item.source == "google_news" and "news.google.com" in item.url:
        resolved = decode_google_news_url(item.url)
        if resolved != item.url:
            item = dataclasses.replace(item, url=resolved)
    decision, verdict, item = _process_item(item=item, config=_cfg(), qc=qc)
    return {
        "id": case_id,
        "expected": expected,
        "tier": decision.tier,
        "reasons": decision.reasons,
        "body_status": item.body_fetch_status,
        "verdict": verdict,
    }


def _classify(tier: str, reasons: list) -> str:
    if tier in ("CRITICAL", "HIGH", "MEDIUM"):
        return "SEND"
    if tier == "DROP":
        if any(r.startswith(("ticker_collision", "exclude_strict_hit")) for r in reasons):
            return "DROP_keyword"
        if "must_have" in str(reasons) or "no_candidate_ticker" in reasons:
            return "DROP_keyword"
        return "DROP_routine"
    return "REVIEW"  # REVIEW tier


qc = QCLogger(processed_log_dir=Path("/tmp"), daily_report_dir=Path("/tmp"))
results = []
try:
    for cid, expected, item in CASES:
        print(f"\n{'─'*70}\n► {cid:40s} expect={expected}")
        r = _run_one(cid, expected, item, qc)
        actual = _classify(r["tier"], r["reasons"])
        match = "✓" if actual == expected else "✗"
        print(f"  {match} actual={actual} tier={r['tier']} body={r['body_status']}")
        if r["reasons"]:
            print(f"    reasons: {r['reasons']}")
        if r["verdict"]:
            v = r["verdict"]
            print(f"    chinese : {v.chinese_summary}")
            print(f"    impact  : {v.impact_assessment}")
        results.append({"id": cid, "expected": expected, "actual": actual, "match": actual == expected})
finally:
    qc.close()

print(f"\n{'═'*70}\nMATRIX SUMMARY\n{'═'*70}")
hits = sum(1 for r in results if r["match"])
for r in results:
    print(f"  {'✓' if r['match'] else '✗'}  {r['id']:40s} expected={r['expected']:15s} actual={r['actual']}")
print(f"\n  {hits}/{len(results)} cases match expected outcome")
