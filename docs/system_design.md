# 分散AIエージェントタスク分担システム設計

## 🧩 システム概要

**目的**：Git リポジトリを"メールボックス"として使い、分散環境に配置された CLI 型 AI エージェント（常駐プログラム）が自律的にタスクを分担・完遂する仕組みを構築する。

- タスク発注側は HTTP API に自然文で要件を投げる
- 統合サーバプロセス内の Controller がタスクを AI で分解し Git にプッシュ
- 同じプロセス内の Worker が Git をポーリングしてタスクを受注・実行
- Worker は CLI 型 AI エージェントを呼び出して成果物を生成し Git に返す
- Controller が成果物を取得しタスク関連ファイルをクリーンアップ

### アーキテクチャ概観

```
発注者
  │  POST /tasks  {"requirement": "..."}
  ▼
┌─────────────────────────────────┐
│  TaskServer (src/server.py)     │
│  ┌────────────┐  ┌────────────┐ │
│  │ HTTP API   │  │ Controller │ │  ← background thread
│  │ :8080      │  │ loop       │ │
│  └────────────┘  └────────────┘ │
│           ┌────────────┐        │
│           │ Worker     │        │  ← background thread
│           │ loop       │        │
│           └─────┬──────┘        │
└─────────────────┼───────────────┘
                  │ subprocess
              ┌───▼──────┐
              │ ai-agent │ (claude/gh/q/kiro CLI) 
              └──────────┘
                  │
        ┌─────────▼─────────┐
        │  Git リポジトリ     │  (タスクバス)
        │  tasks/<id>/      │
        │  workers/<id>/    │
        └───────────────────┘
```

---

## 🗂️ リポジトリ構成（メールボックスとしての Git）

```
/tasks/
  <task-id>/
    meta.yaml        # ステータス / 担当 / リソース要件
    requirements.txt # 自然文要件
    workplan.md      # 手順・プロンプト・スキル案
    artifacts/       # 成果物（コード/ドキュメント）
      result.md      # メイン成果物（必須）
      agent_stdout.txt # エージェントの標準出力
      error.log      # 失敗時のエラーログ
      <その他ファイル>
/workers/            # Worker 状態管理
  <worker-id>/
    status.yaml      # Worker の稼働状態・リソース情報
/logs/               # 任意（デバッグ用ログ）
```

---

## 🔄 タスクフロー（エンドツーエンド）

### 全体シーケンス図

```
発注者        HTTP API          Controller          Worker            AI Agent
  |               |                 |                  |                |
  |-- POST /tasks>|                 |                  |                |
  |               |-- decompose() ->|                  |                |
  |               |                 |-- push(open) --> Git              |
  |               |<- {task_ids} ---|                  |                |
  |               |                 |                  |<- pull --------|
  |               |                 |                  |-- claim(push)->Git
  |               |                 |                  |-- push(in_prog)>Git
  |               |                 |                  |-- run agent -->|
  |               |                 |                  |                |-- artifacts
  |               |                 |                  |<- done --------|
  |               |                 |                  |-- push(done) ->Git
  |               |                 |<- poll done ---  Git              |
  |               |                 |-- collect artifacts               |
  |<- GET /tasks ->                 |-- push(cleanup)-> Git             |
```

### 1) 発注（HTTP API）

- 発注者が `POST /tasks` に JSON ボディで自然文要件を送信
- Controller がタスクを AI で分解し、`tasks/<task-id>/` を作成して Git にプッシュ
- **タスク ID の採番**: `task-<YYYYMMDD>-<ランダム6桁英数字>` 形式（例: `task-20260313-a3f9x2`）
- `meta.yaml` の `status` を `open` に設定
- API はすぐに `task_ids` リストを返す（非同期）

```bash
# 発注例
curl -X POST http://localhost:8080/tasks \
  -H "Content-Type: application/json" \
  -d '{"requirement": "Pythonで素数判定関数を実装してテストも書いて", "by": "alice"}'
# → {"task_ids": ["task-20260313-a3f9x2"]}

# 状態確認
curl http://localhost:8080/tasks/task-20260313-a3f9x2
```

#### タスク分解ロジック

1. 自然文要件を Claude CLI に渡し、JSON 配列形式でタスクリストを生成
2. 各タスクに `requirements`・`workplan`・`priority`・`resources`・`depends_on` を設定
3. `depends_on` はインデックス参照（同一呼び出しで生成したタスクへの依存）
4. Claude CLI が利用不可の場合はシングルタスクへのフォールバック

