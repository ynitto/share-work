#!/usr/bin/env bash
# share-work インストールスクリプト (macOS / Linux 版)
#
# 使い方:
#   bash install.sh [オプション]
#
# オプション:
#   --install-dir DIR   インストール先 (既定: $HOME/share-work)
#   --port PORT         HTTP サーバーポート番号 (既定: 8080)
#   --agent-type TYPE   エージェント種別: claude | copilot | amazon-q (既定: claude)
#   --agent-model MODEL モデル名 (Claude 専用, 既定: claude-sonnet-4-6)
#   --no-service        systemd / launchd への登録をスキップ
#   -h, --help          このヘルプを表示
#
# 例:
#   bash install.sh
#   bash install.sh --agent-type copilot
#   bash install.sh --port 9090 --install-dir /opt/share-work
#   bash install.sh --no-service

set -euo pipefail

# ---------------------------------------------------------------------------
# デフォルト値
# ---------------------------------------------------------------------------
INSTALL_DIR="$HOME/share-work"
PORT=8080
AGENT_TYPE="claude"
AGENT_MODEL="claude-sonnet-4-6"
NO_SERVICE=false

# ---------------------------------------------------------------------------
# 引数パース
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --port)        PORT="$2";        shift 2 ;;
        --agent-type)  AGENT_TYPE="$2";  shift 2 ;;
        --agent-model) AGENT_MODEL="$2"; shift 2 ;;
        --no-service)  NO_SERVICE=true;  shift   ;;
        -h|--help)
            sed -n '2,/^[^#]/p' "$0" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "不明なオプション: $1" >&2; exit 1 ;;
    esac
done

case "$AGENT_TYPE" in
    claude|copilot|amazon-q) ;;
    *) echo "[ERROR] --agent-type は claude / copilot / amazon-q のいずれかを指定してください" >&2; exit 1 ;;
esac

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
fail()  { echo -e "\n${COLOR_RED}[ERROR]${COLOR_RESET} $*" >&2; exit 1; }

# OS 判定
OS="$(uname -s)"
case "$OS" in
    Darwin) OS_TYPE="macos"  ;;
    Linux)  OS_TYPE="linux"  ;;
    *)      fail "未対応の OS: $OS" ;;
esac
ok "OS: $OS_TYPE"

BARE_REPO_DIR="${INSTALL_DIR}-tasks.git"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Step 1: 前提条件チェック
# ---------------------------------------------------------------------------
step "前提条件チェック"

# Python 3.10+
PYTHON_CMD=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [[ "$major" -ge 3 && "$minor" -ge 10 ]]; then
            PYTHON_CMD="$candidate"
            ok "Python $major.$minor 検出: $(command -v "$candidate")"
            break
        fi
    fi
done
[[ -z "$PYTHON_CMD" ]] && fail "Python 3.10 以上が見つかりません。https://www.python.org/ からインストールしてください。"

# Git
command -v git &>/dev/null || fail "Git が見つかりません。https://git-scm.com/ からインストールしてください。"
ok "Git 検出: $(git --version)"

# エージェント CLI (警告のみ)
case "$AGENT_TYPE" in
    claude)   command -v claude &>/dev/null || warn "claude コマンドが見つかりません。後でインストールしてください。" ;;
    copilot)  command -v gh     &>/dev/null || warn "gh コマンドが見つかりません (GitHub CLI)。後でインストールしてください。" ;;
    amazon-q) command -v q      &>/dev/null || warn "q コマンドが見つかりません (Amazon Q CLI)。後でインストールしてください。" ;;
esac

# ---------------------------------------------------------------------------
# Step 2: ディレクトリ構造の作成
# ---------------------------------------------------------------------------
step "インストールディレクトリ作成: $INSTALL_DIR"

for d in \
    "$INSTALL_DIR" \
    "$INSTALL_DIR/src" \
    "$INSTALL_DIR/config" \
    "$INSTALL_DIR/logs" \
    "$INSTALL_DIR/tasks" \
    "$INSTALL_DIR/workers" \
    "$INSTALL_DIR/collected_artifacts" \
    "$INSTALL_DIR/scripts"
do
    mkdir -p "$d"
done
ok "ディレクトリ作成完了"

# ---------------------------------------------------------------------------
# Step 3: ソースファイルのコピー
# ---------------------------------------------------------------------------
step "ソースファイルのコピー"

