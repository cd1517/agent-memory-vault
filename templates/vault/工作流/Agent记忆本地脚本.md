---
memory_type: workflow
track: workflow
project_id: agent-memory-vault-scripts
app_id: {{APP_ID}}
user_id: {{USER_ID}}
agent_id: {{AGENT_ID}}
session_id: ""
status: active
sensitivity: normal
verified_at: 2026-06-20
keywords:
  - scripts
  - sqlite
---

# Agent 记忆本地脚本

## 当前有效摘要

本模板提供以下本地脚本：

- `agent_memory_index.py`：全库 Markdown 索引和搜索。
- `agent_memory_search.py`：统一检索入口，合并 SQLite、可选 Zvec 和手动 rg 结果。
- `agent_memory_safety.py`：在检索和对账前检查来源、知识类型和敏感信息。
- `agent_memory_closeout.py`：任务结束收尾，负责检查、对账、刷新索引、捎带 audit 和可选 scoped commit。
- `agent_memory_claim.py`：记录当前会话负责的记忆文件，并预览或显式过期异常退出遗留的旧认领。
- `agent_memory_intent.py`：给高影响文件建立内容绑定的写入意图、批准记录和不可变回执。
- `agent_memory_audit.py`：定期体检，发现过期记忆、重复标题、open-loop 噪声和已过时状态。
- `agent_memory_audit_autorun.py`：自动触发器，只在超过设定间隔时运行内容 audit，并顺带执行只读 Doctor，把基础设施健康报告写入 `latest-doctor.json`。
- `agent_memory_doctor.py`：统一体检 Markdown、SQLite、FTS、INDEX、Zvec、远端备份、会话认领、语义 Python、验证来源和自动化状态。
- `agent_memory_stop_hook.py`：Stop 事件节流提醒；到期 audit 仍由 7 天闸门决定是否执行。
- `agent_memory_evolution.py`：Agent case 和 skill 候选状态统计。
- `agent_memory_check.py`：结构、frontmatter、SQLite、泄密风险检查。
- `agent_memory_zvec_index.py`：可选 Zvec 语义索引和搜索。
- `agent_memory_retrieval_benchmark.py`：对比 SQLite 和向量检索召回效果。
- `agent_memory_decision_outcomes.py`：检查已有决策是否补了结果、复盘日期和证据。

## 环境变量

```bash
AGENT_MEMORY_ROOT=/path/to/your/agent-memory-vault
AGENT_MEMORY_GIT_ROOT=/path/to/git-root-containing-the-vault
AGENT_MEMORY_CONFIG_ROOT=$HOME/.config/agent-memory
AGENT_MEMORY_STATE_DB=$HOME/.config/agent-memory/state.sqlite
AGENT_MEMORY_USER_ID=demo-user
AGENT_MEMORY_AGENT_ID=codex
AGENT_MEMORY_APP_ID=codex
AGENT_MEMORY_AUDIT_DB=$HOME/.config/agent-memory/audit_decisions.sqlite
AGENT_MEMORY_CLOSEOUT_LOG=$HOME/.config/agent-memory/logs/closeout.jsonl
AGENT_MEMORY_PYTHON=python3
AGENT_MEMORY_ZVEC_PYTHON=python3
AGENT_MEMORY_VECTOR_DIR=$HOME/.config/agent-memory/zvec/memory_chunks_embeddinggemma_768
AGENT_MEMORY_EMBEDDING_MODEL=google/embeddinggemma-300m
```

## 常用命令

```bash
python3 scripts/agent_memory_index.py --init --scan --report
python3 scripts/agent_memory_search.py "关键词" --limit 5
python3 scripts/agent_memory_closeout.py --prewrite "准备写入的记忆摘要" \
  --source-class local_verified --knowledge-kind fact \
  --asserted-by codex --evidence-ref "local-check:example"
python3 scripts/agent_memory_closeout.py --dry-run
python3 scripts/agent_memory_closeout.py --commit
python3 scripts/memoryctl --actor codex claim --file "/absolute/path/to/memory.md"
python3 scripts/memoryctl --actor human claims-expire --older-than-hours 24 --json
python3 scripts/agent_memory_audit.py
python3 scripts/agent_memory_audit_autorun.py --reason manual --json
python3 scripts/agent_memory_doctor.py
python3 scripts/agent_memory_evolution.py --init --scan --report
python3 scripts/agent_memory_check.py
python3 scripts/agent_memory_zvec_index.py --init
python3 scripts/agent_memory_zvec_index.py --scan --prune
python3 scripts/agent_memory_zvec_index.py --report
python3 scripts/agent_memory_zvec_index.py --search "只记得大概意思的问题" --limit 5
python3 scripts/agent_memory_retrieval_benchmark.py --limit 5
python3 scripts/memoryctl --actor codex decision-outcomes --json
```

## 检索边界

- `zvec_raw_distance` 是模型原始距离，只用于阈值和写入对账；`zvec_rank_distance` 只用于排序。rank 再靠前，也不能单独触发写入。
- 项目任务用 `--current-project <project-id>`；任何带有非 `global/shared` `project_id` 的记忆都默认硬隔离，显式 `--cross-project` 才能看其他项目类比，且不能据此授权动作。
- 没有 `project_id` 的内容按未限定共享参考处理；只有明确标成 `global` 或 `shared` 才是全局共享。检索结果始终不替代当前授权或实时确认。
- `valid_until` 到期的条目仍会召回并标记 `expired`，但必须实时核验。

## 高影响文件写入

把少量精确路径放入 TOML 的 `[write_intents].protected_paths`。`enforcement` 可用 `off`、`advisory`、`enforce`，升级时先小范围观察，再切到强制。

顺序必须是：先在 vault 外准备 UTF-8 提案；用带完整来源参数的 `prewrite --create-intent --target-file ... --proposal-file ...` 创建意图；若结果要求批准，再执行 `memoryctl ... intent approve`，并同时绑定 raw hash、canonical hash、批准人和只入库哈希的 `approval_ref`；编辑前执行带 `--intent-id` 的 `claim`；最后由 closeout 校验内容和 Git 版本链并写 receipt。只差 BOM、换行符或 Unicode 规范化可记为 `format_only`；Markdown 行尾两个空格有语义，实质内容不一致默认只显示 diff 哈希和统计并停止，显式私密 diff 也必须先脱敏凭证行。外部工具提前提交时，只有内容和版本链都匹配才作为 `early_commit` 恢复。本地 approval CLI 只能作为同一信任域内的审批见证，不能冒充宿主 UI 签发的独立用户签名。

真实 benchmark 文件必须留在公开仓库之外。默认报告只显示 id、哈希、长度和名次；不要开启明文详情，除非正在本机人工排查并明确接受暴露。

## 下次优先看

- 修改目录结构后，先更新字段规范，再跑检查脚本。
