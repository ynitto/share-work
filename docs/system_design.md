# 分散AIエージェントタスク分担システム設計

## 🧩 システム概要

**目的**：GitLab を"メールボックス"として使い、分散環境に配置された CLI 型 AI エージェント（常駐プログラム）が自律的にタスクを分担・完遂する仕組みを構築する。

- タスク発注側は自然文で要件を投げる
- 発注側常駐プログラムがタスクを分解し GitLab にプッシュ
- 受注側常駐プログラムが GitLab をポーリングしてタスクを受注
- 受注側は CLI 型 AI エージェントを動かして成果物を生成し GitLab に返す
- 発注側は成果物を取得しタスク関連ファイルをクリーンアップ

---

## 🗂️ リポジトリ構成（メールボックスとしてのGitLab）

```
/tasks/
  <task-id>/
    meta.yaml        # ステータス / 担当 / リソース要件
    requirements.txt # 自然文要件
    workplan.md      # 手順・プロンプト・スキル案
    artifacts/       # 成果物（コード/ドキュメント）
      result.md      # メイン成果物
      error.log      # 失敗時のエラーログ
      <その他ファイル>
/logs/               # 任意（デバッグ用ログ）
  <worker-id>/
    <date>.log
/workers/            # Worker状態管理
  <worker-id>/
    status.yaml      # Worker の稼働状態・リソース情報
```

---

## 🔄 タスクフロー（エンドツーエンド）

### 全体シーケンス図

```
発注者         Controller              GitLab            Worker(s)         AI Agent
  |                |                     |                   |                |
  |-- 自然文要件 -->|                     |                   |                |
  |                |-- タスク分解        |                   |                |
  |                |-- push(open) ------>|                   |                |
  |                |                     |<-- polling -------|                |
  |                |                     |--- 新規タスク ---->|                |
  |                |                     |<-- push(claimed)--|                |
  |                |                     |<-- push(in_prog)--|                |
  |                |                     |                   |-- タスク渡す ->|
  |                |                     |                   |<-- 成果物 -----|
  |                |                     |<-- push(done) ----|                |
  |                |<-- polling -------->|                   |                |
  |                |--- 完了検知         |                   |                |
  |                |-- 成果物取得 ------>|                   |                |
  |<-- 成果物 -----|                     |                   |                |
  |                |-- cleanup(delete) ->|                   |                |
```

### 1) 発注（自然文入力）

- 発注者が発注ノードの常駐プログラムに自然文で要件を投げる
- Controller がタスクを AI で分解し、`tasks/<task-id>/` を作成して GitLab にプッシュ
- **タスクIDの採番**: `task-<YYYYMMDD>-<ランダム6桁英数字>` 形式（例: `task-20260313-a3f9x2`）
- `meta.yaml` の `status` を `open` に設定

#### タスク分解ロジック

1. 自然文要件をパース（AI エージェントで解釈）
2. 必要リソース（CPU/メモリ/ディスク）を見積もり
3. 依存関係がある場合は複数タスクに分割し `depends_on` を設定
4. `workplan.md` に実行手順・プロンプト・スキルを記述

### 2) 受注（ポーリング & 受注）

- 受注側ノードは GitLab を**定期ポーリング**（デフォルト30秒間隔）
- `status: open` のタスクを検知したら以下を評価：
  - 自ノードのリソース空き状況（CPU使用率 < 80%、メモリ使用率 < 70% 等）
  - タスクの `priority` と要求リソース量
  - `worker_capabilities` とタスクの `required_skills` の照合
- 受注可能と判断した場合：
  1. `git pull` で最新を取得
  2. `meta.yaml` の `status` を `claimed`、`assigned_to` を自 Worker ID に更新
  3. `commit` & `push`（競合発生時は `open` に戻り次回ポーリングで再試行）

### 3) 実行（AIエージェント呼び出し）

1. `status: claimed` → `status: in_progress` に更新（開始タイムスタンプ記録）
2. `requirements.txt` と `workplan.md` を読み込み、CLI 型 AI エージェントを呼び出し
3. 成果物を `artifacts/` に書き出し
4. 実行完了後 `meta.yaml` を `status: done` に更新してプッシュ
5. 失敗時は `status: failed`、`artifacts/error.log` にエラー詳細を残す

#### AI エージェント呼び出しインターフェース

```bash
# 実行例
claude-agent \
  --task-dir ./tasks/<task-id>/ \
  --requirements ./tasks/<task-id>/requirements.txt \
  --workplan    ./tasks/<task-id>/workplan.md \
  --output-dir  ./tasks/<task-id>/artifacts/ \
  --timeout     3600
```

### 4) 完了回収 & クリーンアップ

