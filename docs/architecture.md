# Architecture

## 1. Markdown source of truth

所有长期记忆都先写成 Markdown。这样做的好处是：

- 人可以直接打开、编辑、diff。
- Git 可以追踪变化。
- Obsidian 可以作为可选的可视化入口；不安装 Obsidian 时，它也只是一个普通 Markdown 文件夹。
- 即使 SQLite 坏了，原始记忆也还在。

SQLite 只负责索引，不负责成为唯一事实源。

## 2. Local stack

这套模板的默认本地链路是：

- Markdown：保存正式记忆。
- SQLite：保存文件索引、搜索字段、未闭环事项和 Agent case 状态。
- Session claims：在 SQLite 中记录“哪个会话负责哪些 Markdown”，避免两个 Agent 串提交。
- Source safety：在检索和对账前判断来源、知识类型与敏感内容，只保存哈希和分类。
- Write intents：对少量高影响路径，把目标、基础版本、提案哈希、批准和会话绑定起来，closeout 后生成不可变回执。

来源安全是所有写入的操作规范，但技术上的 fail-closed 只覆盖配置中的少量高影响路径。普通笔记仍依赖 Agent 先运行 prewrite；这是明确的低摩擦边界，不是恶意本地调用者无法绕过的安全边界。
- File observations：成功 closeout 后记录文件内容 hash；它证明某一版内容已经完成检查、索引与收尾，不能用全库索引时顺带扫到来冒充。
- Git：保存修改记录，支持 scoped commit 和回滚。
- 统一搜索脚本：合并关键词搜索、字段过滤、可选语义召回和手动 rg。
- closeout 脚本：在任务结束时自动检查、对账、刷新索引、写日志，并可选择提交本轮记忆文件。
- audit 脚本：定期发现过期、重复、open-loop 噪声和已过时状态，裁决结果存在本地 SQLite 中。

可选语义检索层是：

- Embedding model：把 Markdown chunk 和查询语句转成向量。
- Zvec：保存向量，并做相似度检索。

向量层不替代 SQLite。SQLite 继续负责路径、字段、FTS、open-loop 和正交过滤；Zvec 只负责“意思相近”的候选召回。

统一搜索会并行查询 SQLite/FTS 与 Zvec，合并去重后再统一执行 `track`、`memory_type`、`project_id`、`status` 等筛选。语义距离超过阈值的结果直接丢弃，因此向量库不会为了凑足数量而返回明显无关的记忆。这里严格分开原始距离和排序距离：raw 只负责阈值与写入对账，rank 只负责把候选排得更好看，不能反过来触发写入。

## 3. Shared core and host adapters

Claude Code 与 Codex 共用 Markdown、Git、SQLite、Zvec、closeout 和 audit。每个宿主只保留自己的规则入口与 Hook：Claude 使用 `CLAUDE.md` 导入共享 `AGENTS.md`，Codex 直接读取 `AGENTS.md`。

普通事实默认 `agent_scope: shared`，这个字段决定可见范围；`agent_id` 只记录来源。`created_by` 和 `last_updated_by` 记录来源；closeout 日志另外记录 actor、trigger、session hash 和 run id。不要为每个 Agent 建独立 Git 基线或独立向量库。

并发控制和写入授权是两件事。全局文件锁保证 SQLite/Zvec/Git 操作不会同时执行；`memory_session_claims` 保证每次 closeout 只处理当前会话自己的文件；`memory_file_observations` 以内容 hash 记录别的会话已经处理完某一版文件；写入意图记录“本地流程声称批准的是这个目标的这一版内容”。同一 Agent 可调用的 approval CLI 不是独立用户签名；没有宿主 UI 或独立人工通道签发的一次性回执时，它只能防误操作，不能对抗已控制本地账号的恶意程序。这些机制不能互相替代。

## 4. User memory and Agent memory

用户记忆和 Agent 记忆分开：

- `用户记忆/`：用户偏好、边界、长期画像。
- `agent/`：Agent 的可复用案例、失败教训、skill 候选、未闭环事项。

这样不会把“用户是谁”和“Agent 怎么做事”混在一起。

## 5. Orthogonal retrieval

正交检索就是用多个互不冲突的字段过滤记忆。

例如同一条记忆可以同时有：