### 2) 受注（ポーリング & CLAIM）

- Worker は Git を**定期ポーリング**（デフォルト 30 秒間隔）
- `status: open` のタスクを検知したら以下を評価：
  - 自ノードのリソース空き状況（CPU・メモリ・ディスク）
  - タスクの `priority` と要求リソース量
  - `capabilities` とタスクの `required_skills` の照合
- 受注可能と判断した場合：
  1. `git pull` で最新を取得
  2. `meta.yaml` の `status` を `claimed`、`assigned_to` を自 Worker ID に更新
  3. `commit` & `push`（競合発生時は `open` に戻り次回ポーリングで再試行）
- 優先度順: `critical > high > normal > low`、同優先度内は作成日時順

### 3) 実行（AI エージェント呼び出し）

1. `status: claimed` → `status: in_progress` に更新（開始タイムスタンプ記録）
2. `requirements.txt` と `workplan.md` を読み込み、AI エージェント CLI（`claude` / `gh` / `q` / `kiro`）を subprocess で呼び出し
3. エージェントは `artifacts/` ディレクトリに成果物を書き出し（`result.md` 必須）
4. 実行完了後 `meta.yaml` を `status: done` に更新してプッシュ
5. 失敗時は `status: failed`、`artifacts/error.log` にエラー詳細を残す

#### AI エージェント呼び出し（実装）

```python
# src/agent.py の AgentRunner が以下を実行（例: `claude`, `kiro`, `q` など）
subprocess.run([
    "claude",
    "--print",
    "--model", "claude-sonnet-4-6",
    "--max-turns", "50",
    prompt_text,          # requirements + workplan を含むプロンプト
], cwd=artifacts_dir, timeout=3600)
```

### 4) 完了回収 & クリーンアップ

- Controller の完了検知ポーリング（デフォルト 60 秒間隔）
- `status: done` を検知したら：
  1. `artifacts/` 以下を `collected_artifacts/<task-id>/` にコピー
  2. `tasks/<task-id>/` ディレクトリを削除してプッシュ
  3. 依存タスクがあれば依存元ディレクトリの消滅を確認して自動アンブロック

---

## 🧠 データモデル詳細

### meta.yaml（完全版）

```yaml
task_id: task-20260313-a3f9x2
created_at: "2026-03-13T12:00:00Z"
updated_at: "2026-03-13T12:05:00Z"
status: open          # open / claimed / in_progress / done / failed / cancelled
requested_by: alice
assigned_to: null     # worker_id（CLAIM 時に埋まる）
priority: normal      # low / normal / high / critical
deadline: null        # ISO 8601 形式（任意）

# リソース要件
resources:
  cpu: 2              # 必要コア数
  memory: 2048        # 必要メモリ（MB）
  disk: 1024          # 必要ディスク（MB）
  gpu: false          # GPU が必要か
  required_skills:    # Worker に必要な能力タグ
    - python
    - code-generation

# タスク依存関係
depends_on: []        # task_id のリスト（ブロッキング依存）

# タイムアウト設定
timeouts:
  claim_ttl: 300      # claimed のまま放置する許容秒数（デフォルト 5 分）
  execution_ttl: 3600 # in_progress の最大実行秒数（デフォルト 1 時間）

# 実行記録
execution:
  started_at: null
  finished_at: null
  worker_node: null   # Worker の物理/仮想ノード識別子
  retry_count: 0
  max_retries: 3
```

### workers/\<worker-id\>/status.yaml

```yaml
worker_id: worker-node1
last_heartbeat: "2026-03-13T12:10:00Z"
status: idle          # idle / busy / offline
capabilities:
  - general
  - code-generation
  - documentation
current_tasks:        # 実行中 task_id のリスト
  - task-20260313-a3f9x2
resources:
  cpu_total: 8
  cpu_available: 6
  memory_total: 16384
  memory_available: 12288
  disk_total: 102400
  disk_available: 80000
  has_gpu: false
```

---

## 🛠️ 主要コンポーネント詳細

### TaskServer（`src/server.py`）

メインエントリポイント。1 プロセスで HTTP API・Controller・Worker をすべて動かす。

