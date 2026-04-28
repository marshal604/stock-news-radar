"""Pipeline entry point.

Local debug:
    python -m src.main --dry-run
GitHub Actions:
    python -m src.main"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .pipeline import PipelineConfig, run

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_configs() -> tuple[dict, dict, dict]:
    with open(REPO_ROOT / "config" / "tickers.json", encoding="utf-8") as f:
        tickers = json.load(f)
    with open(REPO_ROOT / "config" / "keywords.json", encoding="utf-8") as f:
        keywords = json.load(f)
        keywords.pop("_comment", None)
    with open(REPO_ROOT / "config" / "sources.json", encoding="utf-8") as f:
        sources_config = json.load(f)
        sources_config.pop("_comment", None)
    return tickers, keywords, sources_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="stock-news-radar pipeline")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run pipeline but do not POST to Discord",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("stock-news-radar")

    tickers, keywords, sources_config = _load_configs()

    config = PipelineConfig(
        tickers=tickers,
        keywords=keywords,
        sources_config=sources_config,
        state_db=REPO_ROOT / "data" / "seen.sqlite",
        processed_log=REPO_ROOT / "data" / "processed-log.ndjson",
        daily_report_dir=REPO_ROOT / "qc",
        dry_run=args.dry_run,
    )

    try:
        stats = run(config)
        logger.info("done: %s", stats)
        return 0
    except Exception as e:
        logger.exception("pipeline failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
