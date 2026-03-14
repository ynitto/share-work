#!/usr/bin/env bash
# share-work アンインストールスクリプト (macOS / Linux 版)
#
# 使い方:
#   bash uninstall.sh [オプション]
#
# オプション:
#   --install-dir DIR   アンインストール対象ディレクトリ (既定: $HOME/share-work)
#   --keep-tasks        タスクバス Git リポジトリを残す
#   --keep-logs         ログファイルを残す
#   -y, --yes           確認プロンプトをスキップ
#   -h, --help          このヘルプを表示
#
# 例:
#   bash uninstall.sh
#   bash uninstall.sh --keep-tasks
#   bash uninstall.sh --install-dir /opt/share-work -y

set -euo pipefail

# ---------------------------------------------------------------------------
# デフォルト値
# ---------------------------------------------------------------------------
INSTALL_DIR="$HOME/share-work"
KEEP_TASKS=false
KEEP_LOGS=false
YES=false

# ---------------------------------------------------------------------------
# 引数パース
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --keep-tasks)  KEEP_TASKS=true;  shift   ;;
        --keep-logs)   KEEP_LOGS=true;   shift   ;;
        -y|--yes)      YES=true;         shift   ;;
        -h|--help)
            sed -n '2,/^[^#]/p' "$0" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "不明なオプション: $1" >&2; exit 1 ;;
    esac
done

BARE_REPO_DIR="${INSTALL_DIR}-tasks.git"

# ---------------------------------------------------------------------------
# ヘルパー関数
# ---------------------------------------------------------------------------
COLOR_CYAN='\033[0;36m'
COLOR_GREEN='\033[0;32m'
COLOR_YELLOW='\033[1;33m'
COLOR_RED='\033[0;31m'
COLOR_RESET='\033[0m'

step()  { echo -e "\n${COLOR_CYAN}==> $*${COLOR_RESET}"; }
ok()    { echo -e "    ${COLOR_GREEN}[OK]${COLOR_RESET} $*"; }
warn()  { echo -e "    ${COLOR_YELLOW}[WARN]${COLOR_RESET} $*"; }
info()  { echo -e "    $*"; }

# OS 判定
OS="$(uname -s)"
case "$OS" in
    Darwin) OS_TYPE="macos" ;;
    Linux)  OS_TYPE="linux" ;;
    *)      echo "未対応の OS: $OS" >&2; exit 1 ;;
esac

# ---------------------------------------------------------------------------
# 対象確認
# ---------------------------------------------------------------------------
echo ""
echo -e "${COLOR_RED}============================================================${COLOR_RESET}"
echo -e "${COLOR_RED}  share-work アンインストール${COLOR_RESET}"
echo -e "${COLOR_RED}============================================================${COLOR_RESET}"
echo ""
echo "  削除対象:"
echo "    インストールディレクトリ : $INSTALL_DIR"
if [[ "$KEEP_TASKS" == "false" ]]; then
    echo "    タスクバスリポジトリ     : $BARE_REPO_DIR"
else
    echo "    タスクバスリポジトリ     : $BARE_REPO_DIR  (--keep-tasks により保持)"
fi
if [[ "$OS_TYPE" == "linux" ]]; then
    echo "    systemd サービス         : share-work.service"
else
    echo "    launchd エージェント     : com.share-work.server"
fi
echo ""

if [[ "$YES" == "false" ]]; then
    read -r -p "アンインストールを続行しますか? [y/N] " ans
    ans="${ans:-N}"
    if [[ ! "$ans" =~ ^[Yy] ]]; then
        echo "キャンセルしました。"
        exit 0
    fi
fi

# ---------------------------------------------------------------------------
# Step 1: プロセス停止
# ---------------------------------------------------------------------------
step "実行中のプロセスを停止"

PID_FILE="$INSTALL_DIR/share-work.pid"
if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        ok "プロセス停止: PID $PID"
    else
        info "プロセス (PID $PID) は既に停止しています"
    fi
    rm -f "$PID_FILE"
else
    # フォールバック: プロセス名で検索
    PIDS=$(pgrep -f "server.py.*share-work" 2>/dev/null || true)
    if [[ -n "$PIDS" ]]; then
        echo "$PIDS" | xargs kill 2>/dev/null || true
        ok "プロセス停止: PID $PIDS"
    else
        info "実行中のプロセスはありません"
    fi
fi

# ---------------------------------------------------------------------------
# Step 2: サービス登録解除
# ---------------------------------------------------------------------------
if [[ "$OS_TYPE" == "linux" ]]; then
    step "systemd ユーザーサービスの削除"

    UNIT_FILE="$HOME/.config/systemd/user/share-work.service"
    if [[ -f "$UNIT_FILE" ]]; then
        if systemctl --user is-active share-work.service &>/dev/null; then
            systemctl --user stop share-work.service 2>/dev/null || true
        fi
        systemctl --user disable share-work.service 2>/dev/null || true
        rm -f "$UNIT_FILE"
        systemctl --user daemon-reload 2>/dev/null || true
        ok "systemd サービス削除: $UNIT_FILE"
    else
        info "systemd サービスファイルなし (スキップ)"
    fi
else
    step "launchd ユーザーエージェントの削除"

    PLIST_FILE="$HOME/Library/LaunchAgents/com.share-work.server.plist"
    if [[ -f "$PLIST_FILE" ]]; then
        launchctl unload "$PLIST_FILE" 2>/dev/null || true
        rm -f "$PLIST_FILE"
        ok "launchd エージェント削除: $PLIST_FILE"
    else
        info "launchd plist なし (スキップ)"
    fi
fi

# ---------------------------------------------------------------------------
# Step 3: ログの処理
# ---------------------------------------------------------------------------
if [[ "$KEEP_LOGS" == "true" && -d "$INSTALL_DIR/logs" ]]; then
    LOGS_BACKUP="${INSTALL_DIR}-logs-$(date +%Y%m%d-%H%M%S)"
    step "ログを保存"
    mv "$INSTALL_DIR/logs" "$LOGS_BACKUP"
    ok "ログ退避先: $LOGS_BACKUP"
fi

# ---------------------------------------------------------------------------
# Step 4: インストールディレクトリの削除
# ---------------------------------------------------------------------------
step "インストールディレクトリを削除"

if [[ -d "$INSTALL_DIR" ]]; then
    rm -rf "$INSTALL_DIR"
    ok "削除完了: $INSTALL_DIR"
else
    warn "ディレクトリが見つかりません: $INSTALL_DIR"
fi

# ---------------------------------------------------------------------------
# Step 5: タスクバスリポジトリの削除
# ---------------------------------------------------------------------------
if [[ "$KEEP_TASKS" == "false" ]]; then
    step "タスクバスリポジトリを削除"

    if [[ -d "$BARE_REPO_DIR" ]]; then
        rm -rf "$BARE_REPO_DIR"
        ok "削除完了: $BARE_REPO_DIR"
    else
        warn "リポジトリが見つかりません: $BARE_REPO_DIR"
    fi
else
    info "タスクバスリポジトリを保持: $BARE_REPO_DIR"
fi

# ---------------------------------------------------------------------------
# 完了メッセージ
# ---------------------------------------------------------------------------
echo ""
echo -e "${COLOR_GREEN}============================================================${COLOR_RESET}"
echo -e "${COLOR_GREEN}  share-work アンインストール完了${COLOR_RESET}"
echo -e "${COLOR_GREEN}============================================================${COLOR_RESET}"
echo ""