```yaml
memory_type: project
track: project
user_id: demo-user
agent_id: shared
agent_scope: shared
app_id: agent-memory
project_id: example-app
session_id: ""
status: active
```

以后搜索时可以说：

- 只看某个项目：`--project-id example-app`
- 只看用户记忆：`--track user`
- 只看工作流：`--memory-type workflow`
- 只看有未闭环事项的文件：`--has-open-loop`

它的价值不是让目录更复杂，而是减少 Agent 每次读取无关内容。

项目边界由 `project_id` 决定，而不是由目录或 `track` 决定。传入当前项目后，只要记忆带有非 `global/shared` 的项目标识，就只在对应项目返回；显式请求跨项目时也只能标为类比线索。没有项目标识的内容是未限定共享参考，只有明确标成 `global` 或 `shared` 才视为全局共享。无论哪条轨道，检索命中本身都不构成执行授权。

时间边界与召回边界也分开。`valid_until` 过期不会抹掉历史，搜索仍可返回它；结果会要求实时核验，避免把“过去正确”误当成“现在仍正确”。

## 6. Semantic retrieval sidecar

语义检索适合这些问题：

- 用户只记得大概意思，不记得文件名或关键词。
- 同一件事有多种说法，例如 “closeout”“收尾”“对话结束归档”。
- 记忆库变大后，需要先用本地索引缩小候选文件。

查询建议：

1. 默认使用 `agent_memory_search.py`。
2. 关键词、项目名、路径、字段明确时，SQLite/FTS 会给出稳定结果。
3. 表达模糊时，可以启用 Zvec 做语义候选召回。
4. Zvec 命中的 chunk 只作为候选，最终仍然回读 Markdown 原文。

### Canonical read boundary

`agent_memory_retrieve.py` 是“候选召回”和“把正文交给宿主”之间的只读边界。它调用
统一搜索的 `--no-log` 路径缩小候选，但不信任候选中的绝对路径、摘要、hash 或
frontmatter。每个候选都通过 `agent_memory_intent.canonical_target` 的同一套 containment
与 symlink 规则，随后以严格 UTF-8 重新读取当前 Markdown，并再次应用 active、
agent scope、app 和 project 过滤。

对 `yichen-content-studio`，app id 是 Runtime 固定的中央边界而不是可选筛选器。项目
请求只返回精确项目；省略项目的创作请求只返回同 app 的 global/shared/unscoped
内容。该 actor 的查询只从 stdin JSON 进入，中央 `memoryctl` 不接受 argv query，且
只暴露 `retrieve`、`write`、`version`。

输出只保留相对路径、当前内容 hash、验证/时效策略、Git HEAD 和有字节上限的 excerpt；
查询只保留规范化 SHA-256。单个 stale/missing/oversized/invalid 候选不会拖垮整次读取，
而是成为不含绝对路径和正文的结构化 warning。Vault 根目录不可用或参数协议不安全才
fail closed。该入口不迁移 SQLite schema、不写搜索日志，也不修改 Markdown。

### Canonical host write boundary

`agent_memory_write.py` 把宿主 UI 的正式记忆写入压成 `read-target` /
`prepare` / `apply` / `cancel` 状态转换。请求正文只经 stdin 进入进程，全路径按
`yichen-content-studio` 独立 session 授权，不接受 Codex 或 Claude 宿主会话回退；
session 只从 `AGENT_MEMORY_SESSION_ID` 环境变量读取，中央 wrapper 和子命令 parser
都拒绝显式 session argv 与 argparse 长选项缩写。

`read-target` 用 canonical target 边界严格读取不超过 2 MiB 的 UTF-8 Markdown，
先按当前 frontmatter 复核 active/shared/app/project 与 secret，再返回完整正文、当前
raw/canonical hash、Git HEAD、base existence 和 session/target/scope 绑定的 opaque
read token。它不取全局写锁，也不写 state DB、
日志或 Vault。`UPDATE` 必须在这份全文上生成完整最终版；不能用一段
Agent 回答代替旧文件。

