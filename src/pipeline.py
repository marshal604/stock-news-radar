"""6-phase pipeline orchestrator.

Phase 1 Collect      → all sources fetch (M3: source_anomaly counter on failure)
Phase 2 Dedup+Cap    → SQLite seen + freshness window + C3 max_items_per_run
Phase 3 Substring    → LLM mention_quotes verbatim check
Phase 4 Differential → keyword path A vs LLM path B; tier from alert-rules.md
Phase 5 Self-consist → re-run LLM with auditor phrasing for HIGH and MEDIUM
Phase 6 Discord post → render + POST per tier (C1: mark_seen only on success)"""
from __future__ import annotations

import dataclasses
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .article_fetcher import fetch_article_body
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
from .oracles.llm import numeric_guardrail_pass
from .oracles.substring import SubstringResult
from .qc import QCLogger
from .sources import (
    CompetitorFinvizSource,
    EdgarSource,
    FinnhubNewsSource,
    FinvizSource,
    GoogleNewsSource,
    NewsItem,
    PRWireSource,
    Source,
)
from .sources.google_news import decode_google_news_url
from .state import SeenStore
from .text_utils import term_present

logger = logging.getLogger(__name__)

# C3: source-confidence priority for the max_items_per_run cap. Critical first
# (8-K filings can't wait), then ticker-feed editorial, then keyword macro.
_SOURCE_PRIORITY = {"critical": 0, "high": 1, "medium": 2}


@dataclass
class PipelineConfig:
    tickers: dict
    keywords: dict
    sources_config: dict
    state_db: Path
    processed_log_dir: Path
    daily_report_dir: Path
    dry_run: bool = False
    # User spec: "只抓當天的新聞". Implemented as 24-hour rolling window to avoid
    # the midnight-UTC edge case where a 23:55 UTC item drops at 00:05 UTC.
    max_age_hours: int = 24
    # C3: hard ceiling on items processed per run. Worst-case LLM time per item
    # = ~540s (primary + auditor with retries); 20 items × 540s ≈ 3 hours, but
    # GH Actions kills at 10min. Cap at 20 so we always leave time for commit-back.
    # Items beyond cap stay un-marked — next run picks them up.
    max_items_per_run: int = 20


@dataclass
class TierDecision:
    """Pure-function output of decide_tier(). Testable independently of LLM/IO."""

    tier: str  # CRITICAL / HIGH / MEDIUM / REVIEW / DROP
    reasons: List[str] = field(default_factory=list)
    primary_ticker: Optional[str] = None
    summary_caveat: bool = False


def build_sources(sources_config: dict) -> List[Source]:
    """Construct active source adapters per sources.json (respects 'enabled' flag)."""
    active: List[Source] = []
    if sources_config.get("edgar", {}).get("enabled", True) is not False:
        active.append(EdgarSource())
    finviz = FinvizSource()
    if sources_config.get("finviz", {}).get("enabled", True) is not False:
        active.append(finviz)
    if sources_config.get("competitor_finviz", {}).get("enabled", False) is True:
        active.append(CompetitorFinvizSource(finviz=finviz))
    if sources_config.get("pr_newswire", {}).get("enabled", True) is not False:
        active.append(PRWireSource())
    if sources_config.get("finnhub_news", {}).get("enabled", True) is not False:
        api_key = os.getenv("FINNHUB_API_KEY", "").strip()
        if api_key:
            active.append(FinnhubNewsSource(api_key=api_key))
        else:
            logger.warning("finnhub_news enabled but FINNHUB_API_KEY not set — skipping")
    if sources_config.get("google_news", {}).get("enabled", True) is not False:
        queries = sources_config.get("google_news_queries", {})
        active.append(GoogleNewsSource(queries_by_ticker=queries))
    return active


# Aggregator publishers that re-syndicate other publishers' content with a
# refreshed pubDate. Articles from these on Google News are routed to REVIEW
# regardless of LLM verdict — the timestamp is unreliable (often weeks-old
# content with a fresh-looking date) and the SPA pages defeat body extraction.
# Keys are substring matches against publisher name OR URL host (case-insensitive).
_AGGREGATOR_DENYLIST = (
    "msn.com",
    "msn",
    "yahoo finance",
    "finance.yahoo.com",
    "news.yahoo.com",
    "aol.com",
    "247wallst.com",
    "24/7 wall st",
)


