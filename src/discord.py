"""Discord webhook poster. Adapted from tradingview-snr/bot/notifications.py.

Fail-loud variant: errors are logged AND raised. Caller decides retry/drop.
(In tradingview-snr the trade bot must keep running; here we'd rather know
which alerts didn't make it through.)"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from .oracles import LLMVerdict
from .sources import NewsItem

logger = logging.getLogger(__name__)

_TIMEOUT_SEC = 10.0
_SAFE_CONTENT_LIMIT = 1900

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
    pass


def get_webhook_url() -> Optional[str]:
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    return url or None


def post_discord(content: str, *, webhook_url: Optional[str] = None) -> bool:
    """POST plain content. Returns True on 2xx, raises DiscordPostError on failure.

    Returns False (no-op) when webhook is not configured — useful for dry-run."""
    url = webhook_url if webhook_url is not None else get_webhook_url()
    if not url:
        logger.info("DISCORD_WEBHOOK_URL not set — skipping post")
        return False

    payload = {"content": content[:_SAFE_CONTENT_LIMIT]}
    try:
        with httpx.Client(timeout=_TIMEOUT_SEC) as client:
            resp = client.post(url, json=payload)
    except Exception as e:
        raise DiscordPostError(f"discord webhook network error: {e}") from e

    if 200 <= resp.status_code < 300:
        return True

    raise DiscordPostError(
        f"discord webhook non-2xx status={resp.status_code} body={resp.text[:200]!r}"
    )


def format_alert(
    *,
    tier: str,
    item: NewsItem,
    verdict: LLMVerdict,
    primary_ticker: str,
) -> str:
    """Render a Discord message for a single alert."""
    tier_emoji = _TIER_EMOJI.get(tier, "🟢")
    sentiment_label = _SENTIMENT_LABEL.get(verdict.sentiment, verdict.sentiment)
    sentiment_emoji = _SENTIMENT_EMOJI.get(verdict.sentiment, "")
    published_str = item.published_at.strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"{tier_emoji} **[{tier}] ${primary_ticker}** · {sentiment_emoji} {sentiment_label} · `{verdict.category}`",
        f"**{item.title}**",
        f"📝 {verdict.chinese_summary}",
        f"📰 {item.publisher or item.source} · {published_str}",
        f"🔗 {item.url}",
    ]
    return "\n".join(lines)
