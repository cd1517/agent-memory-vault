# Windows 原生使用指南

支持 Windows 10/11、Python 3.10+ 和 Git。PowerShell 7 优先，也兼容 Windows PowerShell 5.1；Obsidian 可选。

## 安装

在仓库根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1 `
  -MemoryRoot "$HOME\Documents\Agent Memory Vault"
```

`Bypass` 只作用于这一个 PowerShell 进程。安装器会检查 Python/Git、创建私有虚拟环境、安装可校验 Runtime、从 Runtime 自带模板初始化 Vault、创建首个 Git 基线、初始化 SQLite/INDEX，并运行 check 与 doctor。它不会安装可选的大型向量依赖。

可选同时安装 Codex Stop Hook 和每周 audit：

```powershell
.\scripts\install-windows.ps1 `
  -MemoryRoot "$HOME\Documents\Agent Memory Vault" `
  -InstallCodexHook -AutoCloseout -InstallAuditTask
```

路径均作为独立参数传递，带空格和中文的路径不需要转换成短路径。

## 日常命令

```powershell
$runtime = Join-Path $env:LOCALAPPDATA 'AgentMemoryVault'
$python = Join-Path $runtime '.venv\Scripts\python.exe'
$memoryctl = Join-Path $runtime 'scripts\memoryctl'
& $python $memoryctl --actor codex search "项目状态" --limit 5
& $python $memoryctl --actor codex closeout --dry-run
& $python $memoryctl --actor codex closeout
& $python $memoryctl --actor human doctor
```

Python 会直接加载 Runtime TOML，PowerShell 不需要模拟 Bash 的 `source .env`。

## Codex Stop Hook

单独安装时运行：

```powershell
.\scripts\install-codex-hook.ps1 -AutoCloseout
```

安装器会保留 `hooks.json` 中其他 Hook，只追加当前 Runtime 的 wrapper。确认 `%USERPROFILE%\.codex\config.toml` 已启用 Hooks：

```toml
[features]
hooks = true
```

PowerShell wrapper 从 stdin 原样接收事件 JSON，再通过当前 Runtime 的 Python 运行 `agent_memory_stop_hook.py`。Python 核心仍负责 session claim、SQLite/INDEX、去重、closeout 和可选 Git commit。

## Task Scheduler audit

```powershell
.\scripts\audit-task.ps1 install
.\scripts\audit-task.ps1 status
.\scripts\audit-task.ps1 run
```

默认任务名为 `AgentMemoryVaultAudit`，以当前用户和 Limited 权限运行。重复 `install` 会更新同名任务，不会创建副本。`uninstall` 会删除计划任务，只有明确需要移除时才运行。

## Obsidian 与 Doctor

在 Obsidian 中选择“Open folder as vault”并打开 `-MemoryRoot` 目录即可。模板已经忽略 `.obsidian/`，因此界面状态不会污染 Git 或 closeout 变更识别。

```powershell
& $python (Join-Path $runtime 'scripts\agent_memory_doctor.py')
```

Zvec 未启用时不会在 closeout 中启动，SQLite 搜索仍可正常使用。

## 常见问题

- `running scripts is disabled`：使用上面的单进程 `-ExecutionPolicy Bypass`，不要永久设置 `Unrestricted`。
- `python not found`：安装 Python 3.10+，并启用 `py.exe` 或把 Python 加入 PATH。
- 中文乱码：使用仓库 wrapper；它会为 Python 子进程固定 UTF-8 I/O，Git 输出也按 UTF-8 解码。
- Task Scheduler 不运行：先执行 `status`，再确认用户已登录、Python 与 Runtime 路径仍存在。
- Runtime 脱离源码后不能 bootstrap：重新运行安装器；当前 manifest 会校验 `templates/vault` 是否完整。
