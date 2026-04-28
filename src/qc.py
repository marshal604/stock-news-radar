"""QC signal logger. Three artifacts:

1. processed-log-YYYY-MM-DD.ndjson — every item we touched + verdict (M6: rotated daily)
2. daily-report-YYYY-MM-DD.json — per-day cumulative counters (B4)
3. record_llm_call / record_source_anomaly — explicit observability counters (C4 / M3)

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
    def __init__(self, processed_log_dir: Path, daily_report_dir: Path):
        # M6: per-day processed log (was: single file growing unbounded). Rotated
        # by date; downstream tooling globs processed-log-*.ndjson.
        today = datetime.now(timezone.utc).date().isoformat()
        processed_log_dir.mkdir(parents=True, exist_ok=True)
        daily_report_dir.mkdir(parents=True, exist_ok=True)

        self.processed_log_path = processed_log_dir / f"processed-log-{today}.ndjson"
        self.daily_report_dir = daily_report_dir
        self._counters: Counter[str] = Counter()
        self._fp = open(self.processed_log_path, "a", encoding="utf-8")

    def log(
        self,
        *,
        item: NewsItem,
        verdict: str,  # SENT / DROP / REVIEW / DRY_RUN_SENT / DISCORD_FAIL / DEFER
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
            "body_fetch_status": item.body_fetch_status,
            "verdict": verdict,
            "tier": tier,
            "reasons": reasons or [],
            "details": details or {},
        }
        self._fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fp.flush()

        self._counters[f"verdict:{verdict}"] += 1
        self._counters[f"body_fetch:{item.body_fetch_status}"] += 1
        if tier:
            self._counters[f"tier:{tier}"] += 1
        for reason in reasons or []:
            self._counters[f"reason:{reason}"] += 1

    def record_llm_call(self, role: str) -> None:
        """C4: track LLM call volume per role.

        role: 'primary' | 'auditor' | 'translate'. Counts include retries
        (each subprocess invocation = one call). Surfaced in daily-report so
        a runaway retry loop or quota burn can be spotted before the bill."""
        self._counters[f"llm_call:{role}"] += 1

    def record_source_anomaly(self, source: str, error: str) -> None:
        """M3: track source fetch failures. Caller (pipeline) catches the
        exception and calls this — turns a silent return-empty into a counted,
        grep-able signal in the daily report."""
        self._counters[f"source_anomaly:{source}"] += 1
        # Also write a one-line audit trail to processed-log via a synthetic record
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": "source_anomaly",
            "source": source,
            "error": error[:300],
        }
        self._fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fp.flush()

    def flush_daily_report(self) -> None:
        """Merge this run's counters into the day's report file. Runs accumulate."""
        today = datetime.now(timezone.utc).date().isoformat()
        path = self.daily_report_dir / f"daily-report-{today}.json"

        existing: Dict[str, Any] = {}
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("could not read existing daily report %s: %s", path, e)

        cumulative = Counter(existing.get("counters", {}))
        cumulative.update(self._counters)

        report = {
            "date": today,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "runs_today": existing.get("runs_today", 0) + 1,
            "counters": dict(cumulative),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

    def close(self) -> None:
        self._fp.close()
