# Alert Tier Rules

決定一條新聞要不要發 Discord、發到哪一級。Pipeline phase 4 (differential) + phase 5 (self-consistency) 的輸出餵進這張表。

## Tier 定義

| Tier | 觸發條件 | Discord 表現 |
|------|---------|--------------|
| **CRITICAL** | source=edgar (8-K filing) AND substring oracle pass | 🔴 紅燈 + ping 提醒 + 長摘要 |
| **HIGH** | source∈{edgar, yahoo, finviz} AND keyword path A=yes AND llm path B=yes AND self-consistency pass | 🟢 綠燈（利多） / 🔴 紅燈（利空）+ 短摘要 |
| **MEDIUM** | source=google_news AND keyword=yes AND llm=yes AND self-consistency pass | 🟡 黃燈 + 短摘要 |
| **REVIEW (silent)** | differential disagreement (A=yes, B=no) OR self-consistency fail | 不發 channel；寫進 qc/daily-report.json |
| **DROP (silent)** | substring oracle fail OR ticker not in source OR exclude_strict hit OR already_sent | 不發；寫 processed-log.ndjson 紀錄原因 |

## 為什麼這樣切

- **CRITICAL 直發、不靠 LLM**：8-K 是公司自己發的法定公告，substring 驗證過 ticker 在文中即可。LLM 只用來生中文摘要不用來做 relevance 判斷（fail-loud：LLM 出包不該影響 8-K 通報）。
- **HIGH 要 A & B 都 yes**：ticker-specific source 已經是高 precision，再過 keyword + LLM 雙向驗證。
- **MEDIUM 用於 keyword 來源**：Google News 拉的是 topic-level，相關性低，LLM oracle 角色更重；明顯標記 medium 讓使用者知道是「可能相關」。
- **REVIEW 不丟**：differential disagreement 是最有研究價值的 case（要嘛 keyword 漏判要嘛 LLM 過度推論），保留作為 golden-set 候選。
- **DROP 寫 log**：用戶要能 query「為什麼這條沒發」，符合 fail-loud 原則。

## sentiment 跟 category（不影響 tier，影響 Discord 表現）

LLM 在 magenta-key contract 裡同步輸出：

- `sentiment ∈ {bullish, bearish, neutral, mixed}` → emoji 顏色（🟢🔴⚪🟡）
- `category ∈ {earnings, regulatory, M&A, analyst, rumor, macro, partnership}` → tag 標籤

## QC signal 對應 (對應 harness step 5)

| Signal | 嚴重度 | 處理 |
|--------|--------|------|
| `quote_not_in_source` | CRITICAL | 不發；計入 LLM 引用幻覺率；scenario regression |
| `ticker_not_in_source` | CRITICAL | 直接 DROP |
| `date_too_old` (>24h) | CRITICAL | DROP（user 規格：「只抓當天」） |
| `already_sent` | CRITICAL | DROP |
| `differential_disagreement` | MAJOR | REVIEW，不發 |
| `self_consistency_fail` | MAJOR | 重跑一次再決定，再 fail 就 REVIEW |
| `source_not_whitelisted` | MAJOR | DROP（防擴散） |
| `relevance_type=buzzword-list-only` | MAJOR | DROP |
| `sentiment_title_body_mismatch` | MINOR | 標 review 但仍發 |
| `multi_ticker_no_distinction` | MINOR | 標 review 但仍發 |

## 反飼料校準（v2，先佔位）

Discord 訊息加 react 👍 / 👎 → bot 收 reaction → 寫進 `golden-set/positive/` 或 `golden-set/negative/`。每週跑 scenario regression 看誤判率。
