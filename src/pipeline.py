"""6-phase pipeline orchestrator.

Phase 1 Collect      → all sources fetch
Phase 2 Dedup        → SQLite seen + date='today' filter
Phase 3 Substring    → LLM mention_quotes verbatim check (CRITICAL gate)
Phase 4 Differential → keyword path A vs LLM path B; tier from alert-rules.md
Phase 5 Self-consist → re-run LLM with auditor phrasing for HIGH tier
Phase 6 Discord post → render + POST per tier"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional

from .discord import DiscordPostError, format_alert, post_discord
from .oracles import (
    LLMOracleError,
    LLMVerdict,
    check_consistency,
    classify_with_llm,
    score_keywords,
    verify_quotes,
)
from .oracles.llm import SECONDARY_MODEL
from .qc import QCLogger
from .sources import (
    EdgarSource,
    FinvizSource,
    GoogleNewsSource,
    NewsItem,
    Source,
)
from .state import SeenStore

logger = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    tickers: dict
    keywords: dict
    sources_config: dict
    state_db: Path
    processed_log: Path
    daily_report: Path
    dry_run: bool = False
    # User spec: "只抓當天的新聞". Implemented as 24-hour rolling window to avoid
    # the midnight-UTC edge case where a 23:55 UTC item drops at 00:05 UTC.
    max_age_hours: int = 24


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
    qc = QCLogger(config.processed_log, config.daily_report)

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

        # Phase 2: Dedup + freshness filter (24h rolling window per max_age_hours)
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
            tier, reasons, verdict, primary_ticker = _process_item(
                item=item,
                config=config,
            )
            if tier in ("CRITICAL", "HIGH", "MEDIUM"):
                _send(item, verdict, primary_ticker, tier, qc, config, stats)
                store.mark_seen(item)
            elif tier == "REVIEW":
                qc.log(
                    item=item,
                    verdict="REVIEW",
                    tier=tier,
                    reasons=reasons,
                    details=_verdict_details(verdict),
                )
                stats["review"] += 1
                store.mark_seen(item)
            else:
                qc.log(
                    item=item,
                    verdict="DROP",
                    reasons=reasons,
                    details=_verdict_details(verdict),
                )
                stats["dropped"] += 1
                store.mark_seen(item)

        # Daily housekeeping
        store.gc_old_days(keep_days=2)
        qc.flush_daily_report()
    finally:
        qc.close()
        store.close()

    logger.info("pipeline summary: %s", stats)
    return stats


def _process_item(*, item: NewsItem, config: PipelineConfig):
    """Run phases 3-5 for one item. Returns (tier, reasons, verdict, primary_ticker)."""
    target_tickers = _candidate_tickers(item, config)
    if not target_tickers:
        return "DROP", ["no_candidate_ticker"], None, None

    # Phase 4 path A: keyword scoring (deterministic, fast)
    keyword_results = {
        t: score_keywords(item.raw_text, t, config.keywords[t])
        for t in target_tickers
    }
    any_collision = [
        t for t, r in keyword_results.items() if r.disambiguation_collisions
    ]
    if any_collision:
        return (
            "DROP",
            [f"ticker_collision:{','.join(any_collision)}"],
            None,
            None,
        )
    any_exclude = any(r.exclude_hits for r in keyword_results.values())
    if any_exclude:
        return (
            "DROP",
            [
                "exclude_strict_hit:"
                + ",".join({h for r in keyword_results.values() for h in r.exclude_hits})
            ],
            None,
            None,
        )

    # CRITICAL fast-path for SEC EDGAR — bypass LLM relevance, only need substring sanity
    if item.source == "edgar":
        return _critical_path(item, target_tickers, config)

    # Phase 4 path B: LLM classifier
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
        return "REVIEW", [f"llm_error:{e}"], None, None

    # Phase 3: substring oracle
    sub = verify_quotes(primary_verdict, item.raw_text)
    if not sub.ok:
        return (
            "DROP",
            ["quote_not_in_source"],
            primary_verdict,
            None,
        )

    # Tier determination per alert-rules.md
    relevant_tickers = [
        t
        for t, rel in primary_verdict.ticker_relevance.items()
        if rel.is_relevant and rel.relevance_type != "buzzword-list-only"
    ]
    if not relevant_tickers:
        return "DROP", ["llm_no_relevant_or_buzzword_only"], primary_verdict, None

    if not primary_verdict.should_alert:
        return "DROP", ["llm_should_not_alert"], primary_verdict, None

    primary_ticker = relevant_tickers[0]

    # Differential: keyword path agreement
    kw_pass_for_relevant = any(keyword_results[t].passed for t in relevant_tickers)

    if item.source_confidence == "high":
        if kw_pass_for_relevant:
            tier = "HIGH"
        else:
            return (
                "REVIEW",
                ["differential_disagreement_kw_no_llm_yes"],
                primary_verdict,
                primary_ticker,
            )
    elif item.source_confidence == "medium":
        if kw_pass_for_relevant:
            tier = "MEDIUM"
        else:
            return (
                "REVIEW",
                ["medium_source_no_keyword_match"],
                primary_verdict,
                primary_ticker,
            )
    else:
        return "DROP", ["unknown_source_confidence"], primary_verdict, primary_ticker

    # Phase 5: self-consistency for HIGH tier
    if tier == "HIGH":
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
            cons = check_consistency(primary_verdict, auditor_verdict)
            if not cons.consistent:
                logger.info("self-consistency mismatch for %s — downgrading", item.url)
                tier = "MEDIUM"
        except LLMOracleError as e:
            logger.warning("auditor classify failed: %s — keeping HIGH", e)

    return tier, [], primary_verdict, primary_ticker


def _critical_path(item: NewsItem, tickers: list[str], config: PipelineConfig):
    """SEC EDGAR fast-path. Substring check ticker presence; LLM (Sonnet) only for translation."""
    primary_ticker = tickers[0]
    try:
        verdict = classify_with_llm(
            tickers=tickers,
            url=item.url,
            title=item.title,
            raw_text=item.raw_text,
            published=item.published_at.isoformat(),
            source=item.source,
            publisher=item.publisher,
            model=SECONDARY_MODEL,
        )
        sub = verify_quotes(verdict, item.raw_text)
        if not sub.ok:
            # 8-K critical events still alert even if LLM mis-quotes — we don't trust LLM
            # for the alert decision here. But note in QC.
            logger.warning("CRITICAL path: substring failed but proceeding: %s", sub.failed_quotes)
        return "CRITICAL", [], verdict, primary_ticker
    except LLMOracleError as e:
        logger.warning("CRITICAL path LLM failed for %s — sending raw alert: %s", item.url, e)
        # Synthesize a minimal verdict so Discord can render
        from .oracles.schema import LLMVerdict, TickerRelevance
        synthetic = LLMVerdict(
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
            chinese_summary=f"[LLM 失敗] {item.title}",
        )
        return "CRITICAL", ["llm_failed_but_critical"], synthetic, primary_ticker


def _candidate_tickers(item: NewsItem, config: PipelineConfig) -> list[str]:
    """Determine which configured tickers might be discussed in this item."""
    if item.ticker_hint and item.ticker_hint in config.tickers:
        return [item.ticker_hint]
    candidates = []
    for ticker, meta in config.tickers.items():
        for alias in [ticker] + list(meta.get("ticker_aliases", [])) + list(meta.get("company_aliases", [])):
            if alias.lower() in item.raw_text.lower():
                candidates.append(ticker)
                break
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
    verdict: LLMVerdict,
    primary_ticker: str,
    tier: str,
    qc: QCLogger,
    config: PipelineConfig,
    stats: dict,
) -> None:
    content = format_alert(
        tier=tier, item=item, verdict=verdict, primary_ticker=primary_ticker
    )
    if config.dry_run:
        logger.info("[DRY RUN] would post:\n%s\n", content)
        qc.log(
            item=item,
            verdict="DRY_RUN_SENT",
            tier=tier,
            details=_verdict_details(verdict),
        )
        stats["sent"] += 1
        return

    try:
        post_discord(content)
        qc.log(item=item, verdict="SENT", tier=tier, details=_verdict_details(verdict))
        stats["sent"] += 1
    except DiscordPostError as e:
        logger.error("discord post failed: %s", e)
        qc.log(
            item=item,
            verdict="DISCORD_FAIL",
            tier=tier,
            reasons=[str(e)],
            details=_verdict_details(verdict),
        )
