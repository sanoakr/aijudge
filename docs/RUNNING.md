# 動かす

Phase 0 の構成は 3 つのプロセスに分かれる。**採点はレビューの前に、
レビューとは独立に走る**（ADR 0007）。

```
aijudge-web       学習者：提出と結果表示                    :8080
aijudge-worker    採点：キューを消費                        （常駐）
aijudge-review    教員：確認して確定 + 科目・課題の管理      :8765
aijudge-finalize  成績：締切を過ぎた分を自動確定            （cron か常駐）
aijudge-admin     運用：学期の頭の一括操作（CLI）
```

教員コンソールの `/manage` にコース・課題・締切・受講の管理がある。
学期の頭の一括操作（90 名の登録、課題の一括取り込み）は `aijudge-admin`。
**新規利用者の作成は CLI だけ**にしてある（パスワードの配布が伴うので、
画面に平文を出さない）。

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

## tailnet の他の端末から使う

既定は `127.0.0.1` にしか bind しないので、他の端末からは見えない。
出す方法は 2 つある。

### tailscale serve（推奨）

TLS 終端が前に入り、**本物の証明書で HTTPS になる**。tailnet の中だけに
公開され、LAN には出ない。

```fish
tailscale serve --bg --https=443 8080     # 学生 UI
tailscale serve --bg --https=8443 8765    # 教員 UI

# → https://<ホスト名>.<tailnet>.ts.net/       学生
# → https://<ホスト名>.<tailnet>.ts.net:8443/  教員

tailscale serve status     # 確認
tailscale serve reset      # 取り消し
```

アプリ側は `X-Forwarded-Proto: https` を見て**セッション Cookie に `Secure`
を付ける**ので、追加の設定は要らない。

### Tailscale のアドレスに直接 bind する

```fish
set -l ts (tailscale ip -4)
uv run aijudge-web    --host $ts --port 8080
uv run aijudge-review --host $ts --port 8765
```

平文 HTTP だが、tailnet 内の通信は WireGuard で暗号化される。ただし
**Cookie に `Secure` が付かない**ので、`AIJUDGE_SECURE_COOKIES=1` は
設定しないこと（設定するとブラウザが Cookie を送らずログインできない）。

**`--host 0.0.0.0` は使わないこと。** tailnet だけでなく同じ LAN 上の
全端末に出る。

### 学生に配るとき

tailnet は教員・TA の端末を繋ぐには足りるが、**学生には配れない**
（全員に Tailscale を入れさせることになる）。実運用では学内ネットワークに
リバースプロキシ（TLS 終端）を立て、`AIJUDGE_SECURE_COOKIES=1` を設定する。

## 環境変数

| 変数 | 用途 | 既定 |
|---|---|---|
| `AIJUDGE_DATABASE_URL` | 接続先 | ローカル PostgreSQL |
| `AIJUDGE_ARTIFACT_DIR` | 提出物の置き場所 | `~/.aijudge/artifacts` |
| `AIJUDGE_OBSERVATION_DIR` | 観測レコード（測定用・任意） | `~/.aijudge/observations` |
| `AIJUDGE_SANDBOX` | 隔離バックエンド（`auto`/`docker`/`gvisor`/`seatbelt`） | `auto` |
| `AIJUDGE_SANDBOX_WORKDIR` | 作業域の置き場所。コンテナがマウントするパスであること | `~/.aijudge/work` |
| `AIJUDGE_SECURE_COOKIES` | セッション Cookie に `Secure` を付ける（`1`/`0`）。未設定なら `X-Forwarded-Proto` で判断 | 未設定 |
| `AIJUDGE_LLM_BASE_URL` / `AIJUDGE_LLM_MODEL` | ローカル LLM | — |
| `AIJUDGE_FEEDBACK_MODEL` | フィードバック生成のモデル。未設定なら要約に落ちる | — |

## 締切集中に備える

採点ワーカーは複数立てられる。**ただし PostgreSQL であること。**

```fish
uv run aijudge-worker --name w1 &
uv run aijudge-worker --name w2 &
uv run aijudge-worker --name w3 &
uv run aijudge-worker --name w4 &
```

実測（受講 91 名 × 1 課題 × テスト 5 件、colima 上のコンテナ隔離）:

| ワーカー | 所要 | 秒/件 |
|---:|---:|---:|
| 1 | 53.0 秒 | 0.58 |
| 2 | 34.8 秒 | 0.38 |
| 4 | 25.2 秒 | 0.28 |