- Controller の完了検知ポーリング（デフォルト60秒間隔）
- `status: done` を検知したら：
  1. `artifacts/` 以下をローカルにコピー
  2. `tasks/<task-id>/` ディレクトリを削除してプッシュ
  3. 依存タスクがあれば次タスクのブロックを解除（`status: open` に変更）

---

## 🧠 データモデル詳細

### meta.yaml（完全版）

```yaml
task_id: task-20260313-a3f9x2
created_at: "2026-03-13T12:00:00Z"
updated_at: "2026-03-13T12:05:00Z"
status: open            # open / claimed / in_progress / done / failed
requested_by: userA
assigned_to: null       # worker_id（CLAIM時に埋まる）
priority: normal        # low / normal / high / critical
deadline: null          # ISO8601形式（任意）

# リソース要件
resources:
  cpu: 2                # 必要コア数
  memory: 2048          # 必要メモリ（MB）
  disk: 1024            # 必要ディスク（MB）
  gpu: false            # GPU が必要か
  required_skills:      # Worker に必要な能力タグ
    - python
    - code-generation

# タスク依存関係
depends_on: []          # task_id のリスト（ブロッキング依存）

# タイムアウト設定
timeouts:
  claim_ttl: 300        # claimed のまま放置する許容秒数（デフォルト5分）
  execution_ttl: 3600   # in_progress の最大実行秒数（デフォルト1時間）

# 実行記録
execution:
  started_at: null
  finished_at: null
  worker_node: null     # Worker の物理/仮想ノード識別子
  retry_count: 0
  max_retries: 3
```

### workers/<worker-id>/status.yaml

```yaml
worker_id: worker-node42
last_heartbeat: "2026-03-13T12:10:00Z"
status: idle            # idle / busy / offline
capabilities:           # 担当可能なスキルタグ
  - python
  - code-generation
  - documentation
current_task: null      # 実行中 task_id
resources:
  cpu_total: 8
  cpu_available: 6
  memory_total: 16384
  memory_available: 12288
```

---

## 🛠️ 主要コンポーネント詳細

### Controller（発注側常駐プログラム）

#### 責務

- 要件受信 → タスク分解 → GitLab push
- 完了タスクの検知・成果物取得・クリーンアップ
- タイムアウト監視（`claimed` / `in_progress` の放置検出）
- 依存関係の解決と次タスクのアンブロック

#### 内部ループ（疑似コード）

```python
class Controller:
    def run(self):
        while True:
            # 新規要件の受付
            for req in self.incoming_queue.drain():
                tasks = self.decompose(req)       # AI でタスク分解
                for task in tasks:
                    self.gitlab.push_task(task)   # GitLab に push

            # 完了・失敗タスクの処理
            for task in self.gitlab.list_tasks(status=['done', 'failed']):
                if task.status == 'done':
                    self.collect_artifacts(task)
                    self.gitlab.delete_task(task)
                    self.unblock_dependents(task)
                elif task.status == 'failed':
                    self.handle_failure(task)     # リトライ or エスカレーション

            # タイムアウト監視
            self.check_timeouts()

            time.sleep(CONTROLLER_POLL_INTERVAL)  # デフォルト60秒
```

#### タイムアウト処理

| 状態 | TTL | 処理 |
|------|-----|------|
| `claimed` | 5分 | `status: open` に戻す（Worker がクラッシュした想定） |
| `in_progress` | 1時間 | `retry_count` をインクリメントし `status: open` に戻す |
| `in_progress` (max_retries超過) | - | `status: failed` に設定 |

---

### Worker（受注側常駐プログラム）

#### 責務

- GitLab ポーリング → 受注判定 → タスク実行 → 結果報告
- ハートビート更新（`workers/<id>/status.yaml` を定期更新）
- CLI エージェント実行・エラーハンドリング

#### 内部ループ（疑似コード）

```python
class Worker:
    def run(self):
        while True:
            # ハートビート更新
            self.update_heartbeat()

            # 並列実行スロットが空いている場合のみタスク受注
            if self.has_free_slot():
                tasks = self.gitlab.list_tasks(status='open')
                for task in self.prioritize(tasks):
                    if self.can_handle(task):
                        if self.try_claim(task):   # 楽観的ロック
                            self.execute_async(task)
                            break

            time.sleep(WORKER_POLL_INTERVAL)  # デフォルト30秒

    def try_claim(self, task) -> bool:
        """楽観的ロックによる CLAIM（失敗時は False を返す）"""
        try:
            self.gitlab.pull()
            task.meta['status'] = 'claimed'
            task.meta['assigned_to'] = self.worker_id
            self.gitlab.commit_and_push(task)
            return True
        except GitConflictError:
            return False  # 他 Worker が先に CLAIM した

    def execute_async(self, task):
        """別スレッド/プロセスでタスクを実行"""
        thread = threading.Thread(target=self._execute, args=(task,))
        thread.start()

    def _execute(self, task):
        try:
            task.meta['status'] = 'in_progress'
            task.meta['execution']['started_at'] = now()
            self.gitlab.commit_and_push(task)

            result = self.agent.run(
                requirements=task.requirements,
                workplan=task.workplan,
                output_dir=task.artifacts_dir
            )

            task.meta['status'] = 'done'
            task.meta['execution']['finished_at'] = now()
            self.gitlab.commit_and_push(task)
        except Exception as e:
            task.meta['status'] = 'failed'
            write_error_log(task.artifacts_dir / 'error.log', e)
            self.gitlab.commit_and_push(task)
```