```
TaskServer
  ├── HTTP API (ThreadingHTTPServer :8080)
  │     ├── POST   /tasks          タスク発注
  │     ├── GET    /tasks          タスク一覧（?status= フィルタ対応）
  │     ├── GET    /tasks/{id}     タスク詳細
  │     ├── DELETE /tasks/{id}     タスクキャンセル
  │     ├── GET    /workers        Worker 状態一覧
  │     ├── GET    /health         ヘルスチェック（JSON）
  │     └── GET    /metrics        Prometheus テキスト形式
  ├── Controller loop  (daemon thread)
  └── Worker loop      (daemon thread)
```

起動方法：

```bash
python src/server.py --config config/server.yaml
python src/server.py --port 8080   # デフォルト設定で起動
```

### Controller（`src/controller.py`）

`TaskServer` から background thread として実行される。

#### 責務

- 要件受信 → AI によるタスク分解 → Git push
- 完了タスクの検知・成果物取得・クリーンアップ
- タイムアウト監視（`claimed` / `in_progress` の放置検出）
- 失敗タスクのリトライ管理
- 依存関係の解決と次タスクのアンブロック

#### タイムアウト処理

| 状態 | TTL | 処理 |
|------|-----|------|
| `claimed` | 5 分 | `status: open` に戻す（Worker クラッシュ想定） |
| `in_progress` | 1 時間 | `retry_count` インクリメント → `status: open` に戻す |
| `in_progress`（`max_retries` 超過） | - | `status: failed` に設定 |

#### 依存関係の解決

- タスク完了 → `tasks/<id>/` が削除される
- 次ポーリング時に `depends_on` に含まれるディレクトリが存在しなければブロック解除
- 依存タスクが `failed` のまま残っている場合は依存先タスクはブロックされ続ける

### Worker（`src/worker.py`）

`TaskServer` から background thread として実行される。

#### 責務

- Git ポーリング → 受注判定 → タスク実行 → 結果報告
- ハートビート更新（`workers/<id>/status.yaml` を定期更新）
- `AgentRunner` 経由で claude CLI を実行

#### 並列実行管理

- `threading.Semaphore(max_concurrent_tasks)` で同時実行数を制御（デフォルト 3）
- 各タスクは独立した daemon thread で実行
- `_active_tasks: dict[task_id, Thread]` で実行中タスクを管理

#### リソース判定

```python
def _can_handle(self, meta: TaskMeta) -> bool:
    avail = self._get_available_resources()  # psutil でリアルタイム計測
    return (
        avail["cpu"]    >= meta.resources.cpu
        and avail["memory"] >= meta.resources.memory
        and avail["disk"]   >= meta.resources.disk
        and all(s in self.capabilities for s in meta.resources.required_skills)
        and (not meta.resources.gpu or self._resources.has_gpu)
    )
```

### AgentRunner（`src/agent.py`）

Worker から呼び出される claude CLI ラッパー。

```
入力:
  requirements.txt  → 自然文の要件定義
  workplan.md       → 実行手順・プロンプト案（任意）

出力:
  artifacts/
    result.md         → メイン結果（必須、なければ自動生成）
    agent_stdout.txt  → エージェントの標準出力
    error.log         → 失敗時のみ
    <その他生成ファイル>
```

| オプション | デフォルト | 説明 |
|------------|-----------|------|
| `binary` | `claude` | CLI バイナリのパスまたはコマンド名 |
| `model` | `claude-sonnet-4-6` | 使用モデル |
| `timeout` | 3600 秒 | エージェントプロセスのタイムアウト |
| `sandbox` | `true` | `false` のとき `--dangerously-skip-permissions` を付与 |

### GitClient（`src/git_client.py`）

Controller と Worker が共有する Git 操作ライブラリ。

#### 楽観的ロック（CLAIM フロー）

```
Worker A        Worker B         Git リポジトリ
   |               |                |
   |-- pull ------>|                |
   |               |-- pull ------> |
   |-- push(A) --->|                | ← A が先に成功
   |               |-- push(B) ---->| ← B は conflict で失敗 (GitConflictError)
   |               |-- reset HEAD~1 |   ローカルコミットをロールバック
   |               |-- pull ------->|
   |               |  (status=claimed by A → 諦める)
```

#### 指数バックオフリトライ

push 競合やネットワークエラー時は最大 4 回リトライ（2s → 4s → 8s → 16s）。

