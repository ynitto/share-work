# share-work

Git をタスクバスとして使う、分散 AI エージェントタスク管理システムです。

HTTP API に自然文で要件を投げると、バックグラウンドで動く AI エージェント (Claude Code / GitHub Copilot / Amazon Q) が自律的にタスクを引き受けて成果物を生成します。

```
発注者
  │  POST /tasks  {"requirement": "Pythonで素数判定関数を実装して"}
  ▼
┌────────────────────────────────────────┐
│  TaskServer                            │
│  ┌──────────┐  ┌────────────────────┐  │
│  │ HTTP API │  │ Controller         │  │
│  │ :8080    │  │ (タスク分解/監視)    │  │
│  └──────────┘  └────────────────────┘  │
│               ┌────────────────────┐   │
│               │ Worker             │   │
│               │ (タスク取得/実行)    │   │
│               └─────────┬──────────┘   │
└─────────────────────────┼──────────────┘
                          │ subprocess
                    ┌─────▼───────────────┐
                    │  AI エージェント CLI  │
                    │  (claude/gh/q/kiro) │
                    └─────┬───────────────┘
               ┌──────────▼────────────┐
               │  Git リポジトリ         │
               │  tasks/<id>/          │  ← タスクバス
               │  collected_artifacts/ │  ← 成果物
               └───────────────────────┘
```

## 必要な環境

| 要件 | バージョン |
|------|-----------|
| Python | 3.10 以上 |
| Git | 2.x |
| AI エージェント CLI | 下記参照 |

**AI エージェント CLI (いずれか1つ)**

