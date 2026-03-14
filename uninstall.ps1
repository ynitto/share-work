#Requires -Version 5.1
<#
.SYNOPSIS
    share-work のアンインストールスクリプト (Windows 版)

.DESCRIPTION
    install.ps1 でインストールしたものをすべて削除します:
      1. 実行中プロセスの停止
      2. タスクスケジューラからの登録解除
      3. インストールディレクトリの削除
      4. タスクバス bare Git リポジトリの削除

.PARAMETER InstallDir
    アンインストール対象のディレクトリ (既定: %USERPROFILE%\share-work)

.PARAMETER KeepTasks
    タスクバス Git リポジトリを残す

.PARAMETER KeepLogs
    ログをタイムスタンプ付きフォルダに退避して残す

.PARAMETER Yes
    確認プロンプトをスキップする

.EXAMPLE
    .\uninstall.ps1
    .\uninstall.ps1 -KeepTasks
    .\uninstall.ps1 -InstallDir D:\tools\share-work -Yes
#>
[CmdletBinding()]
param(
    [string] $InstallDir = "$env:USERPROFILE\share-work",
    [switch] $KeepTasks,
    [switch] $KeepLogs,
    [switch] $Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$bareRepoDir = "${InstallDir}-tasks.git"
$taskName    = "share-work-server"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
function Write-Step { param($msg) Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-OK   { param($msg) Write-Host "    [OK] $msg" -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host "    [WARN] $msg" -ForegroundColor Yellow }
function Write-Info { param($msg) Write-Host "    $msg" }

# ---------------------------------------------------------------------------
# 対象確認
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "============================================================" -ForegroundColor Red
Write-Host "  share-work アンインストール" -ForegroundColor Red
Write-Host "============================================================" -ForegroundColor Red
Write-Host ""
Write-Host "  削除対象:"
Write-Host "    インストールディレクトリ : $InstallDir"
if (-not $KeepTasks) {
    Write-Host "    タスクバスリポジトリ     : $bareRepoDir"
} else {
    Write-Host "    タスクバスリポジトリ     : $bareRepoDir  (-KeepTasks により保持)"
}
Write-Host "    タスクスケジューラ       : $taskName"
Write-Host ""

if (-not $Yes) {
    $ans = Read-Host "アンインストールを続行しますか? [y/N]"
    if ($ans -notmatch "^[Yy]") {
        Write-Host "キャンセルしました。"
        exit 0
    }
}

# ---------------------------------------------------------------------------
# Step 1: プロセス停止
# ---------------------------------------------------------------------------
Write-Step "実行中のプロセスを停止"

try {
    $procs = Get-WmiObject Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -like "*launch.pyw*" }
    if ($procs) {
        $procs | ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Write-OK "プロセス停止: PID $($_.ProcessId)"
        }
    } else {
        Write-Info "実行中のプロセスはありません"
    }
} catch {
    Write-Warn "プロセス停止中にエラー: $_"
}

# ---------------------------------------------------------------------------
# Step 2: タスクスケジューラから削除
# ---------------------------------------------------------------------------
Write-Step "タスクスケジューラから削除"

$existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existingTask) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-OK "タスクスケジューラ削除: $taskName"
} else {
    Write-Info "タスクスケジューラに登録なし (スキップ)"
}

# ---------------------------------------------------------------------------
# Step 3: ログの処理
# ---------------------------------------------------------------------------
if ($KeepLogs -and (Test-Path "$InstallDir\logs")) {
    Write-Step "ログを保存"
    $timestamp  = Get-Date -Format "yyyyMMdd-HHmmss"
    $logsBackup = "${InstallDir}-logs-${timestamp}"
    Move-Item "$InstallDir\logs" $logsBackup
    Write-OK "ログ退避先: $logsBackup"
}

# ---------------------------------------------------------------------------
# Step 4: インストールディレクトリの削除
# ---------------------------------------------------------------------------
Write-Step "インストールディレクトリを削除"

if (Test-Path $InstallDir) {
    Remove-Item $InstallDir -Recurse -Force
    Write-OK "削除完了: $InstallDir"
} else {
    Write-Warn "ディレクトリが見つかりません: $InstallDir"
}

# ---------------------------------------------------------------------------
# Step 5: タスクバスリポジトリの削除
# ---------------------------------------------------------------------------
if (-not $KeepTasks) {
    Write-Step "タスクバスリポジトリを削除"

    if (Test-Path $bareRepoDir) {
        Remove-Item $bareRepoDir -Recurse -Force
        Write-OK "削除完了: $bareRepoDir"
    } else {
        Write-Warn "リポジトリが見つかりません: $bareRepoDir"
    }
} else {
    Write-Info "タスクバスリポジトリを保持: $bareRepoDir"
}

# ---------------------------------------------------------------------------
# 完了メッセージ
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "============================================================" -ForegroundColor Green
Write-Host "  share-work アンインストール完了" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host ""
