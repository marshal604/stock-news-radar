"""Layer 2-B oracle: Claude CLI subprocess classifier.

Strict JSON output via Magenta-key contract (see schema.py). Retry on parse fail
(harness rule: don't patch, retry). Fail-loud on validation error."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Optional

from pydantic import ValidationError

from .schema import LLMVerdict

logger = logging.getLogger(__name__)

# Model IDs are env-var overridable so a single env change can swap to newer models
# (e.g. 4.7 → 4.8) without touching code. Defaults pin to the current latest.
#
# Primary classifier (relevance + sentiment + decisions) uses Opus for highest
# judgment quality. Secondary roles (auditor self-consistency, EDGAR translation)
# use Sonnet — cheaper, and a different model gives stronger differential independence.
PRIMARY_MODEL = os.getenv("RADAR_PRIMARY_MODEL", "claude-opus-4-7")
SECONDARY_MODEL = os.getenv("RADAR_SECONDARY_MODEL", "claude-sonnet-4-6")
DEFAULT_TIMEOUT = 180
TRANSLATE_TIMEOUT = 60
MAX_RETRIES = 2

CLASSIFIER_SYSTEM = """You are a strict financial news relevance classifier.

Output ONLY a JSON object matching the schema below — no preamble, no explanation, no markdown code fences.

CRITICAL RULES:
1. mention_quotes MUST be VERBATIM substrings copied EXACTLY from the article. Never paraphrase. Whitespace can be collapsed but words must be exact. If you cannot find a verbatim supporting quote, set is_relevant=false and mention_quotes=[].
2. relevance_type semantics:
   - "company-specific": article is primarily ABOUT this ticker's company (earnings, exec, M&A, product)
   - "sector-policy": article is about a regulation/event that EXPLICITLY mentions and impacts this ticker
   - "macro-tangential": article touches the sector but does NOT specifically discuss the company
   - "buzzword-list-only": ticker appears only as one of N stocks in a list, NOT as primary subject
3. should_alert=true ONLY if relevance_type is "company-specific" or "sector-policy"
4. alert_tier:
   - "high": breaking material news (earnings, M&A, FDA approval/rejection, lawsuit, exec change, 8-K)
   - "medium": notable but not market-moving (analyst rating change, partnership announcement, study results)
   - "low": background context only
5. Ticker disambiguation: TEM = Tempus AI on NASDAQ. If article refers to "Templeton Emerging Markets" or "TEMPO Automation" or other TEM-named entities, set is_relevant=false for TEM.
6. Ticker disambiguation: UUUU = Energy Fuels on NYSE. Almost no collisions.
7. chinese_summary: ONE concise sentence in 繁體中文 (30-50 字), describing the news + likely impact direction on the target ticker.

Schema (output exactly this shape):
{
  "ticker_relevance": {
    "<TICKER>": {
      "is_relevant": <bool>,
      "ticker_appears_verbatim": <bool>,
      "mention_quotes": [<verbatim string from article>, ...],
      "relevance_type": "company-specific"|"sector-policy"|"macro-tangential"|"buzzword-list-only",
      "confidence": <float 0.0-1.0>
    }
  },
  "publish_date_iso": "<YYYY-MM-DDTHH:MM:SSZ>",
  "sentiment": "bullish"|"bearish"|"neutral"|"mixed",
  "category": "earnings"|"regulatory"|"M&A"|"analyst"|"rumor"|"macro"|"partnership",
  "should_alert": <bool>,
  "alert_tier": "high"|"medium"|"low",
  "chinese_summary": "<繁體中文一句話>"
}"""

# Self-consistency uses a different framing to test if the model anchors on the article
# vs. on the prompt. If both prompts converge → consistent → trustworthy.
AUDITOR_SYSTEM = """You are a financial relevance auditor. For the given article and target tickers, judge:
- Is this article PRIMARILY about the listed company, or just mentioning it in passing?
- Could a swing trader of this stock plausibly act on this news?
- Does the article quote material directly attributable to or about the company?

Output ONLY a JSON object with the following schema (no prose, no markdown):
{
  "ticker_relevance": {
    "<TICKER>": {
      "is_relevant": <bool>,
      "ticker_appears_verbatim": <bool>,
      "mention_quotes": [<verbatim string>, ...],
      "relevance_type": "company-specific"|"sector-policy"|"macro-tangential"|"buzzword-list-only",
      "confidence": <float 0.0-1.0>
    }
  },
  "publish_date_iso": "<YYYY-MM-DDTHH:MM:SSZ>",
  "sentiment": "bullish"|"bearish"|"neutral"|"mixed",
  "category": "earnings"|"regulatory"|"M&A"|"analyst"|"rumor"|"macro"|"partnership",
  "should_alert": <bool>,
  "alert_tier": "high"|"medium"|"low",
  "chinese_summary": "<one sentence in 繁體中文 describing news + likely direction on ticker>"
}

mention_quotes MUST be verbatim substrings of the article. should_alert=true only if relevance_type is company-specific or sector-policy. Disambiguate TEM=Tempus AI (not Templeton Emerging Markets / TEMPO)."""


_TRANSLATE_SYSTEM = """你是一個嚴謹的翻譯員。把英文標題翻譯成繁體中文。