def _is_aggregator(item: NewsItem) -> bool:
    haystack = f"{item.publisher or ''} {item.url}".lower()
    return any(needle in haystack for needle in _AGGREGATOR_DENYLIST)


def run(config: PipelineConfig) -> dict:
    """Execute pipeline. Returns summary stats."""
    sources = build_sources(config.sources_config)
    store = SeenStore(config.state_db)
    qc = QCLogger(config.processed_log_dir, config.daily_report_dir)

    stats = {
        "collected": 0,
        "fresh": 0,
        "sent": 0,
        "would_send": 0,    # N4: separate counter for dry-run
        "review": 0,
        "dropped": 0,
        "deferred": 0,      # C3: items skipped due to max_items_per_run
    }

    try:
        # Phase 1: Collect (M3: source failure → counted anomaly, not silent return)
        all_items: List[NewsItem] = []
        for ticker, meta in config.tickers.items():
            for source in sources:
                try:
                    items = source.fetch(ticker, meta)
                except Exception as e:
                    logger.warning("%s.fetch(%s) raised: %s", source.name, ticker, e)
                    qc.record_source_anomaly(source.name, str(e))
                    continue
                all_items.extend(items)
                logger.info("collected %d from %s for %s", len(items), source.name, ticker)
        stats["collected"] = len(all_items)

        # Phase 2a: freshness + dedup
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

        # Phase 2b: C3 cap. Sort by source priority + recency, take first N.
        fresh.sort(
            key=lambda i: (
                _SOURCE_PRIORITY.get(i.source_confidence, 9),
                -i.published_at.timestamp(),
            )
        )
        if len(fresh) > config.max_items_per_run:
            overflow = fresh[config.max_items_per_run:]
            fresh = fresh[:config.max_items_per_run]
            logger.warning(
                "backlog: %d items deferred (max_items_per_run=%d) — picked up next run",
                len(overflow),
                config.max_items_per_run,
            )
            for item in overflow:
                # #3 starvation observability: tag reason with source_confidence
                # so daily-report shows e.g. 'reason:max_items_per_run_exceeded:medium'.
                # If medium starves while high deferred=0, priority queue is needed (v2).
                qc.log(
                    item=item,
                    verdict="DEFER",
                    reasons=[f"max_items_per_run_exceeded:{item.source_confidence}"],
                )
                # NB: do NOT mark_seen — these need to be re-processed next run
            stats["deferred"] = len(overflow)
        stats["fresh"] = len(fresh)

        # Phase 3-6 per item
        for item in fresh:
            # Resolve Google News redirect URL up-front so ALL downstream
            # consumers see the publisher URL (LLM context, body fetcher,
            # Discord display, mark_seen url_hash). Doing this inside
            # _process_item only rebinds a local — the outer loop's `item`
            # reference would stay as the redirect URL, leaking into _send.
            # Same applies to pr_newswire which also uses news.google.com
            # as transport (with site: filter for newswire publishers).
            if (
                item.source in ("google_news", "pr_newswire")
                and "news.google.com" in item.url
            ):
                resolved = decode_google_news_url(item.url)
                if resolved != item.url:
                    item = dataclasses.replace(item, url=resolved)

            decision, verdict, item = _process_item(item=item, config=config, qc=qc)

            handled = True  # default for DROP/REVIEW (decision recorded, no Discord side-effect)
            if decision.tier in ("CRITICAL", "HIGH", "MEDIUM"):
                handled = _send(item, verdict, decision, qc, config, stats)
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

            # C1: mark_seen only when item was successfully handled. Transient
            # Discord failures leave the item un-marked so the next run retries.
            # B1: dry_run never marks — re-runs are reproducible.
            if handled and not config.dry_run:
                # Items demoted to REVIEW *because* we couldn't analyze them
                # (no body, or known aggregator publisher) must not block a
                # subsequent body-rich version of the same story by title hash.
                # Use the no-title-dedup sentinel so url_hash still prevents
                # re-LLM'ing the same URL but a different URL with the same
                # title can fall through to alert.
                unanalyzable_review = decision.tier == "REVIEW" and any(
                    r == "title_only_no_body_for_analysis"
                    or r.startswith("aggregator_publisher:")
                    for r in decision.reasons
                )
                store.mark_seen(item, dedup_by_title=not unanalyzable_review)

        # Daily housekeeping (only on real runs)
        if not config.dry_run:
            store.gc_old_days(keep_days=2)
        qc.flush_daily_report()

        # #2: source fetch failures → active Discord alert. Daily-report alone is
        # passive observability; if EDGAR is stuck for hours, user might miss 4
        # 8-K filings before grepping the report. Push the failure live.
        _alert_on_source_anomalies(qc, config)
    finally:
        qc.close()
        store.close()

    logger.info("pipeline summary: %s", stats)
    return stats