#### リソース判定ロジック

```python
def can_handle(self, task) -> bool:
    meta = task.meta
    res = meta['resources']
    caps = meta['resources'].get('required_skills', [])

    return (
        self.cpu_available >= res['cpu']
        and self.memory_available >= res['memory']
        and self.disk_available >= res['disk']
        and all(skill in self.capabilities for skill in caps)
        and (not res.get('gpu') or self.has_gpu)
    )
```

#### 並列実行管理

- Worker ごとに `max_concurrent_tasks`（デフォルト3）を設定
- セマフォで同時実行数を制御
- 各タスクは独立したサブプロセス/コンテナで実行（副作用の隔離）

---

### CLI 型 AI エージェント

#### 役割

各 Worker ノードにインストール済みの AI エージェント（例: Claude Code CLI）。

#### 実行インターフェース

```
入力:
  requirements.txt  → 自然文の要件定義
  workplan.md       → 実行手順・使用スキル・プロンプト案

出力:
  artifacts/        → 生成成果物
    result.md       → メイン結果（必須）
    <ファイル群>    → コード・ドキュメント等
    error.log       → 失敗時のみ
```

#### 実行オプション

| オプション | デフォルト | 説明 |
|------------|-----------|------|
| `--timeout` | 3600秒 | エージェント実行タイムアウト |
| `--max-tokens` | 100000 | 最大トークン数 |
| `--model` | claude-sonnet-4-6 | 使用モデル |
| `--sandbox` | true | サンドボックス実行（危険コマンド制限） |

---

## ⚙️ 設定管理

### Controller 設定（controller.yaml）

```yaml
gitlab:
  url: https://gitlab.example.com
  token: ${GITLAB_TOKEN}           # 環境変数で注入
  repo: org/task-bus
  branch: main

polling:
  controller_interval: 60          # 秒
  task_decompose_model: claude-sonnet-4-6

timeouts:
  claim_ttl: 300
  execution_ttl: 3600

cleanup:
  enabled: true
  keep_failed_tasks: true          # 失敗タスクはデバッグのため残す
  keep_duration_hours: 24          # 完了タスクの保持期間
```

### Worker 設定（worker.yaml）

```yaml
worker_id: worker-node42           # ユニーク識別子
gitlab:
  url: https://gitlab.example.com
  token: ${GITLAB_TOKEN}
  repo: org/task-bus
  branch: main

polling:
  worker_interval: 30              # 秒
  heartbeat_interval: 60           # 秒

execution:
  max_concurrent_tasks: 3
  agent_binary: claude             # CLI エージェントのパス
  agent_timeout: 3600
  agent_model: claude-sonnet-4-6
  agent_sandbox: true

capabilities:
  - python
  - code-generation
  - documentation

resources:
  cpu_total: 8
  memory_total: 16384              # MB
  disk_total: 102400               # MB
  has_gpu: false
```

---

## ⚠️ リスク/対策（運用上の注意）

### レースコンディション（タスク取り合い）

**問題**: 複数 Worker が同じ `open` タスクを同時に `claimed` にしようとする。

**対策（楽観的ロック）**:
1. Worker が `git pull` で最新状態を取得
2. `meta.yaml` を `claimed` に更新して `commit` & `push`
3. push が競合（`rejected`）した場合は `git pull` し直し、すでに `claimed` なら諦める
4. push 成功ならそのタスクの正式な担当者になれる

```
Worker A        Worker B         GitLab
   |               |                |
   |-- pull ------>|                |
   |               |-- pull ------> |
   |-- push(A) --->|                | ← A が先に成功
   |               |-- push(B) ---->| ← B は conflict で失敗
   |               |-- pull ------->|
   |               |  (status=claimed by A → 諦める)
```

### 中断・失敗のリカバリ

