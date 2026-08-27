# 動かす

Phase 0 の構成は 3 つのプロセスに分かれる。**採点はレビューの前に、
レビューとは独立に走る**（ADR 0007）。

```
aijudge-web       学習者：提出と結果表示          :8080
aijudge-worker    採点：キューを消費              （常駐）
aijudge-review    教員：確認して確定              :8765
```

## 開発機（macOS / 単独プロセス）

```fish
docker compose up -d                    # PostgreSQL + MinIO
set -gx AIJUDGE_DATABASE_URL postgresql+psycopg://aijudge:aijudge@localhost:5432/aijudge

uv sync --extra dev
uv run aijudge-web --create-schema      # 初回だけスキーマを作る
uv run aijudge-worker
uv run aijudge-review
```

`AIJUDGE_DATABASE_URL` を設定しない場合、既定は同じ PostgreSQL の URL。
SQLite でも動くが**行ロックが無い**ので、ワーカーは 1 プロセスだけにする
（`Database.supports_row_locking` が偽になり、CLI が警告する）。

## 実提出を通す前に

**seatbelt 単体で実学生のコードを走らせてはならない。** プロセス数を
封じ込められず、実測で開発機のプロセス表が埋まった（ADR 0006）。
`sandbox-exec` が止めるのは書き込み・通信・家目録の読み取りまでで、
プロセス数だけが穴になっている。

コンテナ実行環境を入れてから運用に入る。**macOS でも同じ水準に揃う。**

```fish
brew install colima docker      # 軽量 VM 上の Linux コンテナ
colima start --cpu 4 --memory 8

AIJUDGE_SANDBOX=docker uv run pytest packages/sandbox/tests/test_container.py -v
```

このファイルが **skip ではなく pass** することが、実提出を通す前提条件。
skip は「検証していない」であって「安全」ではない。
2026-08-28 に colima で 15 件通過を確認済み（gVisor の 1 件のみ skip）。

作業域は既定で `~/.aijudge/work`。**ホストの一時ディレクトリを使わない**のは、
macOS の `/var/folders/...` がコンテナ実行環境にマウントされず、bind mount が
黙って空になるため（ADR 0006）。変えるなら `AIJUDGE_SANDBOX_WORKDIR` に
マウントされるパスを指定する。構築時に検証するので、マウントされていなければ
採点は始まらず `SandboxUnavailable` になる。

バックエンドは自動選択で最も強いものを取る（gVisor > docker > seatbelt）。
何が選ばれたかは採点結果の `isolation` に残るので、あとから
「どの水準で付いた点か」を選別できる。

```fish
uv run python -c "
from aijudge_sandbox import build_sandbox
s = build_sandbox()
print(s.name, s.isolation.value, sorted(l.value for l in s.limitations))
"
```

## 環境変数

| 変数 | 用途 | 既定 |
|---|---|---|
| `AIJUDGE_DATABASE_URL` | 接続先 | ローカル PostgreSQL |
| `AIJUDGE_ARTIFACT_DIR` | 提出物の置き場所 | `~/.aijudge/artifacts` |
| `AIJUDGE_OBSERVATION_DIR` | 観測レコード（測定用・任意） | `~/.aijudge/observations` |
| `AIJUDGE_SANDBOX` | 隔離バックエンド（`auto`/`docker`/`gvisor`/`seatbelt`） | `auto` |
| `AIJUDGE_SANDBOX_WORKDIR` | 作業域の置き場所。コンテナがマウントするパスであること | `~/.aijudge/work` |
| `AIJUDGE_LLM_BASE_URL` / `AIJUDGE_LLM_MODEL` | ローカル LLM | — |
| `AIJUDGE_FEEDBACK_MODEL` | フィードバック生成のモデル。未設定なら要約に落ちる | — |

## 測定（Phase 1・任意）

```fish
uv run aijudge-eval --subject cs_intro_c
```

記録済みの観測を読むだけで、**採点は行わない**。`packages/analytics` と
`apps/evalrunner` を削除しても採点は動く（ADR 0007）。