91 名の同時提出で「結果表示まで 30 秒以内」を満たすには 4 本必要。

**SQLite でワーカーを複数立てないこと。** 行ロックが無いので同じ提出が
二度採点され、1 つの提出に採点結果が 2 つできる。`aijudge-worker` は
起動時に警告する。

GPU を使う科目と使わない科目でキューを分けたい場合は `--subject` で絞る。

```fish
uv run aijudge-worker --subject net_python --name py1
```

## 成績を閉じる

教員の待ち行列は**学習者が異議を申し立てた提出だけ**である（ADR 0009）。
依頼が出なかった提出は放っておくと未確定のまま残るので、閉じる導線が要る
（ADR 0010）。

### 自動確定（締切で仮確定 → n 時間後に確定）

```
採点完了   暫定。「担当教員の確認を経て確定します」
  ↓ 締切
仮確定     「MM/DD HH:MM に確定します」と学習者に示す。異議はここまで
  ↓ 締切 + n
確定       依頼フォームは閉じる（申し出は担当教員へ）
```

締切と同時に**いつ確定するかを学習者に告げる**のが要点。告げずに確定させると、
確定したこと自体が事後にしか分からない。告げた以上、期限で締め切ってよい。

猶予は**コースごと**に教員が `/manage` で設定する（既定は「自動確定しない」。
設定しないと仮確定にもならず、依頼はいつでも受け付ける）。
設定しただけでは動かない。走らせるのはこのプロセス。

```fish
uv run aijudge-finalize --once                # 1 回走って終わる（cron 向き）
uv run aijudge-finalize                        # 常駐（既定 15 分ごと）
uv run aijudge-finalize --once --dry-run       # 何が確定するかだけ見る
```

cron に置くなら 1 時間ごとで十分（猶予は時間単位の話）。

```
17 * * * * cd /srv/aijudge && AIJUDGE_DATABASE_URL=... /usr/local/bin/uv run aijudge-finalize --once
```

**自動確定しないもの**は次の 3 つ。これらは `/manage` の未確定件数に残るので、
一括確定か待ち行列から個別に処理する。

- 未対応の再確認の依頼があるもの（学習者が人に見てほしいと言っている）
- `review_required` に振り分けられたもの（コンパイルエラー、合否境界の近傍など）
- 未採点の観点があるもの（誰も見ていない観点を成績に入れない）

**採点ワーカーには相乗りさせていない。** 確定はレビュー側の判断で、採点を
止めたいときに確定まで止まると困る。

### 一括確定（課題ごと・教員の操作）

`/manage` の課題一覧から、未確定分をまとめて確定できる。根拠説明が必須で、
**学習者にそのまま表示される**。自動確定と違い `review_required` も含める
（教員が書面で責任を取る操作なので）。未対応の依頼だけは確定しない。

### 既存の DB に入れるとき

マイグレーション機構はまだ無い（`--create-schema` が
`Base.metadata.create_all` を呼ぶだけ）。`create_all` は**新しい表は作るが、
既存の表に列を足さない**。ADR 0010 で `courses` に列が 1 つ増えているので、
既にデータのある DB では手で足す。

```sql
ALTER TABLE courses ADD COLUMN auto_finalize_after_hours DOUBLE PRECISION;
```

`finalizations` 表の方は `--create-schema` で作られる。**開発機でデータを
捨ててよいなら**、作り直す方が確実。

```fish
docker compose down -v; docker compose up -d
uv run aijudge-web --create-schema
```

### 仕掛け忘れに気づく

`/manage` の課題一覧に**未確定件数**と段階（仮確定中／期限経過）が出る。
**期限経過なのに未確定が残っていれば「要対応」**と出る。中身は次のどれか。

- 未対応の異議申立、要レビュー、採点失敗（＝一括確定か個別確認で処理する）
- `aijudge-finalize` が動いていない

**遅れ提出に注意。** 異議の窓は課題の締切から数えるので、締切 + n の直前に
採点された提出には数分しか残らない。遅れ提出を受け付けるなら猶予を長めに取る。

## 測定（Phase 1・任意）

```fish
uv run aijudge-eval --subject cs_intro_c
```

記録済みの観測を読むだけで、**採点は行わない**。`packages/analytics` と
`apps/evalrunner` を削除しても採点は動く（ADR 0007）。
