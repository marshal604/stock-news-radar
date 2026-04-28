from __future__ import annotations

from typing import Dict, List, Literal

from pydantic import BaseModel, Field, field_validator

RelevanceType = Literal[
    "company-specific",
    "sector-policy",
    "macro-tangential",
    "buzzword-list-only",
]
Sentiment = Literal["bullish", "bearish", "neutral", "mixed"]
Category = Literal[
    "earnings",
    "regulatory",
    "M&A",
    "analyst",
    "rumor",
    "macro",
    "partnership",
]
AlertTier = Literal["high", "medium", "low"]


class TickerRelevance(BaseModel):
    is_relevant: bool
    ticker_appears_verbatim: bool
    mention_quotes: List[str] = Field(default_factory=list)
    relevance_type: RelevanceType
    confidence: float = Field(ge=0.0, le=1.0)


class LLMVerdict(BaseModel):
    """Magenta-key contract. LLM 100% can output this; Python 100% can validate."""

    ticker_relevance: Dict[str, TickerRelevance]
    publish_date_iso: str
    sentiment: Sentiment
    category: Category
    should_alert: bool
    alert_tier: AlertTier
    chinese_summary: str

    @field_validator("chinese_summary")
    @classmethod
    def _summary_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("chinese_summary must not be empty")
        return v.strip()
