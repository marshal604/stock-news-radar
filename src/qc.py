"""QC signal logger. Two artifacts:

1. processed-log.ndjson — every item we touched + verdict + decision (append-only)
2. daily-report.json — aggregated counts per QC signal, refreshed each run

Per harness rule 'Fail Loud': we never silently drop. Every drop has a reason."""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .sources import NewsItem

logger = logging.getLogger(__name__)


class QCLogger:
    def __init__(self, processed_log: Path, daily_report: Path):
        processed_log.parent.mkdir(parents=True, exist_ok=True)
        daily_report.parent.mkdir(parents=True, exist_ok=True)
        self.processed_log_path = processed_log
        self.daily_report_path = daily_report
        self._counters: Counter[str] = Counter()
        self._fp = open(processed_log, "a", encoding="utf-8")

    def log(
        self,
        *,
        item: NewsItem,
        verdict: str,  # SENT / DROP / REVIEW
        tier: Optional[str] = None,
        reasons: Optional[list[str]] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "url": item.url,
            "title": item.title,
            "source": item.source,
            "ticker_hint": item.ticker_hint,
            "published_at": item.published_at.isoformat(),
            "verdict": verdict,
            "tier": tier,
            "reasons": reasons or [],
            "details": details or {},
        }
        self._fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fp.flush()

        self._counters[f"verdict:{verdict}"] += 1
        if tier:
            self._counters[f"tier:{tier}"] += 1
        for reason in reasons or []:
            self._counters[f"reason:{reason}"] += 1

    def flush_daily_report(self) -> None:
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "counters": dict(self._counters),
        }
        # Merge with any previous report from same day (within run cumulative — file is overwritten each run)
        with open(self.daily_report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    def close(self) -> None:
        self._fp.close()
