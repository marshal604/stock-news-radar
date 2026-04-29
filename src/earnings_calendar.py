"""Daily earnings calendar reminder via Finnhub.

Fetches upcoming earnings for tracked tickers (next 7 days) and posts
countdown reminders to Discord. Independent of the news radar pipeline.

Why this exists separately
--------------------------
The news pipeline DROPs scheduled-earnings-announcement press releases
('Energy Fuels Announces Call Details for Q1-2026 Earnings') as routine
governance — they don't move stock on their own, the actual results do.
But the user wants to know WHEN earnings will drop so they can plan
positioning / avoid IV crush. Calendar is a separate, structured signal.

Source: Finnhub /calendar/earnings — free tier, 60 calls/min,
includes EPS + revenue estimates."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

FINNHUB_URL = "https://finnhub.io/api/v1/calendar/earnings"
LOOKAHEAD_DAYS = 7
TIMEOUT_SEC = 10.0


@dataclass(frozen=True)
class EarningsEvent:
    ticker: str
    report_date: date
    hour: str  # 'bmo' (before market open) | 'amc' (after market close) | 'dmh' (during) | ''
    quarter: int
    year: int
    eps_estimate: Optional[float]
    revenue_estimate: Optional[float]


def fetch_upcoming(
    tickers: List[str],
    api_key: str,
    *,
    lookahead_days: int = LOOKAHEAD_DAYS,
    today: Optional[date] = None,
) -> List[EarningsEvent]:
    """Query Finnhub for earnings within today..today+lookahead for given tickers."""
    if today is None:
        today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=lookahead_days)

    events: List[EarningsEvent] = []
    for ticker in tickers:
        try:
            with httpx.Client(timeout=TIMEOUT_SEC) as client:
                resp = client.get(
                    FINNHUB_URL,
                    params={
                        "from": today.isoformat(),
                        "to": end.isoformat(),
                        "symbol": ticker,
                        "token": api_key,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.warning("finnhub fetch failed for %s: %s", ticker, e)
            continue

        for entry in data.get("earningsCalendar", []) or []:
            try:
                events.append(
                    EarningsEvent(
                        ticker=entry["symbol"],
                        report_date=date.fromisoformat(entry["date"]),
                        hour=(entry.get("hour") or "").lower(),
                        quarter=int(entry.get("quarter") or 0),
                        year=int(entry.get("year") or 0),
                        eps_estimate=_to_float(entry.get("epsEstimate")),
                        revenue_estimate=_to_float(entry.get("revenueEstimate")),
                    )
                )
            except (KeyError, ValueError) as e:
                logger.warning("malformed finnhub entry: %s — %s", entry, e)
    return events


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


_HOUR_LABEL = {
    "bmo": "盤前 (Before Market Open)",
    "amc": "盤後 (After Market Close)",
    "dmh": "盤中",
}


def format_alert(event: EarningsEvent, today: Optional[date] = None) -> str:
    """Render a Discord message for one upcoming earnings event."""
    if today is None:
        today = datetime.now(timezone.utc).date()
    days_to = (event.report_date - today).days

    if days_to == 0:
        when = "**今天**"
    elif days_to == 1:
        when = "**明天**"
    elif days_to < 0:
        when = f"已過 {-days_to} 天"  # shouldn't normally happen
    else:
        when = f"**{days_to} 天後**"

    hour_label = _HOUR_LABEL.get(event.hour, "時段未定")

    estimate_parts = []
    if event.eps_estimate is not None:
        estimate_parts.append(f"預估 EPS: ${event.eps_estimate:.2f}")
    if event.revenue_estimate is not None:
        rev_m = event.revenue_estimate / 1_000_000
        if rev_m >= 1000:
            estimate_parts.append(f"預估營收: ${rev_m / 1000:.2f}B")
        else:
            estimate_parts.append(f"預估營收: ${rev_m:.1f}M")
    estimate_line = " · ".join(estimate_parts) if estimate_parts else "（無分析師預估）"

    lines = [
        f"📅 **[Earnings] ${event.ticker}** · {when}",
        f"**Q{event.quarter} {event.year} 財報**將於 **{event.report_date.isoformat()}** ({hour_label}) 公佈",
        f"📊 {estimate_line}",
    ]
    return "\n".join(lines)