| シナリオ | 検知方法 | リカバリ |
|----------|---------|---------|
| Worker クラッシュ（`claimed` 放置） | `claim_ttl` 超過 | `status: open` に戻す |
| エージェント実行タイムアウト | `execution_ttl` 超過 | リトライ or `failed` |
| GitLab 一時停止 | push/pull エラー | 指数バックオフでリトライ（最大4回） |
| ネットワーク断絶 | ハートビート途絶 | Worker を `offline` 扱い、タスクを再公開 |

### ハートビートによる Worker 死活監視

- Worker は `worker_interval` ごとに `workers/<id>/status.yaml` を更新
- Controller は `last_heartbeat` が `heartbeat_timeout`（デフォルト3分）を超えた Worker を `offline` とみなす
- `offline` Worker のタスクは即座に `status: open` に戻す

### セキュリティ/安全性

| 対策 | 詳細 |
|------|------|
| アクセス制御 | GitLab のプロジェクトアクセストークン・ロールで読み書き権限を管理 |
| コマンドサンドボックス | Worker 側で `--sandbox true` を強制し危険なシェルコマンドを制限 |
| 許可スキル制限 | `allowed_task_types` リストで受注可能なタスク種別を限定 |
| 禁止コマンドリスト | `rm -rf /`, `format`, ネットワーク外部送信等を禁止 |
| トークン管理 | GitLab トークンは環境変数のみで管理、設定ファイルへのハードコード禁止 |
| 監査ログ | 全 `status` 遷移と操作を `/logs/<worker-id>/` に記録 |

---

## 📊 監視・オブザーバビリティ

### メトリクス（Prometheus 互換）

```
# Worker ごとの稼働指標
worker_tasks_total{worker_id, status}     # 処理済みタスク数
worker_cpu_usage{worker_id}               # CPU 使用率
worker_memory_usage{worker_id}            # メモリ使用率

# タスクキュー指標
task_queue_depth{status}                  # ステータス別キュー深さ
task_duration_seconds{task_id}            # タスク実行時間
task_retry_count{task_id}                 # リトライ回数
```

### ログ形式（JSON Lines）

```json
{
  "timestamp": "2026-03-13T12:05:00Z",
  "level": "INFO",
  "worker_id": "worker-node42",
  "task_id": "task-20260313-a3f9x2",
  "event": "task_claimed",
  "details": { "previous_status": "open" }
}
```

### ヘルスチェックエンドポイント

各 Worker に軽量 HTTP サーバを組み込み、以下を公開：

```
GET /health        → { "status": "ok", "worker_id": "...", "current_tasks": 2 }
GET /metrics       → Prometheus テキスト形式
```

---

## 🧪 拡張案

### 優先度の高い拡張

| 拡張 | 概要 | 実装難易度 |
|------|------|-----------|
| **WebHook / イベント駆動** | GitLab WebHook でポーリングを廃止し、即時タスク検知を実現 | 中 |
| **Web UI** | タスク一覧・進捗・Worker状態のダッシュボード | 中 |
| **タスクキャンセル** | `status: cancelled` を追加し発注者がキャンセル可能に | 低 |
| **複数成果物バージョン** | artifacts に世代管理を導入し差分比較を可能に | 中 |

### 将来拡張

| 拡張 | 概要 | 実装難易度 |
|------|------|-----------|
| **リソース可視化** | 各 Worker の稼働状況をグラフで表示 | 低 |
| **タスクテンプレート** | 頻出タスクのテンプレート化と再利用 | 低 |
| **並列サブタスク** | 親タスクを並列実行可能なサブタスクに自動分割 | 高 |
| **コスト追跡** | API 呼び出しコスト・トークン消費量の記録と予算管理 | 中 |
| **マルチ GitLab** | 異なる GitLab インスタンスにまたがるタスクバス | 高 |

---

## 🚀 デプロイ構成

### 最小構成（開発・検証用）

```
[発注者PC]
  └─ Controller プロセス
       └─ GitLab.com（共有リポジトリ）
            └─ Worker プロセス（同一マシンでもよい）
                  └─ Claude Code CLI
```

### 本番構成

```
[発注者ノード]              [GitLab サーバ]         [Worker ノード群]
  Controller x1  ←───→   task-bus リポジトリ  ←───→  Worker x N
  - タスク分解                                           - AI エージェント実行
  - 成果物収集             [監視]                        - リソース管理
                           Prometheus + Grafana
                           ログ集約（Loki）
```

### 起動手順（概略）

```bash
# 1. GitLab リポジトリの初期化
git clone https://gitlab.example.com/org/task-bus.git
mkdir -p tasks logs workers

# 2. Controller 起動
export GITLAB_TOKEN=<token>
controller --config controller.yaml

# 3. Worker 起動（各ノードで）
export GITLAB_TOKEN=<token>
worker --config worker.yaml --worker-id worker-node42
```