`prepare` 必须带回该 read token；缺 token 或 read 后发生的内容、存在性、scope、Git
HEAD 漂移都会在 Studio 写锁内作为 stale CAS 拒绝，ADD 的 missing token 也不能抢占
后来创建的文件。随后它先过 source safety，再从只读、不记录查询且 query 只走 stdin
的检索路径取候选，并用
canonical target 规则重新读取目标。它最多生成一个绑定 session、目标、Git
基线、read token、app/project scope 和提案 hash 的 intent，不改 Markdown。proposal
必须显式为 active、shared、固定 app，项目声明必须与通道一致。`NOOP` 和 `MERGE_REQUIRED` 不生成
可 apply 的 ID。

`apply` 必须带回同一 ID、目标、两种 hash、完整提案和显式用户确认
reference。它在全局文件锁下再次验证当前基线和意图状态，然后按
`claim → atomic conditional write → session-scoped closeout` 执行。ADD 通过同目录
hard link 提供 no-replace 语义；UPDATE 通过 `renamex_np(RENAME_SWAP)`（macOS）或
`renameat2(RENAME_EXCHANGE)`（Linux）原子交换目标与提案，然后校验被换出的旧字节。
旧字节不等于准备基线时，先创建独立恢复副本再原子换回。只有目标和返回的提案都
精确通过 hash 校验才清理临时文件并返回普通竞态失败；二次竞态、崩溃遗留或文件系统
不支持安全 primitive 时一律 fail closed，保留恢复 sidecar，且不进入 closeout。
任一绑定变化也会 fail closed；写入后收尾失败则保留认领与现场，不用旧正文静默回滚覆盖。
此时 `cancel` 会先比对目标与 base；只有完全未写时才可取消并释放 claim，
已等于 proposal 或发生其他变化时返回 `APPLY_RECOVERY_REQUIRED`。

apply 派生的 closeout transport 单独创建 process group。timeout、取消或宿主 shutdown
都执行 TERM → bounded wait → KILL → reap，并等到整个 group 消失后才让外层 Studio
写锁释放，避免遗留的 Git/index/Zvec 孙进程晚到写入。

## 7. Closeout and audit loop

closeout 是每次任务结束后的自动整理员。它不替 Agent 判断“什么值得记”，但会把收尾动作压成稳定流程：

- 读取当前会话认领的记忆文件，并排除其他会话文件。
- 检查是否有敏感内容、结构问题或膨胀文件。
- 对新文件做写入后查重，发现重复时输出 `MERGE_REQUIRED`。
- 对受保护路径校验写入意图、批准绑定、基础版本和最终提案内容；不匹配时输出 `ASK_USER`。
- 刷新 SQLite 和可选 Zvec。
- Zvec 全量扫描会补齐漏项，并清理已删除、重命名或不再合格的旧向量；“已过时信息/旧方案”等历史段落默认不进入当前事实向量。
- 必要时刷新 Agent evolution。
- 检查 audit 是否超过间隔，超过则捎带运行。
- 记录 closeout 日志，并在允许时只提交本轮记忆文件。
- 日志保存 `git_observed_through` 基线；即使其他备份工具先提交，下一次 closeout 也会从 Git 历史找回尚未处理的记忆变更。
- 如果其他本地工具提前提交，只有连续 Git 版本链中存在与提案匹配的内容，才记录 `early_commit` 并生成成功回执。

audit 是定期体检。它只产出 findings 和裁决记录，不直接改写 Markdown 事实层。除过期、重复和 open-loop 外，它还读取机器可读不变量，检查当前摘要里的旧路径、退役脚本、错误 scope 和已经漂移的固定计数。

`agent_memory_doctor.py` 是统一体检入口，核对 Markdown、SQLite、FTS、INDEX、Zvec 路径与 hash、Git 基线与远端备份时效、会话认领残留、验证来源、日志隐私、Runtime manifest、模型文件 hash、语义 Python 基础解释器、依赖锁、完全离线语义查询和自动化新鲜度。默认只读；`--repair-derived` 也只重建可再生索引。

## 8. Self evolution

普通记忆不设候选池，直接进入正式目录。

但 Agent 自我进化保留两类候选：

- `agent/case-candidates/`：某次任务中可能可复用的方法。
- `agent/skill-candidates/`：多次复用后，可能值得沉淀为正式 skill 的流程。

脚本只做统计和提醒，不自动把候选升级为正式 skill。正式升级前应该由用户确认。
