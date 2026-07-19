# Privacy Checklist

这个模板不是你的真实记忆库。真实信息应该留在本地私有 vault 里。

Claude Code 与 Codex 可以读取同一个私有配置源，但真实 Cookie、token 和 API key 仍然不能进入 Markdown vault、CLAUDE.md、AGENTS.md、auto-memory、搜索日志或公开仓库。推荐把私有值放在 Git 之外、权限为 `600` 的结构化文件或系统 Keychain 中，并通过 runner 只向目标子进程注入所需变量；不要让 Agent 打印整个 secrets 文件。

## 永远不要放进模板

- `.env`
- SQLite 数据库：`*.sqlite`、`*.db`
- audit 裁决库：`audit_decisions.sqlite`
- closeout/audit 运行日志：`logs/*.jsonl`
- API key、token、cookie、密码
- Hugging Face token、模型缓存
- Zvec / LanceDB / Qdrant 等派生向量库
- 真实聊天记录
- 私有项目名和客户名
- 合同、报价、账号、手机号、邮箱、身份证、银行卡
- 真实 Obsidian vault 全量内容

## 推荐做法

- 模板只放 `templates/`、`scripts/`、`docs/`、假示例。
- 本地真实记忆库放在另一个不公开的位置。
- `.env.example` 只放变量名和占位符。
- 文档里的路径使用 `/path/to/...` 或 `$HOME/...`。
- 示例项目统一使用 `example-app`、`demo-user` 这类假名。
- closeout/audit 只能公开脚本，不能公开本地运行产物。
- 公开 benchmark 只用假数据；真实检索、对账和安全边界样本放在 Git 之外。
- 私有 benchmark 默认只输出不可逆的 case reference、哈希、长度、名次和汇总指标，不输出原始 case id、查询原文、候选正文或绝对路径。
- `--show-private-details` 只用于本机人工排查；运行前要明确接受终端和日志里出现明文的风险。
- Runtime 私有目录固定为 `700`；state DB、SQLite sidecar、配置文件和持久日志固定为 `600`，每次打开都会拒绝 symlink 并复核权限。
- 写入意图仅在私有 state DB 中保存有上限的 canonical proposal snapshot。内容不一致时默认只返回 diff 哈希和统计；只有显式 `--show-private-diff` 才返回有界且经过凭证行脱敏的 diff。对外 `show`、receipt、搜索索引和 closeout JSONL 不保留 diff 正文。

## 本地检查命令

```bash
find . -name "*.sqlite" -o -name "*.db" -o -name ".env" -o -name "*.key" -o -name "*.pem" -o -name "zvec" -o -path "*/logs/*.jsonl"
python3 scripts/agent_memory_check.py
```

如果检查结果出现真实 key 或真实路径，先从模板里移除。