for f in "$SCRIPT_DIR"/src/*.py; do
    cp "$f" "$INSTALL_DIR/src/"
    ok "src/$(basename "$f")"
done

cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/requirements.txt"
ok "requirements.txt"

# ---------------------------------------------------------------------------
# Step 4: タスクバス Git リポジトリの初期化
# ---------------------------------------------------------------------------
step "タスクバス Git リポジトリの初期化"

if [[ ! -f "$BARE_REPO_DIR/HEAD" ]]; then
    git init --bare "$BARE_REPO_DIR" >/dev/null
    ok "bare リポジトリ作成: $BARE_REPO_DIR"
else
    ok "bare リポジトリ既存: $BARE_REPO_DIR"
fi

pushd "$INSTALL_DIR" >/dev/null
if [[ ! -d "$INSTALL_DIR/.git" ]]; then
    git init >/dev/null
    git remote add origin "$BARE_REPO_DIR"

    cat > .gitignore <<'EOF'
.venv/
__pycache__/
*.pyc
logs/
*.log
EOF

    git add . 2>/dev/null || true
    git -c user.email="setup@share-work" -c user.name="setup" \
        commit -m "chore: initial install" >/dev/null 2>&1 || true
    git push -u origin HEAD >/dev/null 2>&1 || true
    ok "Git リポジトリ初期化・初回プッシュ完了"
else
    ok "Git リポジトリ既存"
fi
popd >/dev/null

# ---------------------------------------------------------------------------
# Step 5: venv 作成と依存パッケージインストール
# ---------------------------------------------------------------------------
step "Python 仮想環境 (venv) のセットアップ"

VENV_DIR="$INSTALL_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"

if [[ ! -f "$VENV_PY" ]]; then
    "$PYTHON_CMD" -m venv "$VENV_DIR"
    ok "venv 作成: $VENV_DIR"
else
    ok "venv 既存: $VENV_DIR"
fi

step "依存パッケージのインストール"
"$VENV_PIP" install --upgrade pip --quiet
"$VENV_PIP" install -r "$INSTALL_DIR/requirements.txt" --quiet
ok "依存パッケージインストール完了"

# ---------------------------------------------------------------------------
# Step 6: サーバー設定ファイルの生成
# ---------------------------------------------------------------------------
step "設定ファイルの生成"

CONFIG_PATH="$INSTALL_DIR/config/server.yaml"
if [[ ! -f "$CONFIG_PATH" ]]; then
    WORKER_ID="worker-$(hostname -s 2>/dev/null || hostname)"
    cat > "$CONFIG_PATH" <<EOF
# share-work サーバー設定
# install.sh によって生成 ($(date +"%Y-%m-%d %H:%M"))

server:
  host: "127.0.0.1"
  port: $PORT

gitlab:
  repo_path: "$INSTALL_DIR"
  remote: "origin"
  branch: "main"

controller:
  interval: 60
  decompose_model: "$AGENT_MODEL"
  decompose_binary: "claude"
  timeouts:
    claim_ttl: 300
    execution_ttl: 3600
  cleanup:
    enabled: true
    keep_failed_tasks: true
    artifacts_dir: "$INSTALL_DIR/collected_artifacts"

worker:
  id: "$WORKER_ID"
  interval: 30
  heartbeat_interval: 60
  max_concurrent_tasks: 3
  capabilities:
    - general
    - code-generation
    - documentation
  agent:
    type: "$AGENT_TYPE"
    model: "$AGENT_MODEL"
    timeout: 3600
    sandbox: true
  resources:
    has_gpu: false
EOF
    ok "設定ファイル生成: $CONFIG_PATH"
else
    warn "設定ファイル既存 (上書きスキップ): $CONFIG_PATH"
fi

# ---------------------------------------------------------------------------
# Step 7: 管理スクリプトの配置
# ---------------------------------------------------------------------------
step "管理スクリプトの配置"

# ---- scripts/start.sh (フォアグラウンド / デバッグ用) ----
cat > "$INSTALL_DIR/scripts/start.sh" <<EOF
#!/usr/bin/env bash
# share-work サーバーを手動起動 (フォアグラウンド / デバッグ用)
cd "$INSTALL_DIR"
exec "$VENV_PY" src/server.py --config config/server.yaml "\$@"
EOF
chmod +x "$INSTALL_DIR/scripts/start.sh"
ok "scripts/start.sh"

# ---- scripts/start-daemon.sh ----
cat > "$INSTALL_DIR/scripts/start-daemon.sh" <<'SCRIPT_EOF'
#!/usr/bin/env bash
# share-work サーバーをバックグラウンド起動
SCRIPT_EOF

cat >> "$INSTALL_DIR/scripts/start-daemon.sh" <<EOF
INSTALL_DIR="$INSTALL_DIR"
VENV_PY="$VENV_PY"
LOG_DIR="$INSTALL_DIR/logs"
PID_FILE="$INSTALL_DIR/share-work.pid"
SCRIPT_EOF2

cat >> "$INSTALL_DIR/scripts/start-daemon.sh" <<'SCRIPT_EOF'

mkdir -p "$LOG_DIR"

if [[ -f "$PID_FILE" ]]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "既に起動中のプロセスがあります (PID: $PID)"
        exit 0
    fi
fi

nohup "$VENV_PY" "$INSTALL_DIR/src/server.py" \
    --config "$INSTALL_DIR/config/server.yaml" \
    >> "$LOG_DIR/server.log" 2>> "$LOG_DIR/server_error.log" &

echo $! > "$PID_FILE"
sleep 2
echo "share-work サーバーを起動しました (PID: $(cat "$PID_FILE"))。ログ: $LOG_DIR/server.log"
SCRIPT_EOF
chmod +x "$INSTALL_DIR/scripts/start-daemon.sh"
ok "scripts/start-daemon.sh"

# ---- scripts/stop.sh ----
cat > "$INSTALL_DIR/scripts/stop.sh" <<EOF
#!/usr/bin/env bash
# share-work サーバーを停止
PORT=$PORT
PID_FILE="$INSTALL_DIR/share-work.pid"

# ヘルスチェック
if curl -sf "http://127.0.0.1:\$PORT/health" -o /dev/null 2>/dev/null; then
    echo "サーバー確認済み (port \$PORT)"
else
    echo "サーバーに接続できません (停止済みの可能性があります)"
fi

# PID ファイルから停止
if [[ -f "\$PID_FILE" ]]; then
    PID=\$(cat "\$PID_FILE")
    if kill -0 "\$PID" 2>/dev/null; then
        kill "\$PID"
        rm -f "\$PID_FILE"
        echo "停止: PID \$PID"
    else
        echo "プロセス (PID \$PID) は既に終了しています"
        rm -f "\$PID_FILE"
    fi
else
    # フォールバック: プロセス名で検索
    PIDS=\$(pgrep -f "server.py.*share-work" 2>/dev/null || true)
    if [[ -n "\$PIDS" ]]; then
        echo "\$PIDS" | xargs kill
        echo "停止: PID \$PIDS"
    else
        echo "share-work プロセスが見つかりません"
    fi
fi
EOF
chmod +x "$INSTALL_DIR/scripts/stop.sh"
ok "scripts/stop.sh"

# ---- scripts/status.sh ----
cat > "$INSTALL_DIR/scripts/status.sh" <<EOF
#!/usr/bin/env bash
# share-work サーバーの状態確認
PORT=$PORT
BASE_URL="http://127.0.0.1:\$PORT"
PID_FILE="$INSTALL_DIR/share-work.pid"
LOG_FILE="$INSTALL_DIR/logs/server.log"

echo ""
echo "--- プロセス ---"
if [[ -f "\$PID_FILE" ]]; then
    PID=\$(cat "\$PID_FILE")
    if kill -0 "\$PID" 2>/dev/null; then
        echo "  実行中: PID \$PID"
    else
        echo "  プロセスなし (PID ファイルは残存: \$PID)"
    fi
else
    PIDS=\$(pgrep -f "server.py.*share-work" 2>/dev/null || true)
    if [[ -n "\$PIDS" ]]; then
        echo "  実行中: PID \$PIDS"
    else
        echo "  プロセスなし (停止中)"
    fi
fi

echo ""
echo "--- HTTP ヘルスチェック ---"
if command -v curl &>/dev/null; then
    HEALTH=\$(curl -sf "\$BASE_URL/health" 2>/dev/null || true)
    if [[ -n "\$HEALTH" ]]; then
        echo "  \$HEALTH" | python3 -c "
import sys, json
try:
    h = json.load(sys.stdin)
    print(f'  状態       : {h.get(\"status\",\"?\")}')
    print(f'  Worker     : {h.get(\"worker_id\",\"?\")}')
    print(f'  空きスロット: {h.get(\"slots_free\",\"?\")}')
except Exception:
    sys.stdin = open('/dev/stdin')
    print('  (JSON パース失敗)')
" 2>/dev/null || echo "  \$HEALTH"
    else
        echo "  接続失敗 (起動中 or 停止中)"
    fi
else
    echo "  curl が見つかりません"
fi

echo ""
echo "--- タスク一覧 ---"
if command -v curl &>/dev/null; then
    TASKS=\$(curl -sf "\$BASE_URL/tasks" 2>/dev/null || true)
    if [[ -n "\$TASKS" ]]; then
        echo "\$TASKS" | python3 -c "
import sys, json
try:
    tasks = json.load(sys.stdin)
    if not tasks:
        print('  タスクなし')
    else:
        for t in tasks:
            print(f'  [{t.get(\"status\",\"?\")}] {t.get(\"task_id\",\"?\")}  優先度:{t.get(\"priority\",\"?\")}')
except Exception:
    print('  (取得失敗)')
" 2>/dev/null
    else
        echo "  取得失敗"
    fi
fi

echo ""
echo "--- ログ (末尾 20 行) ---"
if [[ -f "\$LOG_FILE" ]]; then
    tail -20 "\$LOG_FILE"
else
    echo "  ログファイルなし"
fi
EOF
chmod +x "$INSTALL_DIR/scripts/status.sh"
ok "scripts/status.sh"

# ---- scripts/submit-task.sh ----
# Write the script in two parts: a single heredoc using \$ for unexpanded vars,
# and $PORT (without backslash) for the installer-time expansion.
cat > "$INSTALL_DIR/scripts/submit-task.sh" <<EOF
#!/usr/bin/env bash
# タスクを投入するサンプルスクリプト
# 使い方:
#   submit-task.sh [オプション] "要件テキスト"
#
# オプション:
#   --by NAME          投稿者名 (既定: \$USER)
#   --repo PATH        作業リポジトリパス (省略可)
#   --local            ローカルモード: このサーバー上で即座に実行
#   -h, --help         このヘルプを表示
#
# 例:
#   submit-task.sh "README を更新して"
#   submit-task.sh --local "バグを直して"
#   submit-task.sh --local --repo /path/to/repo "機能を追加して"
PORT=$PORT

BY="\$USER"
REPO_PATH=""
MODE=""
REQUIREMENT=""

while [[ \$# -gt 0 ]]; do
    case "\$1" in
        --by)   BY="\$2";        shift 2 ;;
        --repo) REPO_PATH="\$2"; shift 2 ;;
        --local) MODE="local";  shift   ;;
        -h|--help) sed -n '/^# /p' "\$0" | sed 's/^# //'; exit 0 ;;
        *) REQUIREMENT="\$1"; shift ;;
    esac
done

if [[ -z "\$REQUIREMENT" ]]; then
    echo "使い方: \$0 [--local] [--by NAME] [--repo PATH] '要件テキスト'" >&2
    exit 1
fi

BODY=\$(python3 -c "
import json, sys
d = {'requirement': sys.argv[1], 'by': sys.argv[2]}
if sys.argv[3]: d['repo_path'] = sys.argv[3]
if sys.argv[4]: d['mode'] = sys.argv[4]
print(json.dumps(d))
" "\$REQUIREMENT" "\$BY" "\$REPO_PATH" "\$MODE")

curl -X POST "http://127.0.0.1:\$PORT/tasks" \\
    -H "Content-Type: application/json" \\
    -d "\$BODY"
echo
EOF
chmod +x "$INSTALL_DIR/scripts/submit-task.sh"
ok "scripts/submit-task.sh"

# ---------------------------------------------------------------------------
# Step 8: systemd (Linux) / launchd (macOS) へのデーモン登録
# ---------------------------------------------------------------------------
if [[ "$NO_SERVICE" == "true" ]]; then
    warn "サービス登録をスキップします (--no-service が指定されました)"
else
    if [[ "$OS_TYPE" == "linux" ]]; then
        # ----------------------------------------------------------------
        # Linux: systemd ユーザーサービス
        # ----------------------------------------------------------------
        step "systemd ユーザーサービスへの登録"

        SYSTEMD_DIR="$HOME/.config/systemd/user"
        mkdir -p "$SYSTEMD_DIR"

        UNIT_FILE="$SYSTEMD_DIR/share-work.service"
        cat > "$UNIT_FILE" <<EOF
[Unit]
Description=share-work 分散 AI タスクサーバー
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$VENV_PY $INSTALL_DIR/src/server.py --config $INSTALL_DIR/config/server.yaml
Restart=on-failure
RestartSec=5
StandardOutput=append:$INSTALL_DIR/logs/server.log
StandardError=append:$INSTALL_DIR/logs/server_error.log

[Install]
WantedBy=default.target
EOF

        if systemctl --user daemon-reload 2>/dev/null; then
            systemctl --user enable share-work.service 2>/dev/null
            ok "systemd ユーザーサービス登録完了: share-work.service"
            ok "ユニットファイル: $UNIT_FILE"

            read -r -p $'\n今すぐサーバーを起動しますか? [Y/n] ' ans
            ans="${ans:-Y}"
            if [[ "$ans" =~ ^[Yy] ]]; then
                systemctl --user start share-work.service
                sleep 2
                if curl -sf "http://127.0.0.1:$PORT/health" -o /dev/null 2>/dev/null; then
                    ok "サーバー起動確認 (port $PORT)"
                else
                    warn "ヘルスチェック失敗。ログを確認してください: $INSTALL_DIR/logs/server.log"
                fi
            fi
        else
            warn "systemd --user が利用できません。手動起動してください: $INSTALL_DIR/scripts/start-daemon.sh"
        fi

    else
        # ----------------------------------------------------------------
        # macOS: launchd ユーザーエージェント
        # ----------------------------------------------------------------
        step "launchd ユーザーエージェントへの登録"

        LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
        mkdir -p "$LAUNCH_AGENTS_DIR"

        PLIST_FILE="$LAUNCH_AGENTS_DIR/com.share-work.server.plist"
        cat > "$PLIST_FILE" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.share-work.server</string>

    <key>ProgramArguments</key>
    <array>
        <string>$VENV_PY</string>
        <string>$INSTALL_DIR/src/server.py</string>
        <string>--config</string>
        <string>$INSTALL_DIR/config/server.yaml</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$INSTALL_DIR</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>

    <key>StandardOutPath</key>
    <string>$INSTALL_DIR/logs/server.log</string>

    <key>StandardErrorPath</key>
    <string>$INSTALL_DIR/logs/server_error.log</string>

    <key>ThrottleInterval</key>
    <integer>5</integer>
</dict>
</plist>
EOF

        # 既存エージェントをアンロード
        launchctl unload "$PLIST_FILE" 2>/dev/null || true
        launchctl load -w "$PLIST_FILE"
        ok "launchd ユーザーエージェント登録完了: com.share-work.server"
        ok "plist ファイル: $PLIST_FILE"

        sleep 2
        if curl -sf "http://127.0.0.1:$PORT/health" -o /dev/null 2>/dev/null; then
            ok "サーバー起動確認 (port $PORT)"
        else
            warn "ヘルスチェック失敗。ログを確認してください: $INSTALL_DIR/logs/server.log"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 完了メッセージ
# ---------------------------------------------------------------------------
echo ""
echo -e "${COLOR_GREEN}============================================================${COLOR_RESET}"
echo -e "${COLOR_GREEN}  share-work インストール完了!${COLOR_RESET}"
echo -e "${COLOR_GREEN}============================================================${COLOR_RESET}"
echo ""
echo "  インストール先  : $INSTALL_DIR"
echo "  タスクバス      : $BARE_REPO_DIR"
echo "  ポート          : $PORT"
echo "  エージェント    : $AGENT_TYPE"
echo ""
echo "  管理コマンド:"
echo "    起動 (手動)    : $INSTALL_DIR/scripts/start.sh"
echo "    起動 (BG)      : $INSTALL_DIR/scripts/start-daemon.sh"
echo "    停止           : $INSTALL_DIR/scripts/stop.sh"
echo "    状態確認       : $INSTALL_DIR/scripts/status.sh"
echo "    タスク投入     : $INSTALL_DIR/scripts/submit-task.sh '要件テキスト'"
echo "    ローカル実行   : $INSTALL_DIR/scripts/submit-task.sh --local '要件テキスト'"
echo ""

if [[ "$NO_SERVICE" == "false" ]]; then
    if [[ "$OS_TYPE" == "linux" ]]; then
        echo "  サービス管理 (systemd):"
        echo "    systemctl --user start   share-work"
        echo "    systemctl --user stop    share-work"
        echo "    systemctl --user status  share-work"
        echo "    systemctl --user restart share-work"
    else
        echo "  サービス管理 (launchd):"
        echo "    launchctl start  com.share-work.server"
        echo "    launchctl stop   com.share-work.server"
    fi
    echo ""
fi

echo "  API エンドポイント: http://127.0.0.1:$PORT"
echo ""
