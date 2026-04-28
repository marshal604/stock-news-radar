from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import List, Literal, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

SourceConfidence = Literal["critical", "high", "medium"]


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

    def url_hash(self) -> str:
        p = urlparse(self.url)
        query = urlencode(
            [(k, v) for k, v in parse_qsl(p.query) if not _is_tracker_param(k)]
        )
        canonical = urlunparse((p.scheme, p.netloc, p.path.rstrip("/"), "", query, ""))
        return hashlib.sha256(canonical.lower().encode()).hexdigest()[:16]

    def title_hash(self) -> str:
        norm = " ".join(self.title.lower().split())
        return hashlib.sha256(norm.encode()).hexdigest()[:16]


def _is_tracker_param(key: str) -> bool:
    k = key.lower()
    return k.startswith("utm_") or k in {"fbclid", "gclid", "mc_cid", "mc_eid", "ncid"}


class Source(ABC):
    name: str
    confidence: SourceConfidence

    @abstractmethod
    def fetch(self, ticker: str, ticker_meta: dict) -> List[NewsItem]:
        """Fetch news for a given ticker. Returns canonical NewsItem list."""
