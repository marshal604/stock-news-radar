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
| **REVIEW (silent)** | differential disagreement / partial substring failure / self-consistency inconclusive / `body_fetch_status==title_only` (universal gate) / Google News aggregator publisher | 不發；寫進 daily-report 計數 |
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
| `title_only_no_body_for_analysis` (universal gate — body fetch yielded no usable text) | MAJOR | REVIEW（不發；避免「僅依標題判斷」型雜訊 alert） | `decide_tier` |
| `aggregator_publisher:<name>` (MSN/Yahoo/AOL/247WallSt re-syndication) | MAJOR | REVIEW（fast-path，跳過 body fetch） | `_process_item` |
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

## §schema-gap-watch（2026-04-28 起，instrumentation）

### 為什麼留這個 watch

`relevance_type` 目前只有 4 桶（`company-specific` / `sector-policy` / `macro-tangential` / `buzzword-list-only`），但實際新聞至少有 7-8 種：

| 案例 | 應該對應的 bucket | 現狀 LLM 怎麼處理 |
|------|------------------|------------------|
| Earnings, M&A, FDA approval, exec change | `company-news`（缺） | 強塞 `company-specific` + `should_alert=true` |
| 「6 個月前我說...」回顧文 | `company-recap`（缺） | 強塞 `company-specific` + `should_alert=false`（**veto**） |
| 13F、機構增減持、insider trade | `company-holdings`（缺） | 強塞 `company-specific` + `should_alert=false`（**veto**） |
| Roth Capital 評級調整 | `company-analyst`（缺） | 強塞 `company-specific` + `should_alert=false`（**veto**） |
| Sector regulation explicitly impacting target | `sector-policy` | OK |
| 鈾現貨價漲（不直接提 UUUU） | `macro-tangential` | OK |
| 「Top 10 stocks」名單 | `buzzword-list-only` | OK |

當 LLM 想表達「相關但不該打擾」，schema 沒地方放，就用 `should_alert=false` 當隱性閾值 veto。Contract 漏洞，不是 LLM 的錯。

### 怎麼偵測

`decide_tier` 加 counter `schema_gap_suspicious_veto`：當 LLM 說 `is_relevant=true` AND `relevance_type ∈ {company-specific, sector-policy}` 但又 `should_alert=false`，計一筆。

機械可數，每天累積到 `qc/daily-report-YYYY-MM-DD.json` 的 `counters` 欄位：

```json
"counters": {
  "reason:llm_should_not_alert": N,                  // 所有 should_alert=false 的數量
  "reason:schema_gap_suspicious_veto": M             // 其中 M 是 schema gap 子集
}
```

### 一週後的決策樹（嚴格門檻）

跑滿 1 週（≈42 個 polls）後，分析 fresh 流量裡 `schema_gap_suspicious_veto` 占比：

```bash
# 累積 7 天 daily reports（auto-committed）
jq -s 'map(.counters) | reduce .[] as $c ({}; . * $c)' qc/daily-report-2026-04-*.json
```

| 占 fresh 比例 | 判讀 | 行動 |
|---------------|------|------|
| **> 20%** | schema 真的漏接，LLM 在 hack | 做 modification A：`relevance_type` 擴成 7 桶（`company-news` / `company-recap` / `company-holdings` / `company-analyst` / `sector-policy` / `macro-tangential` / `buzzword-list-only`），should_alert 改為 pipeline-derived |
| 5%-20% | 邊界 | 看擋下的 case 是哪種 pattern，挑一桶優先擴（例如只加 `company-analyst`） |
| **< 5%** | 現狀可接受 | 不動 |

不要拍腦袋先決定 — 用實際 7 天分布做。

### FP / FN 不對稱（為什麼不是「保守=安全」）

| 錯誤 | 成本 | 可觀測性 |
|------|------|----------|
| False positive（誤發 alert） | Alert fatigue，使用者忽略 | 看得到（使用者抱怨 / unsub） |
| False negative（漏發 alert） | 錯過 trading opportunity | **看不到**，silent failure |

漏一條 8-K 或 FDA approval 不會主動敲門。所以系統設計應 **surface 訊號 + 用戶決定要不要看**，不是替用戶過濾。`schema_gap_suspicious_veto` 就是用來量測「我們可能漏發了多少」。

### v2 候選：5% DROP 隨機 audit

目前 self-consistency 只跑 HIGH/MEDIUM tier — DROP path 沒 cross-model 驗證，是單邊覆蓋。如果 1 週後 `schema_gap_suspicious_veto` 占比真的高，可加：對 5% 隨機抽樣的 DROP 也跑 Sonnet auditor，比對 verdict 是否 disagree。Disagree 的 case 進 REVIEW，作為 schema 缺口的 ground truth corpus。

成本：LLM 呼叫量多 5%。延後到看到實際 schema gap 訊號再做。

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
