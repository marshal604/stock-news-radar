"""Deterministic date candidate extractor (Layer 1 of harness).

Per harness rule: LLM is bad at precise values (dates / numbers). Don't ask
LLM to extract dates from free text — Python regex extracts the candidates,
LLM only picks which candidate is the *event* date. Selection is a semantic
task LLM is good at; extraction is a precision task it is not.

Magenta key contract: produces ordered List[DateCandidate]. LLM consumes
indexes; pipeline validates bounds. No shared code between this extractor
and the LLM picker — independence holds.

Supported surface forms:
  - ISO:            2026-04-23
  - US long:        April 23, 2026 / Apr 23, 2026 / April 23rd, 2026
  - US no-year:     April 23 / Apr 23rd
  - Day-Month:      23 April 2026 / 23rd April / 23 Apr
  - Numeric slash:  4/23/26 / 4/23/2026 / 04-23-2026
  - Numeric no-year: 4/23

When the year is missing, `reference_year` (typically the article publish
year) is used. This is correct >95% of the time for finance news where
bare 'April 23' refers to the most recent April 23 relative to publication.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

_MONTHS = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# Single capture group around the month name; case-insensitive at compile.
_MONTH_NAME = r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_DAY = r"(\d{1,2})"
_ORD = r"(?:st|nd|rd|th)?"
_YEAR_4 = r"(\d{4})"

# Each regex captures: (month, day, year?) or (day, month, year?) explicitly.
_ISO_RE = re.compile(rf"\b{_YEAR_4}-(\d{{2}})-(\d{{2}})\b")
# 'April 23, 2026' / 'Apr 23rd 2026'
_US_LONG_RE = re.compile(
    rf"\b{_MONTH_NAME}\s+{_DAY}{_ORD}(?:,\s*|\s+){_YEAR_4}\b",
    re.IGNORECASE,
)
# 'April 23' / 'Apr 23rd'  (no trailing year)
_US_NOYEAR_RE = re.compile(
    rf"\b{_MONTH_NAME}\s+{_DAY}{_ORD}\b(?!\s*[,/-]?\s*\d{{2,4}})",
    re.IGNORECASE,
)
# '23 April 2026' / '23rd Apr'
_DM_RE = re.compile(
    rf"\b{_DAY}{_ORD}\s+{_MONTH_NAME}(?:\s+{_YEAR_4})?\b",
    re.IGNORECASE,
)
# '4/23/2026' / '04-23-26'
_NUMERIC_RE = re.compile(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})\b")
# '4/23' (not followed by another /digits)
_NUMERIC_NOYEAR_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})\b(?![/\-]?\d)")


@dataclass(frozen=True)
class DateCandidate:
    iso_date: str       # 'YYYY-MM-DD'
    surface_form: str   # the literal substring as it appears in source
    char_offset: int    # 0-based char index of surface_form in source

    def __post_init__(self) -> None:
        # Fail loud — downstream relies on iso_date being a valid date.
        date.fromisoformat(self.iso_date)


def extract_date_candidates(
    text: str,
    *,
    reference_year: int,
) -> List[DateCandidate]:
    """Pure regex extraction of date mentions, normalized to ISO.

    Sorted by occurrence (char_offset). Deduplicated by iso_date — if the
    same date appears via multiple surface forms, the earliest occurrence
    is kept."""
    raw: List[DateCandidate] = []

    for m in _ISO_RE.finditer(text):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        c = _safe_candidate(y, mo, d, m.group(0), m.start())
        if c:
            raw.append(c)

    for m in _US_LONG_RE.finditer(text):
        mo = _MONTHS[m.group(1).lower()]
        d = int(m.group(2))
        y = int(m.group(3))
        c = _safe_candidate(y, mo, d, m.group(0), m.start())
        if c:
            raw.append(c)

    for m in _DM_RE.finditer(text):
        d = int(m.group(1))
        mo = _MONTHS[m.group(2).lower()]
        y = int(m.group(3)) if m.group(3) else reference_year
        c = _safe_candidate(y, mo, d, m.group(0), m.start())
        if c:
            raw.append(c)

    for m in _NUMERIC_RE.finditer(text):
        a, b, c_raw = int(m.group(1)), int(m.group(2)), int(m.group(3))
        # US convention: month/day/year. If first token > 12 it's day-first.
        if a > 12 and b <= 12:
            day_v, month_v = a, b
        else:
            month_v, day_v = a, b
        year_v = c_raw if c_raw >= 100 else (2000 + c_raw if c_raw < 70 else 1900 + c_raw)
        c = _safe_candidate(year_v, month_v, day_v, m.group(0), m.start())
        if c:
            raw.append(c)

    for m in _US_NOYEAR_RE.finditer(text):
        mo = _MONTHS[m.group(1).lower()]
        d = int(m.group(2))
        c = _safe_candidate(reference_year, mo, d, m.group(0), m.start())
        if c:
            raw.append(c)

    for m in _NUMERIC_NOYEAR_RE.finditer(text):
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 and b <= 12:
            day_v, month_v = a, b
        else:
            month_v, day_v = a, b
        c = _safe_candidate(reference_year, month_v, day_v, m.group(0), m.start())
        if c:
            raw.append(c)

    raw.sort(key=lambda c: c.char_offset)

    seen: dict[str, DateCandidate] = {}
    for c in raw:
        if c.iso_date not in seen:
            seen[c.iso_date] = c
    return list(seen.values())


def _safe_candidate(
    year: int, month: int, day: int, surface: str, offset: int
) -> Optional[DateCandidate]:
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    return DateCandidate(
        iso_date=d.isoformat(),
        surface_form=surface,
        char_offset=offset,
    )


# ── Temporal classification (Layer 3 of harness — pure deterministic) ──

# Boundaries (days, inclusive lower, exclusive upper). Tuned for swing-trading
# news cadence: same-day = breaking; 1-2 days = recent (still actionable); 3-14
# days = retrospective (worth flagging as "old news"); >14 = stale. Calibrate
# after 1 week of production data per cheatsheet rule 6.
_BREAKING_MAX = 0
_RECENT_MAX = 2
_RETRO_MAX = 14

TemporalClass = str  # "breaking" | "recent" | "retrospective" | "stale" | "future"


def classify_temporal(event_date_iso: str, publish_date_iso: str) -> TemporalClass:
    """Classify lag between event and publication into a display bucket.

    Pure function — same inputs always yield same output. Pipeline calls this
    after LLM picks event_date_index; Discord tag rendering reads the bucket."""
    event_d = date.fromisoformat(event_date_iso)
    publish_d = date.fromisoformat(publish_date_iso[:10])  # tolerate ISO datetime input
    lag = (publish_d - event_d).days
    if lag < 0:
        # Event date claimed to be AFTER publication — usually scheduled future
        # event ('Q1 results to be announced May 15'). Treated as 'future' so
        # it's logged but not tagged as retrospective.
        return "future"
    if lag <= _BREAKING_MAX:
        return "breaking"
    if lag <= _RECENT_MAX:
        return "recent"
    if lag <= _RETRO_MAX:
        return "retrospective"
    return "stale"


def event_lag_days(event_date_iso: str, publish_date_iso: str) -> int:
    """Days between event and publication. Negative if event is in the future."""
    event_d = date.fromisoformat(event_date_iso)
    publish_d = date.fromisoformat(publish_date_iso[:10])
    return (publish_d - event_d).days
