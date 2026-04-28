# Alert Tier Rules

決定一條新聞要不要發 Discord、發到哪一級。Pipeline phase 4 (differential) + phase 5 (self-consistency) 的輸出餵進這張表。

**Source of truth**：`pipeline.decide_tier()` 是 pure function 實作，本檔對齊其行為。改 code 必須同步改文件，否則 self-reference 漂移。

## Tier 定義（對應 `decide_tier()` 與 `_apply_self_consistency()`）

| Tier | 觸發條件 | Discord 表現 |
|------|---------|--------------|
| **CRITICAL** | source=`edgar` (8-K filing)；走 `_critical_path()` 跳過 LLM relevance、只走純翻譯 | 🚨 紅燈 + 中文翻譯 + 「請查原文」 |
| **HIGH** | source_confidence=`high` AND keyword path A pass AND LLM path B pass AND substring ok AND self-consistency 一致 | 🟢/🔴 emoji + 中文摘要 |
| **HIGH→MEDIUM 降級** | 上述但 self-consistency 不一致（`should_alert` 或 `is_relevant` 不一致） | 🟡 仍發 channel；reasons 含 `self_consistency_mismatch` |
| **MEDIUM** | source_confidence=`medium`（google_news）AND keyword pass AND LLM pass AND substring ok AND self-consistency 一致 | 🟡 + 中文摘要 |
| **MEDIUM→REVIEW 降級** | 上述但 self-consistency 不一致 | 不發；reasons 含 `self_consistency_mismatch` |
| **REVIEW (silent)** | differential disagreement / partial substring failure / self-consistency inconclusive | 不發；寫進 daily-report 計數 |
| **DROP (silent)** | hard gate（collision / exclude / total substring failure / LLM 說 don't alert） | 不發；寫 processed-log.ndjson |

## 為什麼 HIGH 不一致 → MEDIUM、MEDIUM 不一致 → REVIEW（不對稱降級）

設計選擇，不是 bug：

- **HIGH source = ticker-feed**（EDGAR / Finviz）：訊號本身已經是高 precision。LLM 不一致多半是 prompt 敏感，不是文章歧義。降到 MEDIUM 仍可發，保留訊號流量。
- **MEDIUM source = google_news**：query 結果本身雜訊高，LLM 是主要 precision gate。一旦兩個 model 不一致，這條訊號根本不可信，降到 REVIEW 更合理。

對稱降到 REVIEW 會掉 ticker-feed 的可信訊號；對稱不降會把 google_news 雜訊放出來。非對稱對應 source 的 prior。

## 為什麼 self-consistency 不比對 `alert_tier`（`tier_match` 已從 oracle 移除）

`alert_tier` 是 LLM 自己給的 high/medium/low 標籤。Opus 跟 Sonnet 對「high vs medium」的內部閾值有 noise。但這不是真正的不一致 — 兩者只要同意 `should_alert=true`、`is_relevant`、`relevance_type` 同向，本質判斷就吻合。

最終 tier 由 `pipeline.decide_tier()` 從 source confidence + kw + substring + relevance 算出，**LLM 的 alert_tier 字段只是 hint，不是 ground truth**。比對它會製造假不一致，把本該 HIGH 的訊號錯誤降級。

`ConsistencyResult.tier_diff` 仍保留作 diagnostic（QC log 看得到分歧），但不影響 `consistent`。

## QC Signal 對應（與 code 對齊）

| Signal | 嚴重度 | 處理 | Code 出處 |
|--------|--------|------|----------|
| `total_quote_failure` (LLM mention_quotes 全部不是 verbatim) | CRITICAL | DROP | `decide_tier` substring path |
| `quote_not_in_source` | CRITICAL | DROP（同上的舊名 alias） | 同上 |
| `partial_quote_failure` (有 verbatim 也有幻覺) | MAJOR | REVIEW（不發） | `decide_tier` B5 |
| `ticker_collision` (disambiguation hit) | CRITICAL | DROP | `_process_item` collision gate |
| `exclude_strict_hit` | MAJOR | DROP | `_process_item` exclude gate |
| `differential_disagreement_kw_no_llm_yes` (HIGH source) | MAJOR | REVIEW | `decide_tier` |
| `medium_source_no_keyword_match` | MAJOR | REVIEW | `decide_tier` |
| `self_consistency_mismatch` (HIGH source) | MAJOR | 降到 MEDIUM 仍發 | `_apply_self_consistency` |
| `self_consistency_mismatch` (MEDIUM source) | MAJOR | 降到 REVIEW 不發 | `_apply_self_consistency` |
| `self_consistency_inconclusive` (auditor LLM 失敗) | MAJOR | REVIEW（auditor 失敗本身是訊號，不能吞） | `_apply_self_consistency` B2 |
| `llm_no_relevant_or_buzzword_only` | MAJOR | DROP | `decide_tier` |
| `llm_should_not_alert` | MAJOR | DROP | `decide_tier` |
| `llm_error` (primary classifier 失敗) | MAJOR | REVIEW | `_process_item` |
| `date_too_old` (24h window) | MINOR | DROP | `run` Phase 2 |
| `already_sent` | MINOR | DROP | `run` Phase 2 |

## Critical path 摘要保證（item #3a 強化）

`_critical_path` 不呼叫完整 LLM classifier，只用 `translate_title_to_chinese`：

1. LLM 收到的指令僅為「翻譯英文標題為繁體中文，不要新增資訊、不要新增數字」
2. 翻譯結果經 `numeric_guardrail_pass()` 驗證 — 譯文出現的任何數字必須在標題中也存在
3. 失敗（CLI 失敗、guardrail 觸發）→ fallback 到「📋 SEC 8-K 重大事件公告（請查原文）」

設計 trade-off：8-K 是法定通報，**保守 > 流暢**。寧可 Discord 摘要乾癟，不能讓編造的金額或日期推進。

## sentiment / category（不影響 tier，影響 Discord 表現）

LLM 在 Magenta-key contract 同步輸出（critical path 不適用，固定 neutral / regulatory）：

- `sentiment ∈ {bullish, bearish, neutral, mixed}` → emoji 顏色
- `category ∈ {earnings, regulatory, M&A, analyst, rumor, macro, partnership}` → tag

## T1 競爭對手訊號收集（2026-04-28 起，data-only）

`source=competitor_finviz` 的 item 會被 pipeline early gate 強制走 REVIEW，**完全不打 LLM、完全不發 Discord**。reasons=`["competitor_signal_data_collection"]`。

設計依據：harness 「production data > theoretical refinement」。直接抓 competitor 新聞會踩 LLM 推論幻覺；直接 dismiss 又是放棄收 evidence。T1 是中間路：先用 REVIEW 收一週 raw data，再 data-driven 決定要不要 crystallize 規則。

收完一週分析方法：

```bash
jq 'select(.source=="competitor_finviz")' data/processed-log.ndjson | head -50
jq -s 'group_by(.publisher) | map({pub: .[0].publisher, n: length})' data/processed-log.ndjson
```

決定路徑：
- 大量 competitor 新聞但沒有可辨識 pattern → 廢掉（disable competitor_finviz）
- 有 N 條反覆出現的 trigger pattern（如「Cameco wins X」「FDA awards to Y」）→ 進 T2：寫 `config/competitor-impact-rules.json`，LLM 改成「matched_rules」純 lookup，不准自行推論

## 反饋校準（v2 候選）

Discord 訊息加 react 👍 / 👎 → bot 收 reaction → 寫進 `golden-set/positive/` 或 `negative/`。每週跑 scenario regression 看誤判率變化。
