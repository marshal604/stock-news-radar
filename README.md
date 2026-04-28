# stock-news-radar

UUUU + TEM 即時新聞 radar。每 4 小時輪詢多個來源（SEC EDGAR、Finviz、Google News），經 4 層 oracle 驗證後推送 Discord。

**LLM 模型分配**：

| 角色 | 預設 | 任務 |
|------|------|------|
| Primary classifier | **Claude Opus 4.7** | relevance / sentiment / should_alert 判斷 + chinese_summary + impact_assessment |
| Auditor (self-consistency) | **Claude Sonnet 4.6** | cross-verify primary（differential signal — 必須跟 primary 不同 model） |
| EDGAR translator | **Claude Haiku 4.5** | 8-K 標題翻譯（純翻譯，下游有 numeric_guardrail 安全網） |

模型 ID 透過 env var override（換代不用改 code）：
- `RADAR_PRIMARY_MODEL`（預設 `claude-opus-4-7`）
- `RADAR_AUDITOR_MODEL`（預設 `claude-sonnet-4-6`）
- `RADAR_TRANSLATE_MODEL`（預設 `claude-haiku-4-5`）

## Architecture

採用 [harness-engineering](../harness-engineering/cheatsheet.md) 的 6 步法：

```
Phase 1 Collect       → 多源 RSS/scrape，標 source confidence
Phase 2 Dedup         → SQLite seen-store，當天範圍
Phase 3 Substring     → LLM mention_quotes 必為 verbatim 子字串（CRITICAL gate）
Phase 4 Differential  → keyword path A vs LLM path B 三角驗證
Phase 5 Self-consist  → high-tier 跑兩次不同 prompt，不一致降級
Phase 6 Discord post  → 按 tier 路由 + 中文摘要
```

詳細規則見 [config/alert-rules.md](config/alert-rules.md)。

## Setup

1. 本機產 OAuth token：`claude setup-token`
2. 推 repo 到 GitHub
3. Repo Settings → Secrets and variables → Actions：
   - `CLAUDE_CODE_OAUTH_TOKEN` = 第一步拿到的 `sk-ant-oat01-...`
   - `DISCORD_WEBHOOK_URL` = Discord 頻道 webhook URL
4. workflow 自動每 4 小時跑一次（cron `0 */4 * * *`）。

## 本機 debug

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
npm install -g @anthropic-ai/claude-code   # 或：claude --version 確認已裝

# 已用 `claude` 登入過的 Mac 不需要再 export token
export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

.venv/bin/python -m src.main --dry-run     # 跑 pipeline 但不發 Discord
.venv/bin/python -m src.main               # 完整跑
```

## 測試

```bash
.venv/bin/python -m pytest                 # 13 個 deterministic tests（substring + keyword）
.venv/bin/python -m pytest -m llm          # 加跑 LLM scenario tests（消耗 subscription quota）
```

## 結構

```
config/         crystallized decision artifacts (json + md)
src/            pipeline + sources + oracles
data/           runtime state（gitignored）
golden-set/     scenario corpus（baseline + edge + regression）
qc/             daily QC reports（gitignored）
tests/          oracle + scenario tests
```