def _process_item(
    *, item: NewsItem, config: PipelineConfig, qc: QCLogger
) -> Tuple[TierDecision, Optional[LLMVerdict], NewsItem]:
    """Run phases 3-5 for one item. Returns (decision, verdict, updated_item).

    `updated_item` propagates body_fetch_status (set during enrichment) back to
    the caller so _send / format_alert / mark_seen all see the same item state."""
    target_tickers = _candidate_tickers(item, config)
    if not target_tickers:
        return TierDecision(tier="DROP", reasons=["no_candidate_ticker"]), None, item

    # T1 competitor data collection: short-circuit before any LLM work.
    if item.source == "competitor_finviz":
        return (
            TierDecision(
                tier="REVIEW",
                reasons=["competitor_signal_data_collection"],
                primary_ticker=item.ticker_hint,
            ),
            None,
            item,
        )

    # Google News aggregator gate: MSN / Yahoo SPA entries have unreliable
    # timestamps (re-syndication date, not original) and routinely defeat the
    # article body fetcher. Route to REVIEW so we still capture the signal in
    # processed-log without firing a Discord alert based on a stale headline.
    # pr_newswire / finnhub_news come through other code paths and are NOT
    # subject to this gate.
    if item.source == "google_news" and _is_aggregator(item):
        return (
            TierDecision(
                tier="REVIEW",
                reasons=[f"aggregator_publisher:{(item.publisher or 'unknown')[:60]}"],
                primary_ticker=target_tickers[0],
            ),
            None,
            item,
        )

    # B7: EDGAR fast-path runs BEFORE keyword computation (CIK-bound, no collision possible)
    if item.source == "edgar":
        decision, verdict = _critical_path(item, target_tickers, qc)
        return decision, verdict, item

    # Path A: keyword scoring (deterministic)
    keyword_results = {
        t: score_keywords(item.raw_text, t, config.keywords[t]) for t in target_tickers
    }
    collisions = [t for t, r in keyword_results.items() if r.disambiguation_collisions]
    if collisions:
        return (
            TierDecision(tier="DROP", reasons=[f"ticker_collision:{','.join(collisions)}"]),
            None,
            item,
        )
    exclude_hits = {h for r in keyword_results.values() for h in r.exclude_hits}
    if exclude_hits:
        return (
            TierDecision(tier="DROP", reasons=[f"exclude_strict_hit:{','.join(sorted(exclude_hits))}"]),
            None,
            item,
        )

    # M1: enrich raw_text with article body before LLM. Returns updated item
    # with body_fetch_status set (complete/partial/title_only). Critical:
    # enriched_text must be used by verify_quotes + numeric_guardrail too so
    # LLM-quoted phrases from the body actually substring-match.
    item, enriched_text = _enrich_with_body(item)

    # Path B: LLM classifier (Opus)
    qc.record_llm_call("primary")
    try:
        primary_verdict = classify_with_llm(
            tickers=target_tickers,
            url=item.url,
            title=item.title,
            raw_text=enriched_text,
            published=item.published_at.isoformat(),
            source=item.source,
            publisher=item.publisher,
        )
    except LLMOracleError as e:
        logger.warning("LLM classify failed for %s: %s", item.url, e)
        return TierDecision(tier="REVIEW", reasons=[f"llm_error:{e}"]), None, item

    substring_result = verify_quotes(primary_verdict, enriched_text)

    # M4: numeric guardrail on classifier-emitted chinese_summary against the
    # enriched text — body-fetched numbers count as 'in source' too.
    summary_caveat = not numeric_guardrail_pass(enriched_text, primary_verdict.chinese_summary)
    if summary_caveat:
        logger.warning(
            "classifier_numeric_hallucination url=%s title=%r summary=%r",
            item.url, item.title, primary_verdict.chinese_summary,
        )

    decision = decide_tier(
        source_confidence=item.source_confidence,
        primary_verdict=primary_verdict,
        keyword_results=keyword_results,
        substring_result=substring_result,
        body_fetch_status=item.body_fetch_status,
    )

    # Inject summary_caveat + reason into the decision
    if summary_caveat and decision.tier in ("HIGH", "MEDIUM"):
        decision = dataclasses.replace(
            decision,
            summary_caveat=True,
            reasons=decision.reasons + ["classifier_numeric_hallucination"],
        )

    # Phase 5: Self-consistency on HIGH and MEDIUM
    if decision.tier in ("HIGH", "MEDIUM"):
        decision = _apply_self_consistency(item, target_tickers, primary_verdict, decision, qc)

    return decision, primary_verdict, item


