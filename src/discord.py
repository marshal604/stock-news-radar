"""Discord webhook poster. Adapted from tradingview-snr/bot/notifications.py.

Fail-loud variant: errors are logged AND raised. Caller decides retry/drop."""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx

from .oracles import LLMVerdict
from .sources import NewsItem

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 10.0
_SAFE_CONTENT_LIMIT = 1900
_RETRY_BACKOFF_SEC = (2, 4, 8)  # exponential backoff for transient failures

_TIER_EMOJI = {
    "CRITICAL": "🚨",
    "HIGH": "🟢",
    "MEDIUM": "🟡",
}

_SENTIMENT_LABEL = {
    "bullish": "利多",
    "bearish": "利空",
    "neutral": "中性",
    "mixed": "混合",
}

_SENTIMENT_EMOJI = {
    "bullish": "📈",
    "bearish": "📉",
    "neutral": "➖",
    "mixed": "🔀",
}


class DiscordPostError(Exception):
    """`retryable=True` means the failure was transient (5xx/429/network) — the
    pipeline should NOT mark the item seen so the next run retries.
    `retryable=False` means permanent (4xx other than 429) — mark seen to avoid
    a permanent failure loop."""

    def __init__(self, message: str, *, retryable: bool):
        super().__init__(message)
        self.retryable = retryable


def get_webhook_url() -> Optional[str]:
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    return url or None


def post_discord(content: str, *, webhook_url: Optional[str] = None) -> bool:
    """POST plain content with exponential backoff on transient failures.

    Returns True on 2xx; raises DiscordPostError after all retries exhausted.
    Returns False (no-op) when webhook is not configured — useful for dry-run.

    Retries: 2s → 4s → 8s on 429 / 5xx / network error. Permanent 4xx (excl 429)
    fails immediately."""
    url = webhook_url if webhook_url is not None else get_webhook_url()
    if not url:
        logger.info("DISCORD_WEBHOOK_URL not set — skipping post")
        return False

    payload = {"content": content[:_SAFE_CONTENT_LIMIT]}
    last_err: Optional[str] = None

    for attempt, backoff in enumerate([0] + list(_RETRY_BACKOFF_SEC)):
        if backoff:
            logger.warning("discord retry %d after %ds (last error: %s)", attempt, backoff, last_err)
            time.sleep(backoff)
        try:
            with httpx.Client(timeout=_TIMEOUT_SEC) as client:
                resp = client.post(url, json=payload)
        except Exception as e:
            last_err = f"network: {e}"
            continue

        if 200 <= resp.status_code < 300:
            return True
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            last_err = f"status={resp.status_code} body={resp.text[:200]!r}"
            continue
        # permanent client error (4xx excl 429): do not retry, mark seen to avoid loop
        raise DiscordPostError(
            f"discord webhook non-2xx status={resp.status_code} body={resp.text[:200]!r}",
            retryable=False,
        )

    # All retries exhausted on transient failures — caller should retry next run
    raise DiscordPostError(
        f"discord webhook failed after retries: {last_err}",
        retryable=True,
    )


def format_alert(
    *,
    tier: str,
    item: NewsItem,
    verdict: LLMVerdict,
    primary_ticker: str,
    summary_caveat: bool = False,
) -> str:
    """Render a Discord message for a single alert.

    Three layers of provenance signaling for the user:
      1. `summary_caveat=True` (B6): LLM hallucinated quotes — drop summary,
         show '[摘要待確認 — 引用幻覺]'.
      2. `item.body_fetch_status == 'title_only'` (T1): article body was
         unfetchable (paywall/JS/redirect-loss) — prepend '⚠️ [僅依標題判斷]'
         so user knows to click for real context.
      3. `item.body_fetch_status == 'partial'` (T1): only got short body or
         meta-tag fallback — prepend '📋 [依摘要 meta 描述]'."""
    tier_emoji = _TIER_EMOJI.get(tier, "🟢")
    sentiment_label = _SENTIMENT_LABEL.get(verdict.sentiment, verdict.sentiment)
    sentiment_emoji = _SENTIMENT_EMOJI.get(verdict.sentiment, "")
    published_str = item.published_at.strftime("%Y-%m-%d %H:%M UTC")

    if summary_caveat:
        summary_line = f"⚠️ [摘要待確認 — LLM 引用幻覺，請查原文] {item.title}"
    elif item.body_fetch_status == "title_only":
        summary_line = f"⚠️ [僅依標題判斷，無內文] 📝 {verdict.chinese_summary}"
    elif item.body_fetch_status == "partial":
        summary_line = f"📋 [依摘要 meta 描述] 📝 {verdict.chinese_summary}"
    else:
        summary_line = f"📝 {verdict.chinese_summary}"

    lines = [
        f"{tier_emoji} **[{tier}] ${primary_ticker}** · {sentiment_emoji} {sentiment_label} · `{verdict.category}`",
        f"**{item.title}**",
        summary_line,
        f"🎯 {verdict.impact_assessment}",
        f"📰 {item.publisher or item.source} · {published_str}",
        f"🔗 {item.url}",
    ]
    return "\n".join(lines)
