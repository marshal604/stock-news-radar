"""Entry point for daily earnings calendar reminder.

Local debug:
    FINNHUB_API_KEY=... DISCORD_WEBHOOK_URL=... python -m src.main_calendar --dry-run
GitHub Actions:
    python -m src.main_calendar"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .discord import DiscordPostError, post_discord
from .earnings_calendar import fetch_upcoming, format_alert

REPO_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="earnings calendar reminder")
    parser.add_argument("--dry-run", action="store_true", help="print, don't post Discord")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger("earnings-calendar")

    api_key = os.getenv("FINNHUB_API_KEY", "").strip()
    if not api_key:
        logger.error("FINNHUB_API_KEY not set — cannot fetch calendar")
        return 1

    with open(REPO_ROOT / "config" / "tickers.json", encoding="utf-8") as f:
        tickers = list(json.load(f).keys())
    logger.info("polling earnings for tickers=%s", tickers)

    events = fetch_upcoming(tickers, api_key)
    logger.info("found %d upcoming earnings in next 7 days", len(events))

    if not events:
        logger.info("no upcoming earnings; nothing to post")
        return 0

    posted = 0
    for event in events:
        content = format_alert(event)
        if args.dry_run:
            logger.info("[DRY RUN] would post:\n%s\n", content)
            continue
        try:
            post_discord(content)
            posted += 1
            logger.info("posted reminder for %s on %s", event.ticker, event.report_date)
        except DiscordPostError as e:
            logger.error("discord post failed: %s", e)
    logger.info("done; posted=%d (of %d events)", posted, len(events))
    return 0


if __name__ == "__main__":
    sys.exit(main())