def decide_tier(
    *,
    source_confidence: str,
    primary_verdict: LLMVerdict,
    keyword_results: Dict[str, KeywordScore],
    substring_result: SubstringResult,
    body_fetch_status: str = "summary_only",
) -> TierDecision:
    """Pure function: oracle outputs → tier verdict per alert-rules.md.

    Universal title-only gate: if body_fetch_status == 'title_only', the LLM
    judged the item from the headline alone. Per user spec ('我想看確實可以
    被分析的資料就好，只有標題檔，我覺得只是製造焦慮'), such items go to
    REVIEW — captured in processed-log for backup, but never push a Discord
    alert. EDGAR 8-K filings bypass this function entirely via
    _critical_path; finnhub_news ships summary in raw_text and tags 'partial'
    upfront so the source-context guard in _enrich_with_body keeps it out of
    title_only state."""
    relevant = [
        t for t, rel in primary_verdict.ticker_relevance.items()
        if rel.is_relevant and rel.relevance_type != "buzzword-list-only"
    ]
    if not relevant:
        return TierDecision(tier="DROP", reasons=["llm_no_relevant_or_buzzword_only"])

    if not primary_verdict.should_alert:
        # See alert-rules.md §schema-gap-watch
        reasons = ["llm_should_not_alert"]
        if _detect_suspicious_should_alert_veto(primary_verdict):
            reasons.append("schema_gap_suspicious_veto")
        return TierDecision(
            tier="DROP",
            reasons=reasons,
            primary_ticker=relevant[0],
        )

    primary_ticker = relevant[0]

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

    # Universal title-only gate — applies to every source. No body content
    # means LLM can't assess sentiment / impact reliably; demote to REVIEW
    # rather than fire a noise alert with '⚠️ 僅依標題判斷'.
    if body_fetch_status == "title_only":
        return TierDecision(
            tier="REVIEW",
            reasons=["title_only_no_body_for_analysis"],
            primary_ticker=primary_ticker,
        )

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
    qc: QCLogger,
) -> TierDecision:
    qc.record_llm_call("auditor")
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
    return dataclasses.replace(
        decision, tier="REVIEW", reasons=decision.reasons + ["self_consistency_mismatch"]
    )


def _critical_path(
    item: NewsItem, tickers: List[str], qc: QCLogger
) -> Tuple[TierDecision, Optional[LLMVerdict]]:
    """SEC EDGAR fast-path. CIK-bound, so relevance is guaranteed.
    LLM is used ONLY to translate the title; numeric guardrail prevents fabrication."""
    from .oracles.schema import LLMVerdict as _Verdict, TickerRelevance

    primary_ticker = tickers[0]
    qc.record_llm_call("translate")
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
        # 8-K critical path is body-less by design; impact_assessment is filled
        # with a fixed honest fallback rather than asking LLM to opine on a
        # body it doesn't have access to.
        impact_assessment="影響待原文確認 — 8-K 重大事件公告，請查 SEC filing 細節",
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


