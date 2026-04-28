from __future__ import annotations

import logging
import re
from datetime import datetime, time, timezone
from typing import List, Optional

import httpx
from bs4 import BeautifulSoup

from .base import NewsItem, Source

logger = logging.getLogger(__name__)

URL_TEMPLATE = "https://finviz.com/quote.ashx?t={ticker}&p=d"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
TIMEOUT_SEC = 10.0

_DATE_RE = re.compile(r"^([A-Z][a-z]{2}-\d{2}-\d{2})\s+(\d{1,2}:\d{2}(?:AM|PM))$")
_TIME_ONLY_RE = re.compile(r"^(\d{1,2}:\d{2}(?:AM|PM))$")


class FinvizSource(Source):
    name = "finviz"
    confidence = "high"

    def fetch(self, ticker: str, ticker_meta: dict) -> List[NewsItem]:
        url = URL_TEMPLATE.format(ticker=ticker)
        try:
            resp = httpx.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT_SEC,
                follow_redirects=True,
            )
            resp.raise_for_status()
        except Exception as e:
            logger.warning("finviz fetch failed for %s: %s", ticker, e)
            return []

        soup = BeautifulSoup(resp.text, "lxml")
        table = soup.find("table", id="news-table")
        if table is None:
            logger.warning("finviz: news-table not found for %s", ticker)
            return []

        items: List[NewsItem] = []
        current_date: Optional[datetime] = None
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            stamp_text = cells[0].get_text(strip=True)
            link_tag = cells[1].find("a", class_="tab-link-news") or cells[1].find("a")
            if link_tag is None:
                continue
            title = link_tag.get_text(strip=True)
            href = (link_tag.get("href") or "").strip()
            if not title or not href:
                continue

            publisher_tag = cells[1].find("span")
            publisher = publisher_tag.get_text(strip=True).strip("()") if publisher_tag else None

            published, current_date = _parse_finviz_stamp(stamp_text, current_date)
            if published is None:
                continue

            items.append(
                NewsItem(
                    url=href,
                    title=title,
                    raw_text=title,
                    published_at=published,
                    source=self.name,
                    source_confidence=self.confidence,
                    ticker_hint=ticker,
                    publisher=publisher,
                )
            )
        return items


def _parse_finviz_stamp(text: str, prev_date: Optional[datetime]):
    """Finviz format: full timestamp at top of day ('Apr-28-26 06:40AM'), then time-only for same day."""
    full_match = _DATE_RE.match(text)
    if full_match:
        date_part, time_part = full_match.group(1), full_match.group(2)
        try:
            dt = datetime.strptime(f"{date_part} {time_part}", "%b-%d-%y %I:%M%p")
            dt = dt.replace(tzinfo=timezone.utc)
            return dt, dt
        except ValueError:
            return None, prev_date

    time_match = _TIME_ONLY_RE.match(text)
    if time_match and prev_date is not None:
        try:
            t = datetime.strptime(time_match.group(1), "%I:%M%p").time()
            dt = datetime.combine(prev_date.date(), t, tzinfo=timezone.utc)
            return dt, prev_date
        except ValueError:
            return None, prev_date

    return None, prev_date