```python
def commit_and_push_with_retry(self, message, paths, max_retries=4):
    for attempt in range(max_retries + 1):
        try:
            self.pull()
            self.commit_and_push(message, paths)
            return
        except GitConflictError:
            if attempt == max_retries:
                raise
            time.sleep(2 ** attempt)
```

---

## ⚙️ 設定管理

統合サーバは `config/server.yaml` 1 ファイルで全コンポーネントを設定する。

```yaml
server:
  host: "0.0.0.0"
  port: 8080

gitlab:
  repo_path: "."        # タスクバス Git リポジトリのパス
  remote: "origin"
  branch: "main"

controller:
  interval: 60          # 監視ループ間隔（秒）
  decompose_model: "claude-sonnet-4-6"
  timeouts:
    claim_ttl: 300
    execution_ttl: 3600
  cleanup:
    enabled: true
    keep_failed_tasks: true
    artifacts_dir: "./collected_artifacts"

worker:
  id: "worker-node1"    # ユニーク識別子
  interval: 30          # タスクポーリング間隔（秒）
  heartbeat_interval: 60
  max_concurrent_tasks: 3
  capabilities:
    - general
    - code-generation
    - documentation
  agent:
    binary: "claude"
    model: "claude-sonnet-4-6"
    timeout: 3600
    sandbox: true
  resources:
    has_gpu: false
    # cpu_total / memory_total / disk_total は実行時に psutil で自動検出
```

---

## 📡 HTTP API リファレンス

### POST /tasks

タスクを発注する。Claude CLI でタスク分解し Git にプッシュ後、即座に応答を返す。

**リクエスト**
```json
{
  "requirement": "Pythonで素数判定関数を実装してテストも書いて",
  "by": "alice"
}
```

**レスポンス** `201 Created`
```json
{
  "task_ids": ["task-20260313-a3f9x2"]
}
```

### GET /tasks

タスク一覧を返す。`?status=open,claimed` でフィルタ可能。

**レスポンス** `200 OK`
```json
[
  {
    "task_id": "task-20260313-a3f9x2",
    "status": "in_progress",
    "priority": "normal",
    "assigned_to": "worker-node1",
    "created_at": "2026-03-13T12:00:00Z",
    ...
  }
]
```

### GET /tasks/{id}

タスクの詳細（`meta.yaml` の内容）を返す。

### DELETE /tasks/{id}

タスクをキャンセルする。`done` / `failed` 済みのタスクには `409 Conflict` を返す。

### GET /workers

Worker の状態一覧（`workers/*/status.yaml` の内容）を返す。

### GET /health

```json
{
  "status": "ok",
  "worker_id": "worker-node1",
  "current_tasks": ["task-20260313-a3f9x2"],
  "slots_free": 2
}
```

### GET /metrics

Prometheus テキスト形式。

```
worker_cpu_available{worker_id="worker-node1"} 6
worker_memory_available_mb{worker_id="worker-node1"} 12288
worker_active_tasks{worker_id="worker-node1"} 1
worker_slots_free{worker_id="worker-node1"} 2
```

---

## ⚠️ リスク/対策

### レースコンディション（タスク取り合い）

**問題**: 複数 Worker が同じ `open` タスクを同時に `claimed` にしようとする。

**対策（楽観的ロック）**:
1. Worker が `git pull` で最新状態を取得
2. `meta.yaml` を `claimed` に更新して `commit` & `push`
3. push が競合（`rejected`）した場合はローカルコミットをロールバック（`git reset HEAD~1 --soft`）
4. `git pull` し直してすでに `claimed` なら諦め、次ポーリングで別タスクを探す

### 中断・失敗のリカバリ

| シナリオ | 検知方法 | リカバリ |
|----------|---------|---------|
| Worker クラッシュ（`claimed` 放置） | `claim_ttl` 超過 | `status: open` に戻す |
| エージェント実行タイムアウト | `execution_ttl` 超過 | リトライ or `failed` |
| Git 一時停止 | push/pull エラー | 指数バックオフでリトライ（最大 4 回） |
| タスク分解失敗 | Claude CLI エラー | シングルタスクへのフォールバック |

### セキュリティ/安全性

| 対策 | 詳細 |
|------|------|
| アクセス制御 | Git リモートの認証情報で読み書き権限を管理 |
| コマンドサンドボックス | `agent_sandbox: true` で `--dangerously-skip-permissions` を付与しない |
| トークン管理 | Git 認証情報は環境変数のみで管理、設定ファイルへのハードコード禁止 |