絕對規則:
1. 不要新增任何資訊
2. 不要推論影響、不要寫「利多/利空」
3. 不要新增原文沒有的數字、金額、百分比、日期
4. 不要新增原文沒有的人名、公司名
5. 如果不確定如何翻譯某個專有名詞，保留英文原文

只輸出譯文。沒有引號，沒有解釋，沒有 markdown。"""

_NUMERIC_FALLBACK = "📋 SEC 8-K 重大事件公告（請查原文）"


class LLMOracleError(Exception):
    pass


def translate_title_to_chinese(title: str, *, model: str = SECONDARY_MODEL, timeout: int = TRANSLATE_TIMEOUT) -> str:
    """Translate-only LLM call with numeric guardrail (B6 hardening for critical path).

    Used by EDGAR critical path: 8-K is a legal filing, we want a faithful
    Chinese rendering of the title — never analysis, never invented numbers.
    Any digit in the translation that wasn't in the source title triggers a
    safe fallback string instead of returning the (possibly hallucinated) text."""
    if not title.strip():
        return _NUMERIC_FALLBACK

    try:
        raw = _invoke_claude(
            user_prompt=title,
            system_prompt=_TRANSLATE_SYSTEM,
            model=model,
            timeout=timeout,
        )
    except _SubprocessError as e:
        logger.warning("translate_title CLI failed: %s", e)
        return _NUMERIC_FALLBACK

    translation = raw.strip().strip('"').strip("'").strip()
    if not translation:
        return _NUMERIC_FALLBACK

    if not numeric_guardrail_pass(title, translation):
        logger.warning(
            "translate_title numeric guardrail tripped — title=%r translation=%r",
            title,
            translation,
        )
        return _NUMERIC_FALLBACK

    return translation


def numeric_guardrail_pass(source: str, translation: str) -> bool:
    """True iff every digit in `translation` also appears in `source`.

    Catches LLM digit hallucinations in translation tasks (e.g. inserting
    monetary amounts not present in the original). Set semantics — order and
    multiplicity ignored — because translations may rephrase but should not
    introduce new numeric facts."""
    src_digits = set(re.findall(r"\d", source))
    out_digits = set(re.findall(r"\d", translation))
    return out_digits.issubset(src_digits)


def classify_with_llm(
    *,
    tickers: list[str],
    url: str,
    title: str,
    raw_text: str,
    published: str,
    source: str,
    publisher: Optional[str],
    use_auditor_phrasing: bool = False,
    model: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> LLMVerdict:
    """Call Claude CLI and return validated LLMVerdict. Retry on parse/validation fail.

    Default model selection: PRIMARY_MODEL (Opus) for primary classification,
    SECONDARY_MODEL (Sonnet) for auditor pass. Override via `model` arg."""
    system_prompt = AUDITOR_SYSTEM if use_auditor_phrasing else CLASSIFIER_SYSTEM
    if model is None:
        model = SECONDARY_MODEL if use_auditor_phrasing else PRIMARY_MODEL
    user_prompt = _build_user_prompt(
        tickers=tickers,
        url=url,
        title=title,
        raw_text=raw_text,
        published=published,
        source=source,
        publisher=publisher,
    )

    last_err: Optional[Exception] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            raw_output = _invoke_claude(
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                model=model,
                timeout=timeout,
            )
            parsed = _extract_json(raw_output)
            return LLMVerdict.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError, _SubprocessError) as e:
            last_err = e
            logger.warning("llm classify attempt %d failed: %s", attempt + 1, e)
            continue

    raise LLMOracleError(f"LLM classification failed after {MAX_RETRIES + 1} attempts: {last_err}")


def _build_user_prompt(
    *,
    tickers: list[str],
    url: str,
    title: str,
    raw_text: str,
    published: str,
    source: str,
    publisher: Optional[str],
) -> str:
    tickers_block = "\n".join(f"- {t}" for t in tickers)
    return f"""TARGET TICKERS:
{tickers_block}

ARTICLE:
URL: {url}
TITLE: {title}
PUBLISHED: {published}
SOURCE: {source}{' (' + publisher + ')' if publisher else ''}

CONTENT:
{raw_text}

Classify according to the schema. Output JSON only."""


class _SubprocessError(Exception):
    pass


def _invoke_claude(
    *,
    user_prompt: str,
    system_prompt: str,
    model: str,
    timeout: int,
) -> str:
    cmd = [
        "claude",
        "-p", user_prompt,
        "--append-system-prompt", system_prompt,
        "--output-format", "text",
        "--model", model,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as e:
        raise _SubprocessError(f"claude CLI timeout after {timeout}s") from e
    except FileNotFoundError as e:
        raise _SubprocessError(
            "claude CLI not found in PATH. Install with: npm install -g @anthropic-ai/claude-code"
        ) from e

    if proc.returncode != 0:
        raise _SubprocessError(
            f"claude CLI exit={proc.returncode} "
            f"stderr={proc.stderr[:500]!r} stdout={proc.stdout[:500]!r}"
        )
    return proc.stdout


_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def _extract_json(text: str) -> dict:
    """Pull a JSON object out of model output. Strip code fences if present."""
    fence = _FENCE_RE.search(text)
    if fence:
        return json.loads(fence.group(1).strip())

    match = _OBJECT_RE.search(text)
    if not match:
        raise json.JSONDecodeError("no JSON object found in output", text, 0)
    return json.loads(match.group(0))
