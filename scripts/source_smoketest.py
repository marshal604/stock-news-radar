"""End-to-end smoke test: fetch real items from each source + run the body
fetcher on each, then report whether title + body extraction works.

Use:
    python scripts/source_smoketest.py             # all sources
    python scripts/source_smoketest.py --source edgar
    python scripts/source_smoketest.py --max-items 2  # cap per source
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.article_fetcher import fetch_article_body
from src.sources import (
    CompetitorFinvizSource,
    EdgarSource,
    FinnhubNewsSource,
    FinvizSource,
    GoogleNewsSource,
    PRWireSource,
)
from src.sources.base import NewsItem
from src.sources.google_news import decode_google_news_url


def _trunc(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").replace("\r", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def report_item(idx: int, item: NewsItem, *, decode_redirect: bool = False) -> dict:
    url = item.url
    if decode_redirect and "news.google.com" in url:
        url = decode_google_news_url(url)
    body, status = fetch_article_body(url, title=item.title)
    body_len = len(body) if body else 0
    print(f"  [{idx}] published_at={item.published_at.isoformat()}")
    print(f"      publisher={item.publisher}")
    print(f"      title={_trunc(item.title, 110)}")
    print(f"      url={_trunc(url, 110)}")
    print(f"      raw_text_len={len(item.raw_text)}  body_status={status}  body_chars={body_len}")
    if body:
        print(f"      body_preview={_trunc(body, 220)}")
    return {
        "title": item.title,
        "publisher": item.publisher,
        "published_at": item.published_at.isoformat(),
        "url": url,
        "raw_text_len": len(item.raw_text),
        "body_status": status,
        "body_chars": body_len,
    }


def run_source(name: str, source, ticker: str, ticker_meta: dict, *,
               max_items: int, decode_redirect: bool):
    print(f"\n=== {name} (ticker={ticker}) ===")
    try:
        items = source.fetch(ticker, ticker_meta)
    except Exception as e:
        print(f"  ERROR: fetch raised {type(e).__name__}: {e}")
        return {"source": name, "fetched": 0, "error": str(e)}

    print(f"  fetched={len(items)}")
    if not items:
        return {"source": name, "fetched": 0, "items": []}
    sampled = items[: max_items]
    reports = []
    for i, item in enumerate(sampled, 1):
        try:
            reports.append(report_item(i, item, decode_redirect=decode_redirect))
        except Exception as e:
            print(f"      body fetch raised {type(e).__name__}: {e}")
            reports.append({"error": str(e)})

    body_statuses = [r.get("body_status") for r in reports]
    summary = {
        "source": name,
        "fetched": len(items),
        "sampled": len(sampled),
        "body_complete": body_statuses.count("complete"),
        "body_partial": body_statuses.count("partial"),
        "body_title_only": body_statuses.count("title_only"),
    }
    print(f"  summary: {summary}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="all",
                    choices=["all", "edgar", "finviz", "competitor_finviz",
                             "google_news", "pr_newswire", "finnhub_news"])
    ap.add_argument("--ticker", default="UUUU",
                    help="ticker to test (must exist in tickers.json)")
    ap.add_argument("--max-items", type=int, default=3)
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args()

    logging.basicConfig(level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    with open(REPO_ROOT / "config" / "tickers.json", encoding="utf-8") as f:
        tickers = json.load(f)
    with open(REPO_ROOT / "config" / "sources.json", encoding="utf-8") as f:
        sources_config = json.load(f)
        sources_config.pop("_comment", None)

    if args.ticker not in tickers:
        print(f"ticker {args.ticker} not in tickers.json", file=sys.stderr)
        return 2
    meta = tickers[args.ticker]

    summaries: List[dict] = []
    want = args.source

    if want in ("all", "edgar"):
        summaries.append(run_source("edgar", EdgarSource(), args.ticker, meta,
                                    max_items=args.max_items, decode_redirect=False))

    if want in ("all", "finviz"):
        summaries.append(run_source("finviz", FinvizSource(), args.ticker, meta,
                                    max_items=args.max_items, decode_redirect=False))

    if want in ("all", "competitor_finviz"):
        summaries.append(run_source("competitor_finviz", CompetitorFinvizSource(),
                                    args.ticker, meta,
                                    max_items=args.max_items, decode_redirect=False))

    if want in ("all", "google_news"):
        queries = sources_config.get("google_news_queries", {})
        summaries.append(run_source("google_news",
                                    GoogleNewsSource(queries_by_ticker=queries),
                                    args.ticker, meta,
                                    max_items=args.max_items, decode_redirect=True))

    if want in ("all", "pr_newswire"):
        summaries.append(run_source("pr_newswire", PRWireSource(),
                                    args.ticker, meta,
                                    max_items=args.max_items, decode_redirect=True))

    if want in ("all", "finnhub_news"):
        api_key = os.getenv("FINNHUB_API_KEY", "").strip()
        if not api_key:
            print("\n=== finnhub_news (ticker=%s) ===" % args.ticker)
            print("  SKIPPED: FINNHUB_API_KEY not set in environment")
            summaries.append({"source": "finnhub_news", "skipped": True})
        else:
            summaries.append(run_source("finnhub_news",
                                        FinnhubNewsSource(api_key=api_key),
                                        args.ticker, meta,
                                        max_items=args.max_items, decode_redirect=False))

    print("\n=== overall ===")
    for s in summaries:
        print(f"  {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