| エージェント | CLI | インストール |
|------------|-----|------------|
| Claude Code | `claude` | [Claude Code](https://github.com/anthropics/claude-code) |
| GitHub Copilot | `gh` + Copilot 拡張 | `gh extension install github/gh-copilot` |
| Amazon Q | `q` | [Amazon Q CLI](https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-installing.html) |
| Kiro | `kiro-cli` | *install instructions depend on distribution* |

---

## クイックスタート (macOS / Linux)

### インストール

ターミナルを開き、リポジトリのルートで実行します。

```bash
# デフォルト設定でインストール (Claude, port 8080)
bash install.sh

# GitHub Copilot を使う場合
bash install.sh --agent-type copilot

# Amazon Q を使う場合
bash install.sh --agent-type amazon-q

# Kiro を使う場合
bash install.sh --agent-type kiro

# ポートやインストール先を変える場合
bash install.sh --port 9090 --install-dir /opt/share-work

# サービス登録をスキップする場合
bash install.sh --no-service
```

インストーラーが行うこと:

1. Python 3.10+ / Git / エージェント CLI の存在確認
2. `~/share-work` にファイルをコピー
3. タスクバス用ローカル bare Git リポジトリ (`~/share-work-tasks.git`) を作成
4. Python 仮想環境 (`.venv`) を作成して依存パッケージをインストール
5. `config/server.yaml` を自動生成
6. 管理スクリプト (`scripts/*.sh`) を生成
7. **macOS**: launchd ユーザーエージェント (`~/Library/LaunchAgents/com.share-work.server.plist`) に登録
   **Linux**: systemd ユーザーサービス (`~/.config/systemd/user/share-work.service`) に登録

### サーバーの管理

```bash
# バックグラウンド起動
~/share-work/scripts/start-daemon.sh

# 状態確認 (プロセス / ヘルスチェック / ログ末尾)
~/share-work/scripts/status.sh

# 停止
~/share-work/scripts/stop.sh

# フォアグラウンド起動 (デバッグ用)
~/share-work/scripts/start.sh
```

**macOS (launchd) でのサービス管理:**

```bash
launchctl start  com.share-work.server
launchctl stop   com.share-work.server
```

**Linux (systemd) でのサービス管理:**

```bash
systemctl --user start   share-work
systemctl --user stop    share-work
systemctl --user status  share-work
systemctl --user restart share-work
```

---

## クイックスタート (Windows)

### インストール

PowerShell を開き、リポジトリのルートで実行します。

```powershell
# デフォルト設定でインストール (Claude, port 8080)
.\install.ps1

# GitHub Copilot を使う場合
.\install.ps1 -AgentType copilot

# Amazon Q を使う場合
.\install.ps1 -AgentType amazon-q

# Kiro を使う場合
.\install.ps1 -AgentType kiro

# ポートやインストール先を変える場合
.\install.ps1 -Port 9090 -InstallDir D:\tools\share-work

# タスクスケジューラへの登録をスキップする場合
.\install.ps1 -NoService
```

インストーラーが行うこと:

1. Python / Git / エージェント CLI の存在確認
2. `%USERPROFILE%\share-work` にファイルをコピー
3. タスクバス用ローカル bare Git リポジトリ (`share-work-tasks.git`) を作成
4. Python 仮想環境 (`.venv`) を作成して依存パッケージをインストール
5. `config/server.yaml` を自動生成
6. `launch.pyw` (コンソールなしデーモン起動スクリプト) を生成
7. 管理スクリプト (`scripts/*.ps1`) を生成
8. Windows タスクスケジューラにログオン時自動起動タスクを登録

### サーバーの管理

```powershell
# バックグラウンド起動
powershell %USERPROFILE%\share-work\scripts\start-daemon.ps1

# 状態確認 (プロセス / ヘルスチェック / ログ末尾)
powershell %USERPROFILE%\share-work\scripts\status.ps1

# 停止
powershell %USERPROFILE%\share-work\scripts\stop.ps1

# フォアグラウンド起動 (デバッグ用)
powershell %USERPROFILE%\share-work\scripts\start.ps1
```

---

## タスクの投入と確認

### タスクを投入する

```bash
# 基本: 成果物は tasks/<id>/artifacts/ に保存
curl -X POST http://localhost:8080/tasks \
  -H "Content-Type: application/json" \
  -d '{"requirement": "Pythonで素数判定関数を実装してテストも書いて", "by": "alice"}'
```

```json
{"task_ids": ["task-20260313-a3f9x2"]}
```

```bash
# repo_path 指定: 対象リポジトリのブランチに成果物をコミット
curl -X POST http://localhost:8080/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "requirement": "README に使い方のセクションを追加して",
    "by": "alice",
    "repo_path": "/home/alice/my-project"
  }'
```

Worker は `/home/alice/my-project` に `share-work/task-20260313-a3f9x2` ブランチを作成し、AI が作業した後にコミットします。`result_branch` でブランチ名を確認できます。

### タスク一覧を確認する

```bash
# 全タスク
curl http://localhost:8080/tasks

# ステータスで絞り込み
curl "http://localhost:8080/tasks?status=open,in_progress"
```

### 特定タスクの詳細を確認する

```bash
curl http://localhost:8080/tasks/task-20260313-a3f9x2
```

### タスクをキャンセルする

```bash
curl -X DELETE http://localhost:8080/tasks/task-20260313-a3f9x2
```

### ローカルモードでタスクを投入する (即座に実行)

**macOS / Linux:**
```bash
# このサーバー上で即座に実行
~/share-work/scripts/submit-task.sh --local "Pythonで素数判定関数を実装してテストも書いて"

# リポジトリ指定と組み合わせることも可能
~/share-work/scripts/submit-task.sh --local --repo /home/alice/my-project "READMEを更新して"
```

**Windows:**
```powershell
powershell %USERPROFILE%\share-work\scripts\submit-task.ps1 `
  -Requirement "Pythonで素数判定関数を実装してテストも書いて" -Local

# リポジトリ指定と組み合わせ
powershell %USERPROFILE%\share-work\scripts\submit-task.ps1 `
  -Requirement "READMEを更新して" -Local -RepoPath "C:\Users\alice\my-project"
```

**curl で直接:**
```bash
curl -X POST http://localhost:8080/tasks \
  -H "Content-Type: application/json" \
  -d '{"requirement": "バグを修正して", "mode": "local"}'
```

レスポンスにはキュー状態が返ります。

```json
{
  "task_ids": ["task-20260313-a3f9x2"],
  "local_running": ["task-20260313-a3f9x2"],
  "local_queued": []
}
```

スロットが満杯の場合は `local_running` が空で `local_queued` にタスク ID が入ります。スロットが空くと自動でキューから起動されます。

### リポジトリ指定でタスクを投入する (ブランチに成果物をコミット)

**macOS / Linux:**
```bash
~/share-work/scripts/submit-task.sh "README に使い方を追記して" alice /home/alice/my-project
```

**Windows:**
```powershell
powershell %USERPROFILE%\share-work\scripts\submit-task.ps1 `
  -Requirement "README に使い方を追記して" `
  -By "alice" `
  -RepoPath "C:\Users\alice\my-project"
```

Worker は `/home/alice/my-project` (または `C:\Users\alice\my-project`) に `share-work/<task_id>` ブランチを作成し、AI エージェントが作業した内容をコミットします。

```bash
# 作業完了後、result_branch でブランチ名を確認
curl http://localhost:8080/tasks/task-20260313-a3f9x2 | python3 -m json.tool
# => "result_branch": "share-work/task-20260313-a3f9x2"

# 対象リポジトリでブランチを確認・マージ
cd /home/alice/my-project
git log share-work/task-20260313-a3f9x2 --oneline
git merge share-work/task-20260313-a3f9x2
```

### Windows から PowerShell でタスクを投入する

```powershell
powershell %USERPROFILE%\share-work\scripts\submit-task.ps1 `
  -Requirement "Pythonで素数判定関数を実装してテストも書いて"
```

---

## API リファレンス

| メソッド | パス | 説明 |
|---------|------|------|
| `POST` | `/tasks` | タスクを投入 |
| `GET` | `/tasks` | タスク一覧 (`?status=open,claimed,...` で絞り込み可) |
| `GET` | `/tasks/{id}` | タスク詳細 |
| `DELETE` | `/tasks/{id}` | タスクのキャンセル |
| `GET` | `/workers` | ワーカー状態一覧 |
| `GET` | `/health` | ヘルスチェック |
| `GET` | `/metrics` | Prometheus 形式メトリクス |

**POST /tasks リクエストボディ**

```json
{
  "requirement": "自然文で書いた要件 (必須)",
  "by": "投稿者名 (省略可)",
  "repo_path": "/path/to/your/repo (省略可)",
  "mode": "local (省略可)"
}
```

| フィールド | 説明 |
|-----------|------|
| `requirement` | 自然文で書いた要件 (必須) |
| `by` | 投稿者名 (省略時: `"http-client"`) |
| `repo_path` | 作業対象の Git リポジトリパス。指定すると Worker はここにブランチを作成して成果物をコミットする |
| `mode` | `"local"` を指定するとこのサーバー上で即座に実行する (省略時: 通常の Worker ポーリング) |

**`mode: "local"` の動作:**

1. タスクを Git でクレームし、他 Worker に奪われないようにする
2. 実行スロット (`max_concurrent_tasks`) に空きがあれば即座に起動
3. 空きがなければ **ローカルキュー** に積み、スロットが空き次第 FIFO で起動
4. ローカルキューまたはローカル実行中タスクが存在する間は、リモートタスクのポーリングを停止する (ローカル作業優先)

`repo_path` を指定すると、受注した Worker は指定リポジトリに `share-work/<task_id>` ブランチを作成し、そのブランチ上で AI エージェントを動かして成果物をコミットします。作業が完了したブランチ名は `GET /tasks/{id}` の `result_branch` フィールドで確認できます。

**タスクステータスの遷移**

```
open → claimed → in_progress → done
                             → failed
       (タイムアウト時は open に差し戻し)
```

---

## 設定ファイル

`config/server.yaml` で動作を調整できます。

```yaml
server:
  host: "127.0.0.1"   # バインドアドレス
  port: 8080           # ポート番号

gitlab:
  repo_path: "."       # タスクバス Git リポジトリのパス
  remote: "origin"
  branch: "main"

controller:
  interval: 60                        # 監視ループの間隔 (秒)
  decompose_model: "claude-sonnet-4-6" # タスク分解に使うモデル
  decompose_binary: "claude"          # タスク分解に使う CLI バイナリ
  timeouts:
    claim_ttl: 300                    # claimed 状態のタイムアウト (秒)
    execution_ttl: 3600               # in_progress 状態のタイムアウト (秒)
  cleanup:
    enabled: true
    keep_failed_tasks: true
    artifacts_dir: "./collected_artifacts"

worker:
  id: "worker-node1"
  interval: 30                        # タスクポーリング間隔 (秒)
  max_concurrent_tasks: 3             # 同時実行タスク数の上限 (Worker 数上限)
                                      # ローカルモードではこの値を超えるタスクはキューに積まれる
  capabilities:
    - general
    - code-generation
    - documentation
  agent:
    type: "claude"                    # エージェント種別 (下記参照)
    model: "claude-sonnet-4-6"        # モデル名 (Claude のみ)
    timeout: 3600                     # エージェント実行タイムアウト (秒)
    sandbox: true                     # サンドボックスモード (Claude のみ)
    # suggestion_type: "shell"        # GitHub Copilot のみ: shell | git | gh
  self_order_delay: 0                 # 自分発注タスクを受注するまでの待機時間 (秒, 0=無効)
  # owner_ids:                        # 「自分」とみなす requested_by 値 (worker_id は常に含まれる)
  #   - alice
  resources:
    has_gpu: false
```

### 自分発注タスクの待機 (`self_order_delay`)

同一サーバーが発注と受注を兼ねる場合に、自分が投入したタスクをすぐに自分で受注してしまう問題を防ぎます。`self_order_delay` に秒数を設定すると、その時間が経過するまで当該タスクをスキップします。他のワーカーが先に受注する機会を与える余裕時間として機能します。

```yaml
worker:
  self_order_delay: 300   # 5分間は自分発注タスクをスキップ
  owner_ids:              # 省略時は worker_id のみが「自分」
    - alice               # POST /tasks の by パラメータと一致する値を列挙
    - worker-node1
```

- `self_order_delay: 0` (既定) で機能無効
- `owner_ids` を省略した場合、`worker_id` だけが自分とみなされる
- 待機時間が過ぎてもタスクが未受注なら、自分が通常通りクレームする

### エージェント種別 (`agent.type`)

| 値 | エイリアス | 使用 CLI | 備考 |
|----|-----------|---------|------|
| `claude` | — | `claude --print` | デフォルト。汎用コーディングエージェント |
| `copilot` | `github-copilot`, `gh-copilot` | `gh copilot suggest` | シェルコマンド提案特化 |
| `amazon-q` | `amazonq`, `q` | `q chat` (stdin) | Amazon Q Developer |

---

## ディレクトリ構成

```
share-work/
├── src/
│   ├── server.py       # 統合HTTPサーバー (メインエントリポイント)
│   ├── controller.py   # タスク分解・監視・成果物収集
│   ├── worker.py       # タスク取得・実行管理
│   ├── agent.py        # AI エージェント CLI ラッパー
│   ├── git_client.py   # Git 操作・楽観的ロック
│   └── models.py       # データモデル
├── config/
│   └── server.yaml     # サーバー設定
├── tasks/              # タスクキュー (Git 管理)
│   └── <task-id>/
│       ├── meta.yaml         # ステータス・優先度・リソース要件
│       ├── requirements.txt  # タスク要件 (自然文)
│       ├── workplan.md       # 実行計画
│       └── artifacts/        # 成果物
│           ├── result.md          # メイン成果物 (必須)
│           └── agent_stdout.txt   # エージェントログ
├── workers/            # ワーカー状態 (Git 管理)
├── collected_artifacts/ # 完了タスクの成果物アーカイブ
├── logs/               # サーバーログ
├── requirements.txt
├── install.sh          # macOS / Linux インストーラー
└── install.ps1         # Windows インストーラー
```

---

## トラブルシューティング

### サーバーが起動しない

**macOS / Linux:**
```bash
tail -50 ~/share-work/logs/server.log
tail -20 ~/share-work/logs/server_error.log
```

**Windows:**
```powershell
Get-Content %USERPROFILE%\share-work\logs\server.log -Tail 50
Get-Content %USERPROFILE%\share-work\logs\server_error.log -Tail 20
```

### ポートが使用中

**macOS / Linux:**
```bash
lsof -i :8080
```

**Windows:**
```powershell
netstat -ano | findstr :8080
```

`config/server.yaml` の `server.port` を変更して再起動してください。

### タスクが `claimed` のまま進まない

Worker がクラッシュした場合、`controller.timeouts.claim_ttl` (既定 300 秒) 経過後に自動で `open` に差し戻されます。

### エージェント CLI が見つからない

**macOS / Linux:**
```bash
which claude   # または gh / q
```

**Windows:**
```powershell
where.exe claude   # または gh / q
```

PATH が通っていない場合は `config/server.yaml` の `agent.binary` にフルパスを指定してください。

```yaml
agent:
  type: "claude"
  binary: "/usr/local/bin/claude"   # macOS / Linux の例
  # binary: "C:/Users/yourname/AppData/Local/..../claude.exe"  # Windows の例
```

### アンインストール

**macOS:**
```bash
launchctl unload ~/Library/LaunchAgents/com.share-work.server.plist
rm -f ~/Library/LaunchAgents/com.share-work.server.plist
rm -rf ~/share-work ~/share-work-tasks.git
```

**Linux:**
```bash
systemctl --user stop    share-work
systemctl --user disable share-work
rm -f ~/.config/systemd/user/share-work.service
systemctl --user daemon-reload
rm -rf ~/share-work ~/share-work-tasks.git
```

**Windows:**
```powershell
Unregister-ScheduledTask -TaskName "share-work-server" -Confirm:$false
Remove-Item -Recurse -Force %USERPROFILE%\share-work
Remove-Item -Recurse -Force %USERPROFILE%\share-work-tasks.git
```

---

## ライセンス

MIT
