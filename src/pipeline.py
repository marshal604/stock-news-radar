"""6-phase pipeline orchestrator.

Phase 1 Collect      → all sources fetch
Phase 2 Dedup        → SQLite seen + freshness window
Phase 3 Substring    → LLM mention_quotes verbatim check
Phase 4 Differential → keyword path A vs LLM path B; tier from alert-rules.md
Phase 5 Self-consist → re-run LLM with auditor phrasing for HIGH and MEDIUM (A1)
Phase 6 Discord post → render + POST per tier"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .discord import DiscordPostError, format_alert, post_discord
from .oracles import (
    LLMOracleError,
    LLMVerdict,
    check_consistency,
    classify_with_llm,
    score_keywords,
    translate_title_to_chinese,
    verify_quotes,
)
from .oracles.keyword import KeywordScore
from .oracles.substring import SubstringResult
from .qc import QCLogger
from .sources import (
    EdgarSource,
    FinvizSource,
    GoogleNewsSource,
    NewsItem,
    Source,
)
from .state import SeenStore
from .text_utils import term_present

logger = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    tickers: dict
    keywords: dict
    sources_config: dict
    state_db: Path
    processed_log: Path
    daily_report_dir: Path
    dry_run: bool = False
    # User spec: "只抓當天的新聞". Implemented as 24-hour rolling window to avoid
    # the midnight-UTC edge case where a 23:55 UTC item drops at 00:05 UTC.
    max_age_hours: int = 24


@dataclass
class TierDecision:
    """Pure-function output of decide_tier(). Testable independently of LLM/IO."""

    tier: str  # CRITICAL / HIGH / MEDIUM / REVIEW / DROP
    reasons: List[str] = field(default_factory=list)
    primary_ticker: Optional[str] = None
    summary_caveat: bool = False  # B6: True when LLM summary may be hallucinated


def build_sources(sources_config: dict) -> List[Source]:
    """Construct active source adapters per sources.json (respects 'enabled' flag)."""
    active: List[Source] = []
    if sources_config.get("edgar", {}).get("enabled", True) is not False:
        active.append(EdgarSource())
    if sources_config.get("finviz", {}).get("enabled", True) is not False:
        active.append(FinvizSource())
    if sources_config.get("google_news", {}).get("enabled", True) is not False:
        queries = sources_config.get("google_news_queries", {})
        active.append(GoogleNewsSource(queries_by_ticker=queries))
    return active


def run(config: PipelineConfig) -> dict:
    """Execute pipeline. Returns summary stats."""
    sources = build_sources(config.sources_config)
    store = SeenStore(config.state_db)
    qc = QCLogger(config.processed_log, config.daily_report_dir)

    stats = {"collected": 0, "fresh": 0, "sent": 0, "review": 0, "dropped": 0}

    try:
        # Phase 1: Collect
        all_items: List[NewsItem] = []
        for ticker, meta in config.tickers.items():
            for source in sources:
                try:
                    items = source.fetch(ticker, meta)
                except Exception as e:
                    logger.warning("%s.fetch(%s) raised: %s", source.name, ticker, e)
                    continue
                all_items.extend(items)
                logger.info("collected %d from %s for %s", len(items), source.name, ticker)
        stats["collected"] = len(all_items)

        # Phase 2: Dedup + freshness filter
        cutoff = datetime.now(timezone.utc) - timedelta(hours=config.max_age_hours)
        fresh: List[NewsItem] = []
        for item in all_items:
            if item.published_at < cutoff:
                qc.log(item=item, verdict="DROP", reasons=["date_too_old"])
                stats["dropped"] += 1
                continue
            if store.is_seen(item):
                qc.log(item=item, verdict="DROP", reasons=["already_sent"])
                stats["dropped"] += 1
                continue
            fresh.append(item)
        stats["fresh"] = len(fresh)

        # Phase 3-6 per item
        for item in fresh:
            decision, verdict = _process_item(item=item, config=config)

            if decision.tier in ("CRITICAL", "HIGH", "MEDIUM"):
                _send(item, verdict, decision, qc, config, stats)
            elif decision.tier == "REVIEW":
                qc.log(
                    item=item,
                    verdict="REVIEW",
                    tier=decision.tier,
                    reasons=decision.reasons,
                    details=_verdict_details(verdict),
                )
                stats["review"] += 1
            else:  # DROP
                qc.log(
                    item=item,
                    verdict="DROP",
                    reasons=decision.reasons,
                    details=_verdict_details(verdict),
                )
                stats["dropped"] += 1

            # B1: dry_run never marks seen, so re-runs are reproducible
            if not config.dry_run:
                store.mark_seen(item)

        # Daily housekeeping (only on real runs)
        if not config.dry_run:
            store.gc_old_days(keep_days=2)
        qc.flush_daily_report()
    finally:
        qc.close()
        store.close()

    logger.info("pipeline summary: %s", stats)
    return stats


def _process_item(*, item: NewsItem, config: PipelineConfig) -> Tuple[TierDecision, Optional[LLMVerdict]]:
    """Run phases 3-5 for one item. Returns (TierDecision, primary verdict for QC details)."""
    target_tickers = _candidate_tickers(item, config)
    if not target_tickers:
        return TierDecision(tier="DROP", reasons=["no_candidate_ticker"]), None

    # B7: EDGAR fast-path runs BEFORE keyword computation. EDGAR is CIK-bound so
    # collision/exclude_strict cannot apply — saves a wasted scoring pass.
    if item.source == "edgar":
        return _critical_path(item, target_tickers, config)

    # Path A: keyword scoring (deterministic)
    keyword_results = {
        t: score_keywords(item.raw_text, t, config.keywords[t]) for t in target_tickers
    }
    collisions = [t for t, r in keyword_results.items() if r.disambiguation_collisions]
    if collisions:
        return (
            TierDecision(tier="DROP", reasons=[f"ticker_collision:{','.join(collisions)}"]),
            None,
        )
    exclude_hits = {h for r in keyword_results.values() for h in r.exclude_hits}
    if exclude_hits:
        return (
            TierDecision(tier="DROP", reasons=[f"exclude_strict_hit:{','.join(sorted(exclude_hits))}"]),
            None,
        )

    # Path B: LLM classifier (Opus)
    try:
        primary_verdict = classify_with_llm(
            tickers=target_tickers,
            url=item.url,
            title=item.title,
            raw_text=item.raw_text,
            published=item.published_at.isoformat(),
            source=item.source,
            publisher=item.publisher,
        )
    except LLMOracleError as e:
        logger.warning("LLM classify failed for %s: %s", item.url, e)
        return TierDecision(tier="REVIEW", reasons=[f"llm_error:{e}"]), None

    # Substring oracle
    substring_result = verify_quotes(primary_verdict, item.raw_text)

    # Tier decision (pure function — testable in isolation, see test_pipeline_tier_decision.py)
    decision = decide_tier(
        source_confidence=item.source_confidence,
        primary_verdict=primary_verdict,
        keyword_results=keyword_results,
        substring_result=substring_result,
    )

    # Phase 5: Self-consistency on HIGH and MEDIUM (A1: google_news is most LLM-sensitive)
    if decision.tier in ("HIGH", "MEDIUM"):
        decision = _apply_self_consistency(item, target_tickers, primary_verdict, decision)

    return decision, primary_verdict


def decide_tier(
    *,
    source_confidence: str,
    primary_verdict: LLMVerdict,
    keyword_results: Dict[str, KeywordScore],
    substring_result: SubstringResult,
) -> TierDecision:
    """Pure function: oracle outputs → tier verdict per alert-rules.md.

    Inputs:
        source_confidence: 'high' | 'medium' | 'critical'
        primary_verdict:   LLM oracle output
        keyword_results:   per-ticker KeywordScore from path A
        substring_result:  mention_quotes verbatim verification

    Returns TierDecision. EDGAR critical path is handled separately in _critical_path().
    """
    relevant = [
        t for t, rel in primary_verdict.ticker_relevance.items()
        if rel.is_relevant and rel.relevance_type != "buzzword-list-only"
    ]
    if not relevant:
        return TierDecision(tier="DROP", reasons=["llm_no_relevant_or_buzzword_only"])

    if not primary_verdict.should_alert:
        return TierDecision(
            tier="DROP",
            reasons=["llm_should_not_alert"],
            primary_ticker=relevant[0],
        )

    primary_ticker = relevant[0]

    # B5: substring partial failure (some quotes verbatim, some hallucinated) → REVIEW
    if not substring_result.ok:
        if substring_result.all_failed_for(primary_ticker):
            return TierDecision(
                tier="DROP",
                reasons=["quote_not_in_source"],
                primary_ticker=primary_ticker,
            )
        return TierDecision(
            tier="REVIEW",
            reasons=["partial_quote_failure"],
            primary_ticker=primary_ticker,
        )

    # Differential: keyword path A vs LLM path B
    kw_pass = any(keyword_results[t].passed for t in relevant if t in keyword_results)

    if source_confidence == "high":
        if kw_pass:
            return TierDecision(tier="HIGH", reasons=[], primary_ticker=primary_ticker)
        return TierDecision(
            tier="REVIEW",
            reasons=["differential_disagreement_kw_no_llm_yes"],
            primary_ticker=primary_ticker,
        )
    if source_confidence == "medium":
        if kw_pass:
            return TierDecision(tier="MEDIUM", reasons=[], primary_ticker=primary_ticker)
        return TierDecision(
            tier="REVIEW",
            reasons=["medium_source_no_keyword_match"],
            primary_ticker=primary_ticker,
        )
    return TierDecision(
        tier="DROP",
        reasons=[f"unknown_source_confidence:{source_confidence}"],
        primary_ticker=primary_ticker,
    )


def _apply_self_consistency(
    item: NewsItem,
    target_tickers: List[str],
    primary_verdict: LLMVerdict,
    decision: TierDecision,
) -> TierDecision:
    """Run auditor pass (Sonnet) and downgrade tier on disagreement.

    B2 fix: auditor LLM error is a signal, not noise — downgrade to REVIEW.
    A1 change: applies to MEDIUM as well as HIGH (google_news is more LLM-sensitive
    than ticker-feed sources)."""
    try:
        auditor_verdict = classify_with_llm(
            tickers=target_tickers,
            url=item.url,
            title=item.title,
            raw_text=item.raw_text,
            published=item.published_at.isoformat(),
            source=item.source,
            publisher=item.publisher,
            use_auditor_phrasing=True,
        )
    except LLMOracleError as e:
        logger.warning("auditor classify failed: %s — downgrade to REVIEW", e)
        return dataclasses.replace(
            decision,
            tier="REVIEW",
            reasons=decision.reasons + [f"self_consistency_inconclusive:{e}"],
        )

    cons = check_consistency(primary_verdict, auditor_verdict)
    if cons.consistent:
        return decision

    logger.info("self-consistency mismatch on %s — downgrading", item.url)
    if decision.tier == "HIGH":
        return dataclasses.replace(
            decision, tier="MEDIUM", reasons=decision.reasons + ["self_consistency_mismatch"]
        )
    # MEDIUM with inconsistency → REVIEW (keep human in the loop)
    return dataclasses.replace(
        decision, tier="REVIEW", reasons=decision.reasons + ["self_consistency_mismatch"]
    )


def _critical_path(
    item: NewsItem, tickers: List[str], config: PipelineConfig
) -> Tuple[TierDecision, Optional[LLMVerdict]]:
    """SEC EDGAR fast-path. CIK-bound, so relevance is guaranteed.

    Trust hierarchy: 8-K is the company's own legal filing — we always alert.
    Per review item #3a: LLM is used ONLY to translate the title — never to
    classify, never to summarize. Pure translation has a numeric guardrail (any
    digit in the translation must come from the title) and falls back to a safe
    template string on hallucination or LLM failure. This is more conservative
    than the previous classify-and-caveat approach: there is no path by which a
    hallucinated number reaches Discord."""
    from .oracles.schema import LLMVerdict as _Verdict, TickerRelevance

    primary_ticker = tickers[0]
    chinese_summary = translate_title_to_chinese(item.title)

    verdict = _Verdict(
        ticker_relevance={
            primary_ticker: TickerRelevance(
                is_relevant=True,
                ticker_appears_verbatim=True,
                mention_quotes=[],
                relevance_type="company-specific",
                confidence=1.0,
            )
        },
        publish_date_iso=item.published_at.isoformat(),
        sentiment="neutral",
        category="regulatory",
        should_alert=True,
        alert_tier="high",
        chinese_summary=chinese_summary,
    )
    return (
        TierDecision(
            tier="CRITICAL",
            reasons=[],
            primary_ticker=primary_ticker,
            summary_caveat=False,
        ),
        verdict,
    )


def _candidate_tickers(item: NewsItem, config: PipelineConfig) -> List[str]:
    """Determine which configured tickers might be discussed in this item.

    B3: uses term_present (word-boundary aware) to avoid 'TEM' matching 'system' or
    'Templeton'. Without this gate, every news article containing 'system' would
    burn LLM quota. Disambiguation still happens downstream via keyword oracle."""
    if item.ticker_hint and item.ticker_hint in config.tickers:
        return [item.ticker_hint]
    candidates: List[str] = []
    for ticker, meta in config.tickers.items():
        aliases = (
            [ticker]
            + list(meta.get("ticker_aliases", []))
            + list(meta.get("company_aliases", []))
        )
        if any(term_present(alias, item.raw_text) for alias in aliases):
            candidates.append(ticker)
    return candidates


def _verdict_details(verdict: Optional[LLMVerdict]) -> dict:
    if verdict is None:
        return {}
    return {
        "should_alert": verdict.should_alert,
        "alert_tier": verdict.alert_tier,
        "sentiment": verdict.sentiment,
        "category": verdict.category,
        "ticker_relevance": {
            t: {
                "is_relevant": r.is_relevant,
                "relevance_type": r.relevance_type,
                "confidence": r.confidence,
            }
            for t, r in verdict.ticker_relevance.items()
        },
    }


def _send(
    item: NewsItem,
    verdict: Optional[LLMVerdict],
    decision: TierDecision,
    qc: QCLogger,
    config: PipelineConfig,
    stats: dict,
) -> None:
    if verdict is None or decision.primary_ticker is None:
        logger.error("_send called without verdict/primary_ticker; tier=%s", decision.tier)
        return
    content = format_alert(
        tier=decision.tier,
        item=item,
        verdict=verdict,
        primary_ticker=decision.primary_ticker,
        summary_caveat=decision.summary_caveat,
    )
    if config.dry_run:
        logger.info("[DRY RUN] would post:\n%s\n", content)
        qc.log(
            item=item,
            verdict="DRY_RUN_SENT",
            tier=decision.tier,
            reasons=decision.reasons,
            details=_verdict_details(verdict),
        )
        stats["sent"] += 1
        return

    try:
        post_discord(content)
        qc.log(
            item=item,
            verdict="SENT",
            tier=decision.tier,
            reasons=decision.reasons,
            details=_verdict_details(verdict),
        )
        stats["sent"] += 1
    except DiscordPostError as e:
        logger.error("discord post failed: %s", e)
        qc.log(
            item=item,
            verdict="DISCORD_FAIL",
            tier=decision.tier,
            reasons=decision.reasons + [str(e)],
            details=_verdict_details(verdict),
        )
