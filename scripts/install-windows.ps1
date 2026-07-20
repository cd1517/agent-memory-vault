[CmdletBinding()]
param(
    [string]$MemoryRoot = (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Agent Memory Vault'),
    [string]$ConfigRoot = (Join-Path $env:LOCALAPPDATA 'AgentMemoryVault'),
    [string]$UserId = 'demo-user',
    [string]$AgentId = 'shared',
    [string]$AppId = 'agent-memory',
    [switch]$InstallCodexHook,
    [switch]$AutoCloseout,
    [switch]$InstallAuditTask
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$venvRoot = Join-Path $ConfigRoot '.venv'
$venvPython = Join-Path $venvRoot 'Scripts\python.exe'

function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$Label) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Label failed with exit code $LASTEXITCODE" }
}

$git = Get-Command git.exe -ErrorAction SilentlyContinue
if (-not $git) { throw 'Git was not found in PATH.' }
$python = Get-Command py.exe -ErrorAction SilentlyContinue
$prefix = @('-3')
if (-not $python) { $python = Get-Command python.exe -ErrorAction SilentlyContinue; $prefix = @() }
if (-not $python) { throw 'Python 3 was not found in PATH.' }
$version = & $python.Source @prefix -c 'import sys; print(sys.version_info.major * 100 + sys.version_info.minor); raise SystemExit(sys.version_info < (3, 10))'
if ($LASTEXITCODE -ne 0) { throw "Python 3.10 or newer is required; detected version code $version" }

New-Item -ItemType Directory -Force -Path $ConfigRoot | Out-Null
if (-not (Test-Path -LiteralPath $venvPython)) {
    Invoke-Checked $python.Source ($prefix + @('-m', 'venv', $venvRoot)) 'virtual environment creation'
}
Invoke-Checked $venvPython @((Join-Path $repoRoot 'scripts\install_runtime.py'), '--config-root', $ConfigRoot) 'runtime installation'
$runtimeScripts = Join-Path $ConfigRoot 'scripts'
$stateDb = Join-Path $ConfigRoot 'state.sqlite'
Invoke-Checked $venvPython @(
    (Join-Path $runtimeScripts 'bootstrap.py'), '--memory-root', $MemoryRoot,
    '--config-root', $ConfigRoot, '--state-db', $stateDb, '--git-root', $MemoryRoot,
    '--user-id', $UserId, '--agent-id', $AgentId, '--app-id', $AppId, '--init-git'
) 'vault bootstrap'

function TomlPath([string]$Path) { return $Path.Replace('\', '/') }
function TomlString([string]$Value) { return $Value.Replace('\', '\\').Replace('"', '\"') }
$configDir = Join-Path $ConfigRoot 'config'
New-Item -ItemType Directory -Force -Path $configDir | Out-Null
$configPath = Join-Path $configDir 'agent-memory.toml'
$toml = @"
memory_root = "$(TomlString (TomlPath $MemoryRoot))"
git_root = "$(TomlString (TomlPath $MemoryRoot))"
config_root = "$(TomlString (TomlPath $ConfigRoot))"
state_db = "$(TomlString (TomlPath $stateDb))"
audit_db = "$(TomlString (TomlPath (Join-Path $ConfigRoot 'audit_decisions.sqlite')))"
closeout_log = "$(TomlString (TomlPath (Join-Path $ConfigRoot 'logs\closeout.jsonl')))"
audit_run_log = "$(TomlString (TomlPath (Join-Path $ConfigRoot 'logs\audit_runs.jsonl')))"
audit_report = "$(TomlString (TomlPath (Join-Path $ConfigRoot 'reports\latest-audit.json')))"
python = "$(TomlString (TomlPath $venvPython))"
user_id = "$(TomlString $UserId)"
agent_id = "$(TomlString $AgentId)"
app_id = "$(TomlString $AppId)"

[semantic_retrieval]
enabled = false
python = "$(TomlString (TomlPath $venvPython))"
"@
[System.IO.File]::WriteAllText($configPath, $toml, [System.Text.UTF8Encoding]::new($false))
$env:AGENT_MEMORY_CONFIG_FILE = $configPath
Invoke-Checked $venvPython @((Join-Path $runtimeScripts 'agent_memory_evolution.py'), '--init', '--scan', '--report') 'evolution initialization'
Invoke-Checked $venvPython @((Join-Path $runtimeScripts 'agent_memory_index.py'), '--init', '--scan', '--report') 'SQLite index initialization'
Invoke-Checked $venvPython @((Join-Path $runtimeScripts 'agent_memory_check.py')) 'structure check'

if ($InstallCodexHook) {
    $hookArgs = @('-RuntimeRoot', $ConfigRoot)
    if ($AutoCloseout) { $hookArgs += '-AutoCloseout' }
    & (Join-Path $runtimeScripts 'install-codex-hook.ps1') @hookArgs
}
if ($InstallAuditTask) { & (Join-Path $runtimeScripts 'audit-task.ps1') install -RuntimeRoot $ConfigRoot -Python $venvPython }
Invoke-Checked $venvPython @((Join-Path $runtimeScripts 'agent_memory_doctor.py')) 'doctor'
Write-Output '[OK] Windows installation complete'
Write-Output "Vault: $MemoryRoot"
Write-Output "Runtime: $ConfigRoot"
