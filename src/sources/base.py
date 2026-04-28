from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import List, Literal, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

SourceConfidence = Literal["critical", "high", "medium"]
# Explicit signal for how much LLM context we have on this item. Default at
# construction is 'summary_only' (title + RSS summary, no body fetch attempted).
# Pipeline upgrades to 'partial' / 'complete' or downgrades to 'title_only' after
# article body fetch attempt. Decide_tier consults this for tier cap; format_alert
# annotates the Discord message so user knows what to expect.
BodyFetchStatus = Literal[
    "complete",      # ≥ 500 chars of real article body, quality gates passed
    "partial",       # 200-500 chars or meta-tag-only fallback (og:description)
    "summary_only",  # default — title + RSS summary, no body fetch attempted
    "title_only",    # body fetch tried, all strategies failed (paywall/JS/redirect-loss)
]


@dataclass(frozen=True)
class NewsItem:
    url: str
    title: str
    raw_text: str
    published_at: datetime
    source: str
    source_confidence: SourceConfidence
    ticker_hint: Optional[str] = None
    publisher: Optional[str] = None
    body_fetch_status: BodyFetchStatus = "summary_only"

    def __post_init__(self) -> None:
        # N8: published_at must be timezone-aware. Naive datetimes silently break
        # the 24h freshness filter (cutoff comparison fails or behaves unexpectedly
        # depending on tz boundary). Catch at construction so each source adapter
        # is forced to attach tzinfo, fail-loud rather than silent date_too_old.
        if self.published_at.tzinfo is None:
            raise ValueError(
                f"NewsItem.published_at must be timezone-aware "
                f"(got naive datetime for url={self.url!r}, source={self.source!r})"
            )

    def url_hash(self) -> str:
        p = urlparse(self.url)
        query = urlencode(
            [(k, v) for k, v in parse_qsl(p.query) if not _is_tracker_param(k)]
        )
        canonical = urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", query, ""))
        return hashlib.sha256(canonical.lower().encode()).hexdigest()[:16]

    def title_hash(self) -> str:
        # C2: aggressive cross-source normalize. Same article from different
        # publishers should hash to the same value so we don't alert 3x.
        return hashlib.sha256(normalize_title_for_dedup(self.title).encode()).hexdigest()[:16]


# Strip trailing source attribution like ' - Reuters' or ' — 24/7 Wall St.'.
# Conservative: must be hyphen/dash + whitespace + at least one word.
_TRAILING_SOURCE_RE = re.compile(r"\s+[-–—]\s+[^-–—\s][^-–—]*$")


def normalize_title_for_dedup(title: str) -> str:
    """Aggressive normalization for cross-source title dedup.

    Strips trailing publisher attribution, ellipsis, and punctuation; collapses
    whitespace; lowercases. Preserves alphanumerics + CJK characters.

    'Energy Fuels Reports Q1 - Reuters' and 'Energy Fuels reports Q1...' should
    both reduce to 'energy fuels reports q1' so the title_hash matches across
    Finviz/Google News/etc and we don't fan out a single article into 3 alerts."""
    if not title:
        return ""
    s = title.lower()
    # Strip trailing ' - <publisher>' (run twice in case of nested attribution)
    for _ in range(2):
        new = _TRAILING_SOURCE_RE.sub("", s)
        if new == s:
            break
        s = new
    # Strip ellipsis (both ASCII and unicode forms)
    s = s.replace("…", "")
    s = re.sub(r"\.{2,}", "", s)
    # Remove all punctuation; \w covers alphanumerics + underscores + CJK with re.UNICODE
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    # Collapse whitespace
    return " ".join(s.split())


def _is_tracker_param(key: str) -> bool:
    k = key.lower()
    return k.startswith("utm_") or k in {"fbclid", "gclid", "mc_cid", "mc_eid", "ncid"}


class Source(ABC):
    name: str
    confidence: SourceConfidence

    @abstractmethod
    def fetch(self, ticker: str, ticker_meta: dict) -> List[NewsItem]:
        """Fetch news for a given ticker. Returns canonical NewsItem list."""
