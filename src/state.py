"""SQLite-backed dedup store. Scoped to UTC day per user spec ('only today').

Persisted via GitHub Actions cache with daily key — old days auto-expire."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from .sources import NewsItem


class SeenStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen (
                day_utc TEXT NOT NULL,
                url_hash TEXT NOT NULL,
                title_hash TEXT NOT NULL,
                ticker_hint TEXT,
                source TEXT,
                title TEXT,
                first_seen_at TEXT NOT NULL,
                PRIMARY KEY (day_utc, url_hash)
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_title ON seen(day_utc, title_hash)"
        )
        self.conn.commit()

    def is_seen(self, item: NewsItem, day: Optional[date] = None) -> bool:
        d = (day or _today_utc()).isoformat()
        cur = self.conn.execute(
            "SELECT 1 FROM seen WHERE day_utc = ? AND (url_hash = ? OR title_hash = ?) LIMIT 1",
            (d, item.url_hash(), item.title_hash()),
        )
        return cur.fetchone() is not None

    def mark_seen(
        self,
        item: NewsItem,
        day: Optional[date] = None,
        *,
        dedup_by_title: bool = True,
    ) -> None:
        """Record item as seen so future runs skip it.

        dedup_by_title=False writes a unique-per-url sentinel as title_hash,
        which means a different URL with the same normalized title won't
        match. Used for items routed to REVIEW because we couldn't analyze
        them (title_only body / known aggregator) — if the same story later
        reaches us via a body-rich source (PR Newswire, Finviz), it must
        not be silently deduped against the unanalyzable original."""
        d = (day or _today_utc()).isoformat()
        url_hash = item.url_hash()
        title_hash = item.title_hash() if dedup_by_title else f"!notitle:{url_hash}"
        self.conn.execute(
            """
            INSERT OR IGNORE INTO seen
                (day_utc, url_hash, title_hash, ticker_hint, source, title, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                d,
                url_hash,
                title_hash,
                item.ticker_hint,
                item.source,
                item.title[:200],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()

    def gc_old_days(self, keep_days: int = 2) -> int:
        cutoff = (_today_utc().toordinal() - keep_days)
        cur = self.conn.execute("SELECT day_utc FROM seen GROUP BY day_utc")
        days_to_drop = [
            row[0] for row in cur.fetchall()
            if date.fromisoformat(row[0]).toordinal() < cutoff
        ]
        deleted = 0
        for d in days_to_drop:
            cur = self.conn.execute("DELETE FROM seen WHERE day_utc = ?", (d,))
            deleted += cur.rowcount
        self.conn.commit()
        return deleted

    def close(self) -> None:
        self.conn.close()


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()