---

## 📊 監視・オブザーバビリティ

### メトリクス（`GET /metrics`、Prometheus 互換）

```
worker_cpu_available{worker_id}        # 利用可能 CPU コア数
worker_memory_available_mb{worker_id}  # 利用可能メモリ（MB）
worker_active_tasks{worker_id}         # 実行中タスク数
worker_slots_free{worker_id}           # 空き実行スロット数
```

### ログ

標準出力に以下の形式で出力：

```
2026-03-13T12:05:00Z INFO     worker: Worker worker-node1 claimed task task-20260313-a3f9x2
2026-03-13T12:05:01Z INFO     worker: Executing task task-20260313-a3f9x2
2026-03-13T12:10:00Z INFO     git_client: Task task-20260313-a3f9x2 finished with status done
```

---

## 🧪 拡張案

### 優先度の高い拡張

| 拡張 | 概要 | 実装難易度 |
|------|------|-----------|
| **WebHook / イベント駆動** | Git WebHook でポーリングを廃止し、即時タスク検知を実現 | 中 |
| **Web UI** | タスク一覧・進捗・Worker 状態のダッシュボード | 中 |
| **複数 Worker ノード** | 複数マシンでそれぞれ `server.py` を起動し同一 Git リポジトリを共有 | 低 |
| **複数成果物バージョン** | artifacts に世代管理を導入し差分比較を可能に | 中 |

### 将来拡張

| 拡張 | 概要 | 実装難易度 |
|------|------|-----------|
| **リソース可視化** | Prometheus + Grafana で Worker 稼働状況をグラフ表示 | 低 |
| **タスクテンプレート** | 頻出タスクのテンプレート化と再利用 | 低 |
| **並列サブタスク** | 親タスクを並列実行可能なサブタスクに自動分割 | 高 |
| **コスト追跡** | API 呼び出しコスト・トークン消費量の記録と予算管理 | 中 |

---

## 🚀 デプロイ・起動手順

### 依存ライブラリのインストール

```bash
pip install -r requirements.txt
# gitpython, PyYAML, psutil, requests
```

### 最小構成（単一マシン）

```bash
# 1. 設定ファイルを編集
cp config/server.yaml config/server.local.yaml
# vim config/server.local.yaml

# 2. サーバ起動（Controller + Worker + HTTP API が同一プロセスで動作）
python src/server.py --config config/server.local.yaml

# 3. タスクを発注
curl -X POST http://localhost:8080/tasks \
  -H "Content-Type: application/json" \
  -d '{"requirement": "hello world を出力する Python スクリプトを書いて", "by": "alice"}'

# 4. 状態を確認
curl http://localhost:8080/tasks
curl http://localhost:8080/health
```

### 複数ノード構成

各ノードで `server.py` を起動し、同一 Git リモートを指定するだけでスケールアウトできる。
`worker.id` はノードごとにユニークにすること。

```
[ノード A]                      [Git サーバ]          [ノード B]
  python src/server.py  ←────→  task-bus リポジトリ ←────→  python src/server.py
  worker_id: worker-nodeA        (タスクバス)                worker_id: worker-nodeB
  port: 8080                                                 port: 8080
```

### プロジェクト構成

```
.
├── src/
│   ├── server.py       # 統合 HTTP サーバ（メインエントリポイント）
│   ├── controller.py   # Controller ロジック・タスク分解
│   ├── worker.py       # Worker ロジック・タスク受注・実行
│   ├── agent.py        # AI CLI ラッパー（AgentRunner）（claude / gh / q / kiro など）
│   ├── git_client.py   # Git 操作クライアント（楽観的ロック）
│   └── models.py       # データモデル（TaskMeta, WorkerState 等）
├── config/
│   ├── server.yaml     # 統合設定（controller + worker + HTTP）
│   ├── controller.yaml # Controller 単体起動用設定（legacy）
│   └── worker.yaml     # Worker 単体起動用設定（legacy）
├── tasks/              # タスクバス（Git 管理）
├── workers/            # Worker 状態（Git 管理）
├── logs/               # ログ出力先
├── collected_artifacts/# 完了タスクの成果物収集先（Git 管理外）
└── requirements.txt
```