def _alert_on_source_anomalies(qc: QCLogger, config: PipelineConfig) -> None:
    """If any source fetch failed this run, post a live Discord alert.

    EDGAR especially: silent zero-fetch could cause us to miss multiple 8-K
    filings between daily-report glances. Surfacing the anomaly to the same
    channel turns 'eventual visibility' into 'immediate visibility'."""
    anomalies = {
        k.split(":", 1)[1]: v
        for k, v in qc._counters.items()
        if k.startswith("source_anomaly:")
    }
    if not anomalies:
        return
    if config.dry_run:
        logger.info("[DRY RUN] would alert on source anomalies: %s", anomalies)
        return
    summary = ", ".join(f"{src}={count}" for src, count in sorted(anomalies.items()))
    msg = f"⚠️ stock-news-radar: source fetch failures this run — {summary}"
    try:
        post_discord(msg)
    except DiscordPostError as e:
        logger.error("could not post source anomaly alert: %s", e)


def _enrich_with_body(item: NewsItem) -> Tuple[NewsItem, str]:
    """Fetch body + return (updated_item_with_status, enriched_text).

    Always returns a NewsItem with a definitive body_fetch_status (complete /
    partial / title_only) — never leaves it at the constructor 'summary_only'
    default once we've run extraction. decide_tier and format_alert downstream
    use status to apply tier caps and Discord annotations.

    Source-provided context guard: if the source already shipped body context
    (e.g. finnhub_news ships a 200-500 char summary in raw_text and tags
    'partial' upfront), don't downgrade to title_only just because the URL
    fetcher couldn't extract more. The LLM still has actionable context."""
    body, status = fetch_article_body(item.url, title=item.title)
    if status == "title_only" and item.body_fetch_status in ("partial", "complete"):
        status = item.body_fetch_status
    item = dataclasses.replace(item, body_fetch_status=status)
    if not body:
        return item, item.raw_text
    logger.info("enriched %s (%s) with %d-char body", item.url[:80], status, len(body))
    return item, f"{item.raw_text}\n\n--- ARTICLE BODY ---\n{body}"


def _detect_suspicious_should_alert_veto(verdict: LLMVerdict) -> bool:
    """See alert-rules.md §schema-gap-watch."""
    return any(
        rel.is_relevant and rel.relevance_type in ("company-specific", "sector-policy")
        for rel in verdict.ticker_relevance.values()
    )


def _candidate_tickers(item: NewsItem, config: PipelineConfig) -> List[str]:
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
        "chinese_summary": verdict.chinese_summary,  # capture for QC inspection
        "impact_assessment": verdict.impact_assessment,
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
) -> bool:
    """Render + POST. C1: returns True if the item should be marked seen.

    True  → SENT (2xx) | DRY_RUN | permanent 4xx (mark to avoid loop)
    False → transient failure (5xx/429/network after retries) — try again next run"""
    if verdict is None or decision.primary_ticker is None:
        logger.error("_send called without verdict/primary_ticker; tier=%s", decision.tier)
        return True  # nothing to retry, mark seen
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
        stats["would_send"] += 1   # N4: dry_run uses separate counter
        return True

    try:
        post_discord(content)
        qc.log(item=item, verdict="SENT", tier=decision.tier, reasons=decision.reasons,
               details=_verdict_details(verdict))
        stats["sent"] += 1
        return True
    except DiscordPostError as e:
        logger.error("discord post failed (retryable=%s): %s", e.retryable, e)
        qc.log(
            item=item,
            verdict="DISCORD_FAIL",
            tier=decision.tier,
            reasons=decision.reasons + [f"retryable={e.retryable}", str(e)[:200]],
            details=_verdict_details(verdict),
        )
        # Retryable → don't mark seen; permanent → mark seen to avoid loop
        return not e.retryable
