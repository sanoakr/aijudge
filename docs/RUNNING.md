# 動かす

Phase 0 の構成は 3 つのプロセスに分かれる。**採点はレビューの前に、
レビューとは独立に走る**（ADR 0007）。

```
aijudge-web       学習者：提出と結果表示                    :8080
aijudge-worker    採点：キューを消費                        （常駐・段階ごとに立てる）
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

**採点は 2 段階で、キューが分かれている**（ADR 0011）。決定的評価は 0.5 秒、
AI 評価は 17 秒（実測）。同じワーカーに任せると、テスト実行が終わっている提出の
結果が前に並んだ他人の LLM 待ちの後ろで止まる。

**決定的専用を最低 1 本立てる。** これで学習者に返るまでが p95 0.8 秒になる。

```fish
uv run aijudge-worker --phase deterministic --name det1 &   # 速い段階（最低 1 本）
uv run aijudge-worker --phase ai --name ai1 &               # AI はあとから届く
uv run aijudge-worker --phase ai --name ai2 &
uv run aijudge-worker --phase ai --name ai3 &
uv run aijudge-worker --phase ai --name ai4 &
```

`--phase` を付けないワーカーは両方を取る。開発機ではそれで足りるが、**締切集中
では決定的評価が AI の後ろに並ぶ**ので運用では分ける。

実測（exam08、提出 496 件を 2 時間の一様到着、400 回試行）:

| | 平均 | p95 |
|---|---:|---:|
| 分割前・1 本 | 910 秒 | 1689 秒 |
| 分割前・4 本 | 18.2 秒 | 25.2 秒 |
| **決定的専用 1 本** | **0.5 秒** | **0.8 秒** |
| AI 専用 4 本（到着まで） | 17.6 秒 | 24.3 秒 |

複数立てるときは **PostgreSQL であること。**

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

科目ページ → 問題セットのページから、未確定分をまとめて確定できる。根拠説明が必須で、
**学習者にそのまま表示される**。自動確定と違い `review_required` も含める
（教員が書面で責任を取る操作なので）。未対応の依頼だけは確定しない。

### シラバスから基本情報と知識要素の候補を作る

入口は 2 つに分かれている。**コースの基本情報**（コース全体の設定 →「基本情報を
入力する」）と、**知識要素の候補**（知識要素 →「本文から候補を作る」）。どちらも
**本文を貼り付ける**か **PDF / DOCX / テキストを選ぶ**。

コース名と概要・到達目標は `courses` に保存する。**科目プロファイルには置かない** ──
あちらは採点の仕方の宣言（ADR 0002）で、コードと同じレビューを通す前提の設定である。
学期ごとに変わる事務データのためにブラウザから書ける口を開けると、1 人の操作で
全員の採点が止まる経路ができる。コースコードと学期は（テナント・コード・学期）で
コースの同一性を作っているので、この画面では変えられない。

URL は受け付けない ── 龍谷大学のシラバスは
JavaScript で描画されるページで、取得しても空の外枠しか返らない（実測
1815 バイト、`<title>acslb-client</title>` だけ）。取りに行かないので、
サーバが任意の URL を叩く経路も作らずに済む。

PDF の抽出は提出物の採点と同じもの（`aijudge_norm_document_text.text_of`）を
使い、Markdown に均して入力欄に入れる（見出しと「第 N 回」を整えるだけで中身は
書き換えない）。**スキャン画像の PDF は読めない**（文字が埋め込まれている必要がある）。
読めなければそう言って断る ── 黙って OCR に流すと、読み取り誤りがそのまま
候補になり、出所が分からなくなる。

出てくるのは**候補**で、教員が選んだものだけが体系に入る。規則は手で足すときと
同じ（名前空間・親の実在・第 1 階層は管理者のみ）。

シラバスを開くには deep link が使える。

```
https://syllabus.ws.ryukoku.ac.jp/acrsw/CSylNoSSO/CNoSSO.do?i=<管理番号>&n=<年度>
```

- `i` … シラバス管理番号。**履修登録コードとは別物**で、シラバス一覧の
  検索結果に出ている
- `n` … 年度（`2026` のような文字列）

例（プログラミング及び実習Ⅱ）:

```
https://syllabus.ws.ryukoku.ac.jp/acrsw/CSylNoSSO/CNoSSO.do?i=Y001009010&n=2026
```

出典: <https://hig3r.hatenadiary.com/entry/2023/03/13/220000>

### コースごとの採点設定

`subjects/*.yaml` は**雛形**である。同じ雛形を複数のコースが使い、コースは
そこからの差分だけを持つ。

    実効設定 = 雛形（ファイル） ← コースの上書き（DB の `grading_overrides`）

上書きが空なら雛形そのもので、既存のコースは今までと同じ挙動になる。

**上書きはそのコースにしか効かない。** だから教員が画面から触ってよい
（コース全体の設定 →「採点設定」）。ADR 0002 が避けたかったのは「1 人の
操作で全員の採点が止まる」ことで、それは雛形そのものを書き換えられる場合の
話である。雛形は読み取り専用のまま残る。

触れるのは言語・時間の上限・blind 抽出率・評価器の組み合わせ。評価器は
**インストール済みから選ぶ**（存在しない名前を書けると、その科目の採点が
恒久的に失敗する）。`kc_namespaces` は他のコースと共有する語彙の範囲なので
上書きできない。

保存時に起動時と同じ検査を通す。**それでも捕まらない誤りが 1 つある** ──
`language` の取り違えは設定として正しく、結果は「全員 0 点」で原因が提出側に
見える。「この設定で試す」がそれを拾う（そのコースの参照解答を 1 件走らせる。
**保存はしない**ので採点の履歴には残らない）。

新しい評価器が要るのは、判定の種類そのものが新しいときだけである（数学の
CAS 同値、物理の単位検査など）。`code_test_runner` は 1 つの評価器で言語を
選び、`rubric_ai_judge` は言語を知らないので、**コースを増やすたびに評価器を
作ることにはならない。**

### 既存の DB に入れるとき

マイグレーション機構はまだ無い（`--create-schema` が
`Base.metadata.create_all` を呼ぶだけ）。`create_all` は**新しい表は作るが、
既存の表に列を足さない**。ADR 0010 で `courses` に列が 1 つ増えているので、
既にデータのある DB では手で足す。

```sql
ALTER TABLE courses ADD COLUMN auto_finalize_after_hours DOUBLE PRECISION;
ALTER TABLE grading_jobs ADD COLUMN phase VARCHAR(32) DEFAULT 'deterministic';
```

猶予は**分**に変わった（「締切の 10 分後に確定」を表せるようにするため）。
列を足して換算し、古い列を落とす。

```sql
ALTER TABLE courses ADD COLUMN auto_finalize_after_minutes INTEGER;
UPDATE courses SET auto_finalize_after_minutes = ROUND(auto_finalize_after_hours * 60)
  WHERE auto_finalize_after_hours IS NOT NULL;
ALTER TABLE courses DROP COLUMN auto_finalize_after_hours;
```

提出できるファイル形式を科目が持つようになったので、その列も足す
（NULL なら組み込みの既定）。

```sql
ALTER TABLE courses ADD COLUMN upload_suffixes JSONB;
ALTER TABLE courses ADD COLUMN description TEXT;
ALTER TABLE courses ADD COLUMN grading_overrides JSONB;
```

課題側で増えた項目（提出開始・課題ごとの猶予・課題ごとの提出形式・
課題キーの記録）は `tasks` / `task_versions` の JSON の中なので、
**列の追加は要らない**。古い行は既定値（空・NULL）として読める。

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
