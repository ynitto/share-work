#Requires -Version 5.1
<#
.SYNOPSIS
    share-work のインストールスクリプト (Windows 版)

.DESCRIPTION
    以下を行います:
      1. 前提条件チェック (Python 3.10+, Git)
      2. インストールディレクトリ (%USERPROFILE%\share-work) の作成
      3. ソースファイルのコピー
      4. タスクバス用ローカル bare Git リポジトリの初期化
      5. venv の作成と依存パッケージのインストール
      6. サーバー設定ファイルの生成
      7. ランチャー / 管理スクリプトの配置
      8. Windows タスクスケジューラへのデーモン登録

.PARAMETER InstallDir
    インストール先ディレクトリ (既定: %USERPROFILE%\share-work)

.PARAMETER Port
    HTTP サーバーのポート番号 (既定: 8080)

.PARAMETER AgentType
    使用する AI エージェント CLI: claude | copilot | amazon-q (既定: claude)

.PARAMETER AgentModel
    エージェントモデル名 (Claude 専用, 既定: claude-sonnet-4-6)

.PARAMETER NoService
    タスクスケジューラへの登録をスキップする

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Port 9090 -AgentType copilot
    .\install.ps1 -InstallDir D:\tools\share-work -NoService
#>
[CmdletBinding()]
param(
    [string] $InstallDir  = "$env:USERPROFILE\share-work",
    [int]    $Port        = 8080,
    [ValidateSet("claude","copilot","amazon-q")]
    [string] $AgentType   = "claude",
    [string] $AgentModel  = "claude-sonnet-4-6",
    [switch] $NoService
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Write-Step  { param($msg) Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-OK    { param($msg) Write-Host "    [OK] $msg" -ForegroundColor Green }
function Write-Warn  { param($msg) Write-Host "    [WARN] $msg" -ForegroundColor Yellow }
function Write-Fail  { param($msg) Write-Host "`n[ERROR] $msg" -ForegroundColor Red; exit 1 }

function Find-Command {
    param([string]$Name)
    return (Get-Command $Name -ErrorAction SilentlyContinue)
}

# ---------------------------------------------------------------------------
# Step 1: 前提条件チェック
# ---------------------------------------------------------------------------
Write-Step "前提条件チェック"

# Python
$pythonCmd = $null
foreach ($candidate in @("python", "python3", "py")) {
    $cmd = Find-Command $candidate
    if ($cmd) {
        $ver = & $cmd --version 2>&1
        if ($ver -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -ge 3 -and $minor -ge 10) {
                $pythonCmd = $cmd.Source
                Write-OK "Python $major.$minor 検出: $pythonCmd"
                break
            }
        }
    }
}
if (-not $pythonCmd) {
    Write-Fail "Python 3.10 以上が見つかりません。https://www.python.org/ からインストールしてください。"
}

# Git
if (-not (Find-Command "git")) {
    Write-Fail "Git が見つかりません。https://git-scm.com/ からインストールしてください。"
}
Write-OK "Git 検出: $(git --version)"

# エージェント CLI の存在確認 (警告のみ)
switch ($AgentType) {
    "claude"    { if (-not (Find-Command "claude")) { Write-Warn "claude コマンドが見つかりません。後でインストールしてください。" } }
    "copilot"   { if (-not (Find-Command "gh"))     { Write-Warn "gh コマンドが見つかりません (GitHub CLI)。後でインストールしてください。" } }
    "amazon-q"  { if (-not (Find-Command "q"))      { Write-Warn "q コマンドが見つかりません (Amazon Q CLI)。後でインストールしてください。" } }
}

# ---------------------------------------------------------------------------
# Step 2: ディレクトリ構造の作成
# ---------------------------------------------------------------------------
Write-Step "インストールディレクトリ作成: $InstallDir"

$bareRepoDir = "${InstallDir}-tasks.git"
$dirs = @(
    $InstallDir,
    "$InstallDir\src",
    "$InstallDir\config",
    "$InstallDir\logs",
    "$InstallDir\tasks",
    "$InstallDir\workers",
    "$InstallDir\collected_artifacts",
    "$InstallDir\scripts"
)
foreach ($d in $dirs) {
    New-Item -ItemType Directory -Path $d -Force | Out-Null
}
Write-OK "ディレクトリ作成完了"

# ---------------------------------------------------------------------------
# Step 3: ソースファイルのコピー
# ---------------------------------------------------------------------------
Write-Step "ソースファイルのコピー"

$srcRoot = $PSScriptRoot

# src/*.py
Get-ChildItem "$srcRoot\src\*.py" | ForEach-Object {
    Copy-Item $_.FullName "$InstallDir\src\" -Force
    Write-OK "src\$($_.Name)"
}

# requirements.txt
Copy-Item "$srcRoot\requirements.txt" "$InstallDir\requirements.txt" -Force
Write-OK "requirements.txt"

# ---------------------------------------------------------------------------
# Step 4: タスクバス Git リポジトリの初期化
# ---------------------------------------------------------------------------
Write-Step "タスクバス Git リポジトリの初期化"

# bare リポジトリ（リモート代替）
if (-not (Test-Path "$bareRepoDir\HEAD")) {
    git init --bare "$bareRepoDir" | Out-Null
    Write-OK "bare リポジトリ作成: $bareRepoDir"
} else {
    Write-OK "bare リポジトリ既存: $bareRepoDir"
}

# ワーキングツリー
Push-Location $InstallDir
try {
    if (-not (Test-Path "$InstallDir\.git")) {
        git init | Out-Null
        git remote add origin "$bareRepoDir" | Out-Null

        # .gitignore
        @"
.venv/
__pycache__/
*.pyc
logs/
*.log
"@ | Set-Content ".gitignore" -Encoding UTF8

        git add . 2>&1 | Out-Null
        git -c user.email="setup@share-work" -c user.name="setup" `
            commit -m "chore: initial install" 2>&1 | Out-Null
        git push -u origin HEAD 2>&1 | Out-Null
        Write-OK "Git リポジトリ初期化・初回プッシュ完了"
    } else {
        Write-OK "Git リポジトリ既存"
    }
} finally {
    Pop-Location
}

# ---------------------------------------------------------------------------
# Step 5: venv 作成と依存パッケージインストール
# ---------------------------------------------------------------------------
Write-Step "Python 仮想環境 (venv) のセットアップ"

$venvDir  = "$InstallDir\.venv"
$venvPy   = "$venvDir\Scripts\python.exe"
$venvPyw  = "$venvDir\Scripts\pythonw.exe"
$venvPip  = "$venvDir\Scripts\pip.exe"

if (-not (Test-Path $venvPy)) {
    & $pythonCmd -m venv "$venvDir" | Out-Null
    Write-OK "venv 作成: $venvDir"
} else {
    Write-OK "venv 既存: $venvDir"
}

Write-Step "依存パッケージのインストール"
& $venvPip install --upgrade pip --quiet
& $venvPip install -r "$InstallDir\requirements.txt" --quiet
Write-OK "依存パッケージインストール完了"

# ---------------------------------------------------------------------------
# Step 6: サーバー設定ファイルの生成
# ---------------------------------------------------------------------------
Write-Step "設定ファイルの生成"

$configPath = "$InstallDir\config\server.yaml"
if (-not (Test-Path $configPath)) {
    $escapedDir = $InstallDir.Replace("\", "/")
    $bareEscaped = $bareRepoDir.Replace("\", "/")
    @"
# share-work サーバー設定
# install.ps1 によって生成 ($(Get-Date -Format "yyyy-MM-dd HH:mm"))

server:
  host: "127.0.0.1"
  port: $Port

gitlab:
  repo_path: "$escapedDir"
  remote: "origin"
  branch: "main"

controller:
  interval: 60
  decompose_model: "$AgentModel"
  decompose_binary: "claude"
  timeouts:
    claim_ttl: 300
    execution_ttl: 3600
  cleanup:
    enabled: true
    keep_failed_tasks: true
    artifacts_dir: "$escapedDir/collected_artifacts"

worker:
  id: "worker-$env:COMPUTERNAME"
  interval: 30
  heartbeat_interval: 60
  max_concurrent_tasks: 3
  capabilities:
    - general
    - code-generation
    - documentation
  agent:
    type: "$AgentType"
    model: "$AgentModel"
    timeout: 3600
    sandbox: true
  resources:
    has_gpu: false
"@ | Set-Content $configPath -Encoding UTF8
    Write-OK "設定ファイル生成: $configPath"
} else {
    Write-Warn "設定ファイル既存 (上書きスキップ): $configPath"
}

# ---------------------------------------------------------------------------
# Step 7: ランチャー / 管理スクリプトの配置
# ---------------------------------------------------------------------------
Write-Step "ランチャー / 管理スクリプトの配置"

# ---- launch.pyw (pythonw で実行 - コンソールなしデーモン) ----
$escapedInstallDir = $InstallDir.Replace("\", "\\")
@"
"""share-work daemon launcher (pythonw 用 - コンソールウィンドウなし)."""
import sys
import os
from pathlib import Path

install_dir = Path(r"$escapedInstallDir")
log_dir = install_dir / "logs"
log_dir.mkdir(exist_ok=True)

# stdio をログファイルへリダイレクト
sys.stdout = open(log_dir / "server.log", "a", encoding="utf-8", buffering=1)
sys.stderr = open(log_dir / "server_error.log", "a", encoding="utf-8", buffering=1)

import datetime
print(f"\n{'='*60}", flush=True)
print(f"  share-work 起動: {datetime.datetime.now()}", flush=True)
print(f"{'='*60}", flush=True)

sys.path.insert(0, str(install_dir / "src"))
os.chdir(str(install_dir))

from server import main
main()
"@ | Set-Content "$InstallDir\launch.pyw" -Encoding UTF8
Write-OK "launch.pyw"

# ---- scripts\start.ps1 ----
@"
# share-work サーバーを手動起動 (フォアグラウンド / デバッグ用)
Set-Location "$InstallDir"
& "$venvPy" src\server.py --config config\server.yaml
"@ | Set-Content "$InstallDir\scripts\start.ps1" -Encoding UTF8
Write-OK "scripts\start.ps1"

# ---- scripts\start-daemon.ps1 ----
@"
# share-work サーバーをバックグラウンド起動
`$pywPath = "$venvPyw"
`$launcher = "$InstallDir\launch.pyw"
`$existing = Get-Process pythonw -ErrorAction SilentlyContinue |
    Where-Object { `$_.MainWindowTitle -eq "" }
if (`$existing) {
    Write-Host "既に起動中のプロセスがあります (PID: `$(`$existing.Id -join ', '))"
} else {
    Start-Process `$pywPath -ArgumentList "`"`$launcher`"" -WindowStyle Hidden
    Start-Sleep -Seconds 2
    Write-Host "share-work サーバーを起動しました。ログ: $InstallDir\logs\server.log"
}
"@ | Set-Content "$InstallDir\scripts\start-daemon.ps1" -Encoding UTF8
Write-OK "scripts\start-daemon.ps1"

# ---- scripts\stop.ps1 ----
@"
# share-work サーバーを停止
`$port = $Port
try {
    `$resp = Invoke-RestMethod -Uri "http://127.0.0.1:`$port/health" -TimeoutSec 3
    Write-Host "サーバー確認 (worker: `$(`$resp.worker_id))"
} catch {
    Write-Host "サーバーに接続できません (停止済みの可能性があります)"
}

# launch.pyw を実行している pythonw プロセスを停止
`$procs = Get-WmiObject Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { `$_.CommandLine -like "*launch.pyw*" }
if (`$procs) {
    `$procs | ForEach-Object {
        Stop-Process -Id `$_.ProcessId -Force
        Write-Host "停止: PID `$(`$_.ProcessId)"
    }
} else {
    Write-Host "share-work プロセスが見つかりません"
}
"@ | Set-Content "$InstallDir\scripts\stop.ps1" -Encoding UTF8
Write-OK "scripts\stop.ps1"

# ---- scripts\status.ps1 ----
@"
# share-work サーバーの状態確認
`$port = $Port
`$baseUrl = "http://127.0.0.1:`$port"

Write-Host "`n--- プロセス ---"
`$procs = Get-WmiObject Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { `$_.CommandLine -like "*launch.pyw*" }
if (`$procs) {
    `$procs | ForEach-Object { Write-Host "  実行中: PID `$(`$_.ProcessId)" }
} else {
    Write-Host "  プロセスなし (停止中)"
}

Write-Host "`n--- HTTP ヘルスチェック ---"
try {
    `$h = Invoke-RestMethod -Uri "`$baseUrl/health" -TimeoutSec 5
    Write-Host "  状態   : `$(`$h.status)"
    Write-Host "  Worker : `$(`$h.worker_id)"
    Write-Host "  空きスロット: `$(`$h.slots_free)"
} catch {
    Write-Host "  接続失敗 (起動中 or 停止中)"
}

Write-Host "`n--- タスク一覧 ---"
try {
    `$tasks = Invoke-RestMethod -Uri "`$baseUrl/tasks" -TimeoutSec 5
    if (`$tasks.Count -eq 0) {
        Write-Host "  タスクなし"
    } else {
        `$tasks | ForEach-Object {
            Write-Host "  [`$(`$_.status)] `$(`$_.task_id)  優先度:`$(`$_.priority)"
        }
    }
} catch {
    Write-Host "  取得失敗"
}

Write-Host "`n--- ログ (末尾 20 行) ---"
`$logFile = "$InstallDir\logs\server.log"
if (Test-Path `$logFile) {
    Get-Content `$logFile -Tail 20
} else {
    Write-Host "  ログファイルなし"
}
"@ | Set-Content "$InstallDir\scripts\status.ps1" -Encoding UTF8
Write-OK "scripts\status.ps1"

# ---- scripts\submit-task.ps1 ----
@"
# タスクを投入するサンプルスクリプト
# 使い方:
#   submit-task.ps1 -Requirement "要件テキスト"
#   submit-task.ps1 -Requirement "要件テキスト" -By "alice"
#   submit-task.ps1 -Requirement "要件テキスト" -By "alice" -RepoPath "C:\path\to\repo"
param(
    [Parameter(Mandatory)][string]`$Requirement,
    [string]`$By = "`$env:USERNAME",
    [string]`$RepoPath = ""
)
`$hash = @{ requirement = `$Requirement; by = `$By }
if (`$RepoPath -ne "") { `$hash["repo_path"] = `$RepoPath }
`$body = `$hash | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:$Port/tasks" ``
    -Method POST -Body `$body -ContentType "application/json"
"@ | Set-Content "$InstallDir\scripts\submit-task.ps1" -Encoding UTF8
Write-OK "scripts\submit-task.ps1"

# ---------------------------------------------------------------------------
# Step 8: タスクスケジューラへのデーモン登録
# ---------------------------------------------------------------------------
$taskName = "share-work-server"

if ($NoService) {
    Write-Warn "タスクスケジューラ登録をスキップします (-NoService が指定されました)"
} else {
    Write-Step "Windows タスクスケジューラへの登録"

    # 既存タスクの削除
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

    $action = New-ScheduledTaskAction `
        -Execute "$venvPyw" `
        -Argument "`"$InstallDir\launch.pyw`"" `
        -WorkingDirectory $InstallDir

    # ログオン時に起動
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

    $settings = New-ScheduledTaskSettingsSet `
        -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew

    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -LogonType Interactive `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "share-work 分散 AI タスクサーバー" | Out-Null

    Write-OK "タスクスケジューラ登録完了: '$taskName'"
    Write-OK "次回ログオン時に自動起動します"

    # 今すぐ起動するか確認
    $ans = Read-Host "`n今すぐサーバーを起動しますか? [Y/n]"
    if ($ans -eq "" -or $ans -match "^[Yy]") {
        Start-Process $venvPyw -ArgumentList "`"$InstallDir\launch.pyw`"" -WindowStyle Hidden
        Start-Sleep -Seconds 3
        try {
            $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
            Write-OK "サーバー起動確認: $($h.status) (worker: $($h.worker_id))"
        } catch {
            Write-Warn "ヘルスチェック失敗。ログを確認してください: $InstallDir\logs\server.log"
        }
    }
}

# ---------------------------------------------------------------------------
# 完了メッセージ
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  share-work インストール完了!" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  インストール先  : $InstallDir"
Write-Host "  タスクバス      : $bareRepoDir"
Write-Host "  ポート          : $Port"
Write-Host "  エージェント    : $AgentType"
Write-Host ""
Write-Host "  管理コマンド:"
Write-Host "    起動 (手動)    : powershell $InstallDir\scripts\start.ps1"
Write-Host "    起動 (BG)      : powershell $InstallDir\scripts\start-daemon.ps1"
Write-Host "    停止           : powershell $InstallDir\scripts\stop.ps1"
Write-Host "    状態確認       : powershell $InstallDir\scripts\status.ps1"
Write-Host "    タスク投入     : powershell $InstallDir\scripts\submit-task.ps1 -Requirement '...' "
Write-Host ""
Write-Host "  API エンドポイント: http://127.0.0.1:$Port"
Write-Host ""
