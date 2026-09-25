# 動かす

Phase 0 の構成は 3 つのプロセスに分かれる。**採点はレビューの前に、
レビューとは独立に走る**（ADR 0007）。

```
aijudge-web       学習者：提出と結果表示                    :8080
aijudge-worker    採点：キューを消費                        （常駐・段階ごとに立てる）
aijudge-relay     イベント：outbox を購読者へ流す            （常駐・1 本だけ）
aijudge-review    教員：確認して確定 + 科目・課題の管理      :8765
aijudge-finalize  成績：締切を過ぎた分を自動確定            （cron か常駐）
aijudge-admin     運用：学期の頭の一括操作（CLI）
```

教員コンソールの `/manage` にコース・課題・締切・受講の管理がある。
学期の頭の一括操作（90 名の登録、課題の一括取り込み）は `aijudge-admin`。
**新規利用者の作成は CLI だけ**にしてある（パスワードの配布が伴うので、
画面に平文を出さない）。**止めるのも CLI からできる**（#237）── 画面
（`/manage/users`）はテナント管理者専用で、サーバに入れる人はその権限の
外側にいる。

```fish
uv run aijudge-admin user disable --login <id>   # 削除ではない。記録は残る
```

## 開発機（macOS / 単独プロセス）

```fish
docker compose up -d                    # PostgreSQL（提出物はファイルシステム）
set -gx AIJUDGE_DATABASE_URL postgresql+psycopg://aijudge:aijudge@localhost:5432/aijudge

uv sync --extra dev
uv run alembic upgrade head             # スキーマを作る・更新する
uv run aijudge-worker
uv run aijudge-review
```

### スキーマの更新

**運用中の DB は `alembic upgrade head` で更新する。** `--create-schema`
（`create_all`）は**無いテーブルを作るだけで、既存テーブルに列を足さない**
ので、運用に入ったあとは使えない。列が永久に作られず、その表を読む全ての
クエリが落ちる（実際に起きた・#24）。

```fish
uv run alembic current                  # いまどの版か
uv run alembic upgrade head             # 未適用の版を順に当てる
uv run alembic history                  # 版の並び
```

模型（`packages/persistence/.../schema.py`）を変えたら、**必ず版を足す。**

```fish
uv run alembic revision --autogenerate -m "何を変えたか"
```

### 更新したら、プロセスを全部入れ替える

**ワーカーを忘れない。** Python は起動時に模型を読み込むので、Web とレビュー
だけ入れ替えて古いワーカーを残すと、**新しいコードが書いた採点結果を古い
ワーカーが読めない**という状態になる。実際に 2 度踏んだ。

- `session=0` の問題セット（#60）を古いワーカーが読めず、その回の提出が
  すべて「採点されていません」のまま溜まった
- 点の付いていない採点結果（#80）を古い AI ワーカーが拒み、決定的段階だけ
  終わって AI 段階が落ち続けた

どちらも**エラーはワーカーのログにしか出ない**ので、画面からは「採点が遅い」
としか見えない。模型（`packages/core`）を変えた更新では、次を全部入れ替える。

```fish
pkill -f 'aijudge-(web|review|worker|finalize)'
# それぞれ起動し直す（下の「開発機」の並び）
```

書き忘れは CI が落として教える（`packages/persistence/tests/test_migrations.py`
が、移行だけで作った形とモデルの形を突き合わせる）。

**既にテーブルがある DB を移行の管理下に入れる**ときは、作り直さずに
「適用済み」と刻む。

```fish
uv run alembic stamp head
```

`AIJUDGE_DATABASE_URL` を設定しない場合、既定は同じ PostgreSQL の URL。
SQLite でも動くが**行ロックが無い**ので、ワーカーは 1 プロセスだけにする
（`Database.supports_row_locking` が偽になり、CLI が警告する）。

## ログを読む（ADR 0016）

ログは 3 つあり、置き場も寿命も違う。**新しい記録を足すときは、まずどれかを決める。**

| | 何の記録か | 置き場 | 寿命 |
|---|---|---|---|
| 運用ログ | プロセスに何が起きたか | journald（開発機は stderr） | 90 日 |
| 監査ログ | **誰が**成績に関わる何をしたか | DB `audit_events` | 消さない |
| 採点記録 | どの版・どのモデルで採点したか | `GradingRun`（ADR 0003） | 消さない |

開発機の既定は人が読む形。運用は `AIJUDGE_LOG_FORMAT=json` にする。

```fish
AIJUDGE_LOG_FORMAT=json AIJUDGE_LOG_LEVEL=DEBUG uv run aijudge-worker --once
```

**1 つの提出について、web とワーカーの両方の行を集める。** 突き合わせの鍵は
`submission_id` ── これが上の #60 / #80 で欠けていたものである。

```fish
journalctl -t aijudge-web -t aijudge-worker-det -t aijudge-worker-ai1 -o cat \
  | jq 'select(.submission_id == "SUB-ID")'
```

1 リクエストの中で起きたことは `request_id` で引く。応答の `X-Request-ID`
ヘッダに同じ値が入るので、学生から報告された 1 件をそのまま辿れる。

**ログに載るのは識別子だけ。** 提出物の本文・氏名・LLM のプロンプトと出力は
載せない（P7）。journald はバックアップも暗号化も掛かっていない場所で、
そこへ学習者データを複製した時点でゲートウェイ側の保証（ADR 0004）が迂回される。
`aijudge_telemetry.bind` が値の型と長さを見て弾くので、うっかり本文を渡すと
その場で例外になる。

### 監査ログを読む

`audit_events` は DB にある。**運用ログと違って消さない** ── 成績への異議
申立ては学期が終わってから来る。DB ダンプに入るので restic の対象でもある。

```fish
# この提出に何が起きたか（新しい順）
psql aijudge -c "select at, action, actor_kind, actor_user_id, summary
                 from audit_events
                 where target_type='submission' and target_id='SUB-ID'
                 order by at desc"

# ログイン失敗を新しい順に（総当たりを見る）
psql aijudge -c "select at, target_id, source_ip, detail->>'reason'
                 from audit_events where action='login.failed' order by at desc limit 50"
```

**`actor_kind` を必ず見ること。** 3 つある。

- `user` … 認証済みの誰かがやった（`actor_user_id` がある）
- `system` … 人間の操作者がいない。締切経過による自動確定、`aijudge-admin`
  からの操作（CLI は認証された主体を持たない ── 誰が打ったかはサーバへの
  到達権限の側の問題である）
- `anonymous` … **認証されていない誰かが試みた。** ログイン失敗がこれで、
  `target_id` の口座は分かるが、やったのが本人とは限らない

`request_id` が入っているので、運用ログの同じ ID と突き合わせられる。

**平文は入らない。** パスワード、再発行した平文、API トークン、提出物の本文は
記録しない。`detail` は差分の前後の値のための欄で、上限（4000 文字）を超えると
保存時に弾かれる。

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

## 1 回のログインで両方に入る（#103）

セッションは**両アプリで同じ表**（`AuthService`）を使う。Cookie の名前も
`aijudge_session` に揃えてあるので、**同じホストで動かしていれば片方で
ログインするだけで両方が開く。**

役割はコースごとに決まる（同じ人が「A では学習者・B では教員」になる）。
一覧の行から相手側へ渡すリンクは、**開いているホスト名のまま、相手の
ポートへ**向く（#114）。1 台が `localhost`・IP・短い名前・FQDN のどれでも
応じる以上、「どの名前で来たか」は起動時には決まらない ── 決め打ちの名前へ
渡すと、その名前で開いていない人の Cookie が付いていかず、飛んだ先で
「ログインしてください」になる。

相手のポートが既定（8765 / 8080）でなければ、そこだけ教える。

```fish
set -gx AIJUDGE_CONSOLE_PORT 9765   # 学習者アプリが渡す先のポート
set -gx AIJUDGE_LEARNER_PORT 9080   # コンソールが渡す先のポート
```

**逆プロキシの後ろや、本当に別のホストに置いてある場合は URL で指定する。**
その場合、名前を知っているのは運用者のほうである。

```fish
set -gx AIJUDGE_CONSOLE_URL https://aijudge.example.jp/teach
set -gx AIJUDGE_LEARNER_URL https://aijudge.example.jp
```

**逆プロキシを前に立てるときは `AIJUDGE_ALLOWED_HOSTS` を設定する**（#116）。
`Host` と `X-Forwarded-*` はクライアントが決められる値で、相手側アプリへの
リンクはそこから組み立てている ── 素通しにすると、外部入力を
`X-Forwarded-*` に写す設定や共有キャッシュがある場合に、リンク先を
差し替えられる。

```fish
set -gx AIJUDGE_ALLOWED_HOSTS aijudge.example.jp,aijudge.example.jp:8765
```

**ホスト名が違えばセッションは共有されない**（Cookie はホスト単位。ポートは
無視されるので、同じホスト名ならポートが違っても共有される）。1 つの入口に
まとめる話は #103 の続き（逆プロキシ）で扱う。

## Google ログインを使えるようにする（#124・#125）

aiJudge 側の設定は `/manage/oidc-settings`（テナント管理者だけ）にある。
**その前に、機関の Google Cloud で OAuth クライアントを 1 つ発行しておく。**

### リダイレクト URI は 2 本いる

ここが一番踏みやすい段差である。**学習者アプリと教員コンソールは別の入口を
持ち、それぞれが自分の `/auth/callback` へ戻る。**

```
https://<学習者の入口>/auth/callback
https://<コンソールの入口>/auth/callback
```

**コンソールを接頭辞の下に置いている場合、その接頭辞も URI に入る**
（`AIJUDGE_CONSOLE_ROOT_PREFIX=/console` なら
`https://example.jp/console/auth/callback`）。1 台のホストに 2 つのアプリを
まとめた構成では、学習者側だけ登録して**コンソール側を登録し忘れる**。
学習者の画面では普通にログインできてしまうので、気づくのは教員が最初に
入ろうとしたときになる。

Google は登録した文字列と**完全一致**で判定する。

- `http` は受け付けない（`localhost` を除く）。逆プロキシで TLS を終端して
  いるなら、アプリが `X-Forwarded-Proto` を受け取れていること ── これが
  無いと `http://…/auth/callback` を送り、原因の見えない 400 になる
- 末尾スラッシュの有無、ホスト名の違い（`www` の有無・別名）はすべて別物

### 症状から原因へ

Google の 400 の画面は**「詳細」を開くと実際に受け取った値が出る**。
そこを見ないと切り分けられない。

| 詳細に出るもの | 原因 |
|---|---|
| `redirect_uri_mismatch`（受け取った URI つき） | その URI が承認済みリストに無い。**コンソール側の登録漏れが最有力** |
| `org_internal` | 同意画面が「内部」。組織外のアカウントは原理的に入れない |
| `access_denied` + テストユーザー | 公開ステータスが「テスト」のまま |

### いま何を送っているかを実機で確かめる

推測する前に、サーバ自身に組み立てさせて読む。

```fish
curl -s -o /dev/null -w '%{redirect_url}\n' \
    -H 'Host: <公開しているホスト名>' -H 'X-Forwarded-Proto: https' \
    http://127.0.0.1:8765/auth/login
```

`redirect_uri` の値がそのまま出る。**これを GCP の承認済み URI と 1 文字ずつ
突き合わせる**のが一番速い。学習者側は `:8080` に対して同じことをする。

### 受け付けるアカウント（#220）

**Google Workspace の組織アカウントだけが入れる。** ID トークンの `hd`
（組織の所属として Google が発行する主張）が許可ドメインに一致すること、かつ
`email_verified` が真であることを要求する。

- `hd` が無いトークンは通さない。**無いときにメールアドレスの後ろを見るのは、
  「その機関のドメインを名乗る」ことと区別がつかない** ── 個人の Google
  アカウントでも、確認さえ済めば任意のドメインのアドレスを持てる
- **Workspace を使っていない機関のメールドメインは、この経路では受け付け
  られない。** 運用の前提を狭める判断であり、そういう機関はローカル認証
  （`aijudge-admin staff`）を使うことになる

### アカウントの選択（#208）

認可リクエストには `prompt=select_account` が必ず付く。付けないと Google は
ブラウザに残っているセッションを黙って再利用し、**私物のアカウントで
サインイン済みの端末からは機関のアカウントを選べない**。

許可ドメインがちょうど 1 つのテナントでは `hd` も添える（選択の候補が
その組織に絞られる）。**`hd` はヒントであって検査ではない** ── URL を
書き換えれば外せるので、境界は突合後のドメイン検査の側にある。

### ボタンの文言（#209）

ログイン画面のボタンは `/manage/oidc-settings` で機関ごとに変えられる
（「全学認証アカウントでログイン」など）。空欄なら機関に依らない既定が出る。

## 配色（昼・夜）

画面の右下ではなく**フッタの左**に切り替えがある（自動 / 昼 / 夜）。
フッタは両アプリの全ページに必ずある唯一の場所で、ヘッダはログイン後にしか
出ない ── 眩しいのはログイン画面も同じである（#181）。

既定は**自動**（端末の設定に従う）。以前からの挙動がそのまま既定として残る。
選択はブラウザに保存されるので、**端末ごと・アプリごと**である（学習者アプリと
教員コンソールは別オリジンなので、それぞれ覚える）。サーバには保存しない ──
設定を持たせるほどの重みが無く、DB とバックアップに列が 1 つ増える。

JavaScript を切っていると切り替えは出ず、端末の設定に従う。

## 環境変数

| 変数 | 用途 | 既定 |
|---|---|---|
| `AIJUDGE_DATABASE_URL` | 接続先 | ローカル PostgreSQL |
| `AIJUDGE_ARTIFACT_DIR` | 提出物の置き場所 | `~/.aijudge/artifacts` |
| `AIJUDGE_VIDEO_DIR` | 動画の置き場所（提出物とは**別のディレクトリ**）。**未設定なら動画提出は 501 で断る** ── 設定した人だけが持つ機能である。**web・review・admin が同じ場所を指すこと**（消すのは admin）。**systemd で動かすなら unit の `ReadWritePaths` の中に置くこと**（下記） | 未設定（機能ごと無い） |
| `AIJUDGE_MAX_VIDEO_BYTES` / `AIJUDGE_MAX_CONCURRENT_VIDEO` | 動画 1 件の上限と、同時アップロードの数 | 5 GiB / 4 |
| `AIJUDGE_MAX_VIDEO_BYTES_WITHOUT_DEADLINE` | **締切の無い課題だけ**の上限（ADR 0020）。あちらは提出から 1 年残り、回ごとにまとめて消せないので小さく絞る | 256 MiB |
| `AIJUDGE_OBSERVATION_DIR` | 観測レコード（測定用・任意） | `~/.aijudge/observations` |
| `AIJUDGE_SANDBOX` | 隔離バックエンド（`auto`/`docker`/`gvisor`/`seatbelt`） | `auto` |
| `AIJUDGE_SANDBOX_WORKDIR` | 作業域の置き場所。コンテナがマウントするパスであること | `~/.aijudge/work` |
| `AIJUDGE_SECURE_COOKIES` | セッション Cookie に `Secure` を付ける（`1`/`0`）。未設定なら `X-Forwarded-Proto` で判断 | 未設定 |
| `AIJUDGE_CONSOLE_URL` | 学習者アプリが出す教員コンソールの場所（#103）。逆プロキシの後ろなど、相手が別ホストのときだけ指定する | 未設定（開いているホスト名 + `AIJUDGE_CONSOLE_PORT`） |
| `AIJUDGE_LEARNER_URL` | 教員コンソールが出す学習者アプリの場所（#103） | 未設定（開いているホスト名 + `AIJUDGE_LEARNER_PORT`） |
| `AIJUDGE_CONSOLE_PORT` / `AIJUDGE_LEARNER_PORT` | 相手のポート（URL 未設定のときに使う・#114） | `8765` / `8080` |
| `AIJUDGE_TIMEZONE` | 画面に出す日時と、締切の入力欄のタイムゾーン（IANA 名）。保存は常に UTC。未設定・不正なら `Asia/Tokyo` | `Asia/Tokyo` |
| `AIJUDGE_CONSOLE_ROOT_PREFIX` | コンソールを接頭辞の下に出す（`/console` など）。逆プロキシで 1 つのホストにまとめるときに使う。**Google の承認済みリダイレクト URI にもこの接頭辞が入る** | 未設定（ルート直下） |
| `AIJUDGE_ALLOWED_HOSTS` | 受け付ける `Host`（コンマ区切り・#116）。**逆プロキシを前に立てるなら設定する** | `*`（素通し） |
| `AIJUDGE_LLM_BASE_URL` / `AIJUDGE_LLM_MODEL` | ローカル LLM | — |
| `AIJUDGE_LLM_VISION_BASE_URL` / `AIJUDGE_LLM_VISION_MODEL` | **画像を読むモデル**（`image_text` 抽出器）。主系が vision を持たないときに、画像だけを別ホスト・別モデルへ回す。未設定なら通常の経路をそのまま使う ── そこが画像を読めなければ Gateway が断り、その観点は人へ回る | 未設定 / `qwen3-vl:8b` |
| `AIJUDGE_LLM_VISION_FALLBACK_BASE_URL` | 画像経路の**従系**（#341）。主系が応答しない間だけ回す。**従系も vision を持つこと** ── 持たなければ合成した側も vision を名乗らず、画像を渡す呼び出しは呼ぶ前に断られる（黙って本文だけで答えるよりはよい）。**名乗りまでしか見ない**ので、実際に読めるかは `deploy/aijudge-vision-check.sh` で確かめる | 未設定（従系なし） |
| `AIJUDGE_FEEDBACK_MODEL` | フィードバック生成のモデル。未設定なら要約に落ちる | — |
| `AIJUDGE_OIDC_SECRET_KEY` | Google OIDC 設定の `client_secret` を暗号化する鍵（#124）。`Fernet.generate_key()` の値。**Google ログインを使うなら必須**（未設定だと `/manage/oidc-settings` での保存が失敗する） | — |
| `AIJUDGE_LOG_FORMAT` | 運用ログの形（`json` / `text`）。**運用では `json`** ── 1 行 1 イベントで `jq` で絞れる | `text` |
| `AIJUDGE_LOG_LEVEL` | 運用ログの段（`DEBUG` / `INFO` / `WARNING` …） | `INFO` |
| `AIJUDGE_PROFILES_DIR` | 科目プロファイル（`*.yaml`）の置き場所。**運用では git のチェックアウトの外を指す** ── リポジトリの `subjects/` はサンプルで、デプロイのたびに入れ替わる（`subjects/README.md`）。**web・review・worker・admin のすべてが同じ場所を指すこと** | リポジトリの `subjects/` |
| `AIJUDGE_DEMO_COURSE` | お試しコースのコース ID（#194）。**設定した人だけが持つ機能** ── 未設定なら自動受講登録は 1 行も動かない。値は `aijudge-admin demo seed` が表示する ID を写す | 未設定（機能ごと無い） |
| `AIJUDGE_DEMO_INSTRUCTOR_PREFIX` | お試しコースで教員として登録する、外部 IdP の `login` の接頭辞。**このコースの中だけ**で効く。空は全員に一致するので既定に戻る | `a`（龍谷大学の教職員） |

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

### イベントのリレー（#328）

```fish
uv run aijudge-relay        # 常駐
uv run aijudge-relay --once # 空になるまで流して終わる（cron でも可）
```

**1 本だけ立てる。** 採点結果とイベントは同じトランザクションで書かれる
（outbox）ので失われないが、書いただけでは届かない ── 未送信を読んで購読者を
呼ぶ人が要る。いま購読しているのは習熟度（S7）で、**これが動いていないと
知識要素の習熟度は 1 件も更新されない。**

複数立てても壊れない（購読側は冪等で、同じ採点を二度受け取っても習熟度は
一度しか動かない）が、重複配信を平常にする理由が無い。

**止まっていても採点は完了する。** だから気づけない ── 実際、購読者は
2026-08-29 に書かれたのに登録する人がテストにしか居らず、運用では
`grading.completed` が 254 件、未送信のまま積み上がっていた（`skill_states`
は 0 件）。設定のドリフト検査（`aijudge-config-check`）がこのユニットの停止を
見るようにしてある。

繋いだ初回は溜まっていたぶんが一度に流れる。**推移が「その日に全部動いた」
形にはならない** ── `SkillPoint.recorded_at` はイベントの `occurred_at` を
使うので、記録は実際の提出時刻に沿って並ぶ。

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

### 並列度は 4 か所に散っている ── 一緒に動かす（#260・#261）

**運用機の並列度を決める値は 1 か所に無い。** どれか 1 つだけ上げると、混んだ
ときに接続が取れずに落ちる。**上げるときは DB を先に、下げるときは DB を後に。**

| 値 | 置き場所 | elite の現在 |
|---|---|---|
| 学習者アプリのプロセス数 | `AIJUDGE_WEB_WORKERS`（EnvironmentFile） | 2 |
| 教員コンソールのプロセス数 | 1 固定（`aijudge-review.service`） | 1 |
| 決定的ワーカー | `aijudge-worker-det.service` | 1 |
| AI ワーカー | `aijudge.target` の `Wants=`（@1〜@4） | 4 |
| 1 プロセスあたりの DB 接続 | `packages/persistence/.../engine.py` | `pool_size 10 + max_overflow 20` |
| DB の上限 | postgresql の `max_connections` | 300 |
| 待ち時間の目安に使う本数 | `AIJUDGE_AI_WORKERS`（EnvironmentFile） | 4 |

見積りは**プロセス数 × 30**（`pool_size + max_overflow`）。

```
web 2 + review 1 + det 1 + ai 4 + finalize 1 = 9 プロセス
9 × 30 = 270（最悪）   9 × 10 = 90（定常）
```

**`max_connections` はこの最悪値を上回っていること。** 定常値で決めると、
締切集中でちょうど足りなくなる ── 足りない瞬間は最も混んでいる瞬間である。

**`AIJUDGE_AI_WORKERS` は AI ワーカーの本数と揃えること。** これは学習者に
出す「あと何分」の計算にしか使わない値だが、**本数の 2 つ目の写し**である
── 4 本動いているのに 1 と伝えると、目安が 4 倍になる。採点は止まらないので、
黙ってずれ続ける。`aijudge-config-check` が両方を突き合わせて知らせる。

**教員コンソールは 1 プロセスのまま。** 操作の結果をプロセス内の変数で持って
いるので（`Console.last_*`）、複数にすると POST と続く GET が別のプロセスに
入り、**確定を押しても何も起きなかったように見える**ことがある（#260）。
直してから増やすこと。

### 書いた設定が機械に届いているか

**`deploy/systemd/` の変更は、デプロイのたびに配られる**（`install-units.sh`）。
以前はそうでなく、`bootstrap.sh` を走らせた最初の一度しか届いていなかった ──
2026-09-12 に測ったとき 11 個中 9 個がずれており、AI ワーカー 4 本の宣言も、
ログの名札も、systemd のサンドボックス化も、**書いてあるのに効いていなかった**。

疑わしいときは手で確かめる。

```fish
ssh <host> 'for f in /opt/aijudge/deploy/systemd/*; do n=(basename $f); cmp -s $f /etc/systemd/system/$n || echo "ずれ: $n"; done'
```

配り直しは root の oneshot に閉じてある（デプロイは aijudge ユーザで走り、
`/etc/systemd/system/` への書き込み権限を持たない）。

```fish
ssh <host> 'sudo systemctl start aijudge-units.service; journalctl -u aijudge-units -n 20'
```

GPU を使う科目と使わない科目でキューを分けたい場合は `--subject` で絞る。

```fish
uv run aijudge-worker --subject cs_network_python --name py1
```

### 手でデプロイする（#345）

**デプロイはタグで起きる。** 運用機の `aijudge-autodeploy.timer` が 5 分ごとに
origin の `v*` を見て、新しければ `deploy.sh` を呼ぶ。手で流す必要があるのは、
タグを待たずに入れ直したいときだけである。

**そのとき環境を先に読ませること。** unit は
`EnvironmentFile=/srv/aijudge/config/aijudge.env` を持つが、ssh から
スクリプトを直接叩くと誰もそれを読まない。

```fish
ssh <host> "sudo -u aijudge sh -c 'set -a; . /srv/aijudge/config/aijudge.env; set +a; exec /opt/aijudge/deploy/deploy.sh v1.2.3'"
```

読ませずに叩くと `deploy.sh` が冒頭で止まる（2026-09-20 に `AIJUDGE_DATABASE_URL`
が無いまま走り、alembic が既定値で接続して認証に失敗した。**落ちたから気づけた**
ので、落ちない場合に備えて先に断るようにした）。

### 設定の控えはバックアップの中に置かない（#344）

`/srv/aijudge/config/` を編集する前に控えを取るなら、**`/srv/aijudge` の外**
（`/root/aijudge-env-history/` など）に置く。理由は 2 つある。

- **バックアップが止まる。** restic は `aijudge` ユーザで走るので、控えを
  `root:root` で置くと読めず **exit 3** で終わる。スナップショット自体は
  保存されるので気づきにくいが、`set -e` によって後段の
  `restic forget --prune` に到達しない ── 世代整理が止まったままになる。
  2026-09-20 に 3 系統が同時に failed になったのはこれで、
  原因は控え 7 つのうち 1 つだけ所有者が違ったことだった
- **認証情報の写しが増える。** `/srv/aijudge/config/` はバックアップ対象なので、
  控えを置けばその数だけ全スナップショットに入る

日次の `aijudge-config-check` が、`aijudge` から読めないファイルを見つけたら
知らせる（状態が変わったときだけ送る）。

## 成績を閉じる

教員の待ち行列は**学習者が異議を申し立てた提出だけ**である（ADR 0009）。
依頼が出なかった提出は放っておくと未確定のまま残るので、閉じる導線が要る
（ADR 0010）。

### 自動確定（採点で仮確定 → n 分後に確定）

```
採点完了   仮確定。「MM/DD HH:MM に確定します」と学習者に示す。異議はここまで
  ↓ 採点 + n
確定       依頼フォームは閉じる（申し出は担当教員へ）
```

**数えるのは提出ごとの採点完了からで、課題の締切からではない。** 締切起点だと、
締切前に出した学習者は自分の点が確定するまで何日も待つことになり、その間は
再提出の判断材料が「暫定」のままになる。採点は提出直後に終わるので、そこから
n 分で閉じれば**締切前に確定し、締切前に出し直せる** ── 出し直しは確定に
妨げられず（`Task.accepts_submissions_at` は確定を見ない）、採用されるのは
最高得点の提出である。

**いつ確定するかを学習者に告げる**のが要点。告げずに確定させると、確定した
こと自体が事後にしか分からない。告げた以上、期限で締め切ってよい。

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

### 実施中に課題を直す

問題文の誤りに気づいたら、課題を開いて直す。**新しい版ができ、出題済みの版は
書き換わらない**（P8）── 過去の採点は自分の版を指したまま残る。

**採点材料は、それを使う観点の中にある**（#303）。課題の編集画面でルーブリック
の観点を開くと、その観点が指名した評価器の材料がそこに出る ── `code_test_runner`
なら入出力セット（テストケース）、`checklist_ai_judge` なら項目表。科目プロファイル
が宣言していなくても、**観点に割り当てた時点で欄が出る**（観点は課題ごとに自分の
評価器を持つ・ADR 0018）。0 件なら「その観点は判定できない」と画面が言う
（総点は伏せられる・ADR 0015）。提出の遵守のように材料を持たない評価器には出さない。

「テストケースを直す」「項目表を直す」で足す・直すと**新しい版になる**（P8）。
どちらも「この問題を保存して更新する」とは別の操作で、**別々に版が上がる**。
**TA は確認だけ**（読めないと「入力例 1 で落ちる」に答えられない。直すのは担当教員）。

どの観点も指名していない材料は「**使われていない入出力セット**」として画面の
末尾に出る ── 観点を宣言する前に作られた課題や、評価器を付け替えた課題で起きる。
持っているのに画面から消えるのを避けるためで、採点には使われていない。

### 直した課題をファイルに戻す（#363）

コンソールでの編集は**その場では DB にしか残らない**。課題の正本は講義
リポジトリ（private）の `<科目>/<年度>/assignments/` に置いてあるので、
直した日のうちに書き戻して PR を出す。

```fish
# 1. 運用機から書き出す（DB は変わらない。手元の講義リポジトリへ）
sudo -u aijudge sh -c 'set -a; . /srv/aijudge/config/aijudge.env; set +a; \
  exec /opt/aijudge/.venv/bin/aijudge-admin course export \
  --course <コース ID> --out /srv/aijudge/courses/<code>/2026/assignments --force'

# 2. 手元でずれが無いか確かめてから PR
uv run aijudge-admin course diff --file network/2026/assignments/course.yaml
```

**書き出せなかった課題があれば `export` は 1 を返して名前を挙げる。** 木が
不完全なまま正本になると、次に `course apply` を流した日にその課題が静かに
変わる。`diff` は一致で `0`、差で `1`、判定できないとき（定義が読めない・
まだ流していない）で `2` を返す ── **「判定できない」は通過ではない。**

**公開リポジトリのチェックアウトには書き出せない**（拒否する）。書き出すのは
問題文・テストケース・参照解答で、未公開の回も含む。

### 承認待ちは課題ではない（#321・ADR 0019）

AI が作った課題も、AI が書き直した課題も、**下書き**として溜まる
（`/manage/courses/{id}/drafts`）。**課題になるのは採用したとき**で、それまで
学習者にも一覧にも出ない。

- **採用の瞬間まで何でも直せる** ── 課題キー・題名・問題文・出題する問題セット。
  キーは採用したら変えられない（提出も採点の記録もこの鍵で繋がる）。
- **捨てるのは即時の削除**。理由は聞かない（記録しないため）。課題になって
  いないので、捨てても学習者には何も影響しない。
- 改訂の下書きはキーを変えられない（同じ課題の書き直し）。採用すると新しい版に
  なり、その時点で出題される。

**移行前の承認待ちは引き継がれない。** `task_versions` に残る `IN_REVIEW` の版は
一覧に出なくなるので、課題の画面から削除して入れ直す。

### 既存の課題を現在の基準に合わせて書き直す（#306）

既存の課題は**入出力しか見ない前提**で書かれていることが多い ── 仕様が曖昧、
例と本文が食い違う、読みやすさのような観点への配慮が無い。コースの共通
ルーブリックが「読みやすさ 0.3」と言っても、**問題文がそれを求めていなければ**
学習者には減点の理由が分からない。

課題の編集画面の「**AI に書き直させる**」で、いまの観点に合わせて問題文を
書き直し、問う知識要素を選び直す。

- **AI への指示を渡せる**（1 行 1 件・任意）。何を直してほしいかは、読んだ教員が
  いちばんよく知っている ── 観点との食い違いは機械的に見付かるが、「毎年ここで
  質問が来る」は教員しか知らない。**必須事項の列ではない**ので、外せない条件は
  「必ず」、希望は「できれば」と書く（作問の指示欄と同じ扱い）。空でも走る。
- **書き直すのは問題文と知識要素だけ。** 観点（ルーブリック）はコースが持って
  おり段階まで決まっている ── そこをモデルに書かせると、コースごとに決めた段階が
  課題ごとに割れる。入出力セットと参照解答も触らない（#305 の仕事）。
- **必ず承認待ちの版になる**（P5・ADR 0008）。承認するまで学習者にはいまの版が
  出続ける。
- **直すところが無ければ版を増やさない。** 差分の無い版を積むと、教員は中身の
  無い版を 1 件ずつ開いて確かめることになる。

未承認の一覧（`/manage/courses/{id}/drafts`）には、行ごとに**新規か改訂か**が
出る。改訂には**問題文の差分**と、**提出が何件あるか**が出る ── 出題済みの課題の
問題文を書き換えると、既に提出した学習者と後から提出する学習者で違う問題になる
（過去の採点は自分の版を指したまま残るので壊れない・P8）。

### 解答例からテストケースを作る（#305）

課題の編集画面で、`code_test_runner` を指名した観点を開くと**参照解答**が読める
（**教員だけ**。TA には出さない）。いままで画面のどこにも出ていなかったので、
門 1（参照解答が全ケースを通る）が落ちても何が通らないのか確かめられなかった。

段階は 3 つで、**どの段階でも版は上がらない**（上がるのは保存を押したとき）。

1. **AI に解答例を書かせる** ── いまの問題文から書いて欄に入れる。読んで直す。
2. **解答例からテストケースを提案** ── 入力だけをモデルに考えさせ、**期待出力は
   その解答例を実際に走らせて埋める。** モデルに期待出力を書かせない ── 誤りは
   「全員が落ちる」として現れ、原因は提出物の側に見える（決定的な結果は
   `conclusive` なので AI にも見直されない・P3）。
3. **採用したものだけ**が入出力セットに入る。印を付けなければ消える（P5）。

**走らせるにはサンドボックスが要る。** `AIJUDGE_SANDBOX` が無い環境では提案を
作れないと言う（黙ってモデルの書いた出力に落とさない・ADR 0006）。macOS は
colima を上げてから。

参照解答は入出力セットと**ひと組で保存する** ── 門 1 は両方を突き合わせる検査
なので、片方だけ先に保存できると、教員が意図していない組み合わせを検査する
ことになる。門にかけるのも**いま欄にあるもの**である。

**テストケースは後から付けられる。** 「テストケースを後から付ける」から生成
すると、いまの問題文で参照解答とテストケースを作り、正しさの観点をテスト実行に
戻した**新しい版**ができる。生成物なので承認待ちで、**承認するまで学習者には
1 つ前の承認済みが出続ける**。

**訂正しただけでは、既に出ている提出は古い版の基準のままである。** 課題の
編集画面に「いまの版で採点し直す（N 件）」が出る（対象があるときだけ）。
押したときだけ動き、**確定済みの提出は動かさない** ── 閉じた成績を機械が
開け直さない。過去の採点も消えず、新しい採点が終わると古い採点に
「置き換わった」印が付く。

**課題を作り直して同じ問題を 2 件にしない。** 同じ問題セットに同じ題名の課題が
複数あると、一覧にその旨が出る（別々の課題なので提出も採点も分かれる）。

### 課題を消す・取り下げる

知識要素と同じ区別を課題にも当てている。

    提出が 1 件も無い  削除できる（打ち間違いの後始末）
    提出がある         出題を取り下げる（学習者に出なくなる。記録は残る）

**採点結果は課題版を指している**（P8）。提出のある課題を消すと、その成績が
何の課題の点なのか辿れなくなり、積み上げた習熟度の出所も失われる。だから
「使わなくする」と「無かったことにする」を分けてある。取り下げは**取り消せる**。

取り下げた課題は学習者の一覧に出ず、URL を知っていても開けない。教員の一覧には
「出題を取り下げ済み」と印を付けて残る。

### お試しコース（#194・#197）

**設定した人だけが持つ機能である。** `AIJUDGE_DEMO_COURSE` にコース ID を置いた
ときだけ、ログインした人が自動でそのコースに受講登録される。提出も採点も本物と
同じに動くが、**学習履歴にも評価にも残らない**（`is_trial`・#197）。

```fish
# 1. 定義から作る（冪等。subjects/demo/course.yaml）
uv run aijudge-admin demo seed
# 2. 表示されたコース ID を環境変数に置く（運用なら aijudge.env に書いて再起動）
set -gx AIJUDGE_DEMO_COURSE crs_...
```

**定義は `$AIJUDGE_PROFILES_DIR/demo/course.yaml` から読む。** 運用では科目
プロファイルと同じ木の下に来ている必要がある ── リポジトリの `subjects/*.yaml`
だけを運用側へ写すと、この階層が落ちて `seed` も `reset` も定義が無いと言って
止まる。

**膨れ上がったら人が消す。** `demo reset` は提出・課題・受講登録を消して定義から
作り直す。消す操作なので既定では件数を出して確認を挟み、`--yes` が自動化の口だが、
**cron には載せない**（#194 の判断）── 規模はコンソールのコース一覧から見える。

**運用機で手で叩くときは、env ファイルを自分で読ませること。**
`/srv/aijudge/config/aijudge.env` は systemd の `EnvironmentFile=` が読むだけなので、
`sudo -u aijudge aijudge-admin ...` のような素のシェルには何も入らない。
`AIJUDGE_DEMO_COURSE が設定されていません` で止まるのはこれで、設定の不備ではない
（同じ理由で `AIJUDGE_DATABASE_URL` も落ちるため、別の DB を見かねない）。

```fish
ssh <運用機> 'sudo -u aijudge sh -c "set -a; . /srv/aijudge/config/aijudge.env; set +a; exec /opt/aijudge/.venv/bin/aijudge-admin demo reset"'
```

内側は `sh` なので POSIX 構文である（fish に `set -a` は無い）。

### シラバスから基本情報と知識要素の候補を作る

入口は 2 つに分かれている。**コースの基本情報**（左の帯「コースの基本情報」→
「基本情報を入力する」）と、**知識要素の候補**（知識要素 →「本文から候補を作る」）。どちらも
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

出てくるのは**登録済みの語彙から選ばれた候補**で、印を付けてまとめて採用すると
**このコースが使う範囲**に入る（語彙への登録ではない）。**新しいキーは提案されない**
── 語彙は骨格（`aijudge-admin kc seed`）で決まり、画面から増やす経路は無い。
増やせると同じ概念が別のキーで二重に登録され、Q-matrix と習熟度が割れる
（2026-09-13 決定）。モデルが一覧に無いキーを返したときは、理由を添えて除かれる。
候補に無いものは「名前空間から足す」から分野ごとに探して足す。

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

### ルーブリック（観点）をどこで決めるか

観点は 2 か所で決まり、**課題の宣言が勝つ**。

    課題の宣言（`TaskVersion.criteria`）… 個別に変えたい課題だけ
    コースの共通（`Course.rubric`）……… その科目に共通の観点
    組み込みの既定 ………………………… 正しさ（0.7）＋読みやすさ（0.3）

共通ルーブリックはコースの共通設定で決める。**新しい課題がこれを引き継ぐ**が、
既にある課題は変わらない ── 出題済みの採点基準を書き換えないため（P8）。
個別の課題は問題セットのページから直す。**直すと版が上がる**（過去の採点は
元の版のまま残る）。

段階は `名前 | 説明 | 割合` の行で書く。観点 1 つにつき 4 段、それぞれ名前・
説明・割合があるので、入力欄に分けると 1 画面に 12 個以上並ぶ。空にすると
4 段の既定（未達／一部／概ね／達成）が入る。**重みの合計は 1.0**（観点ごとの
重みが成績の配分そのもの）。

### コースごとの採点設定

`subjects/*.yaml` は**雛形**である。同じ雛形を複数のコースが使い、コースは
そこからの差分だけを持つ。

    実効設定 = 雛形（ファイル） ← コースの上書き（DB の `grading_overrides`）

上書きが空なら雛形そのもので、既存のコースは今までと同じ挙動になる。

**上書きはそのコースにしか効かない。** だから教員が画面から触ってよい
（共通設定 →「採点設定」）。ADR 0002 が避けたかったのは「1 人の
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

### 知識要素の骨格を入れる

**学期の前に 1 度。** 入れないと教員は知識要素を 1 件も足せない ──
分野と単位は骨格でしか作れず、木が空だと「既存の子としてのみ足せる」という
規則が教員を締め出す。

**運用では科目プロファイルと同じ場所に置く。** リポジトリの `subjects/kc/` は
サンプルで、デプロイ（`git checkout <tag>`）のたびに入れ替わる。

```fish
sudo -u aijudge mkdir -p /srv/aijudge/subjects/kc
sudo -u aijudge cp /opt/aijudge/subjects/kc/*.yaml /srv/aijudge/subjects/kc/
```

`reference/` は写さない ── 原典を読むためのもので、投入しない。

同梱しているのは 3 つ。**要るものだけ入れればよい**（入れていない名前空間を
プロファイルが宣言すると、そのコースで知識要素を扱えない）。

| 名前空間 | 中身 | 規模 |
|---|---|---|
| `cs` | CS2023（ACM/IEEE-CS/AAAI） | 987 件 |
| `math` | CUPM 2015（MAA）＋ 高等学校学習指導要領（平成30年告示）数学 | 939 件 |
| `physics` | 参照基準 物理学・天文学分野（2016）＋ FCI ＋ 同 物理基礎・物理 | 535 件 |

```fish
uv run aijudge-admin kc seed --namespace cs
uv run aijudge-admin kc seed --namespace math
uv run aijudge-admin kc seed --namespace physics
```

何度走らせても増えない。骨格ファイル（`$AIJUDGE_PROFILES_DIR/kc/<名前空間>.yaml`）を
直して足したら、もう一度走らせれば差分だけが入る。投入は監査に残る
（`profile.updated` / 対象 `kc_namespace` / 出典つき）。

**知識要素はテナントをまたいで共有される。** `knowledge_components` に
`tenant_id` は無く、語彙はデプロイに 1 つである ── 投入は機関ごとの操作では
なく、サーバの運用者が 1 度行う操作になる。**混ざるのは語彙だけで、習熟度は
混ざらない**（`skill_states` の主キーは `(tenant_id, learner_id, kc_id)`）。
2 つのテナントが同じ `math.calculus.integral.ftc` を参照するのは正常である。
そのため利用件数（課題数・コース数）も**テナントをまたいで**数える ──
自分のところだけ数えると、「ここでは誰も使っていない」が他機関の依存している
知識要素を引退させる理由になってしまう。詳しくは README の
「モジュールとテナント」。

画面から足せるのは**知識要素（第 3 階層）まで**で、分野と単位は骨格が決める。
足すときは近い既存 KC が分野・単位をまたいで提示される（`subjects/kc/README.md`）。

### 習熟度を見る（#328）

知識要素を付けた課題が採点されると、学習者ごとの習熟度が積み上がる。
コンソールの「習熟度」（コースの左の帯）で、コース全体の分布と推移、
受講者 1 人ぶんの内訳と根拠が見られる。**教員とテナント管理者だけ**で、
TA には出さない ── 採点は分担するが、習熟度は成績から積み上がる値である。

**学習者には出していない。** 予測の妥当性（次の課題の正誤を AUC ≥ 0.70 で
予測する、という受け入れ基準）はまだ測れていないので、確定した評価として
読まれる相手には出さない（#327 で段階を分けて検討中）。

**推移は記録のある範囲しか描けない。** `skill_points` は
`a1c4f70b28de` を当てた時点から積み上がり、**過去は埋められない**
── それ以前の採点は習熟度そのものには効いているが、推移としては残って
いない。画面は「記録は ○月○日 から」と出してそれを黙らない。

**習熟度はコースをまたいで積み上がる**（`skill_states` の主キーに
コースは無い）。コースの画面に出るのは、そのコースの受講者を、そのコースが
使う知識要素で切った断面にすぎない ── 値そのものには担当外の科目で得た
観測も入っている。根拠は担当コースの提出だけを開き、外から来たぶんは
件数だけ出す（他コースの課題名は成績に近い）。

### 学内からだけ受け付ける（#333）

一部の問題セットを**学内からの接続に限る**（試験のため）。設定は 2 段階。

**1. テナント管理者が範囲を決める**（`/manage/campus-networks`）。

**学内の端末でこの画面を開くこと。** 画面は「いまあなたが接続している
アドレス」と「この設定だとあなたは学内と判定されるか」を必ず出す ── 範囲は
人が調べて書き写す値で、書き写しの誤りは**試験当日に全員が提出できない**という
形でしか現れない。開いた画面に出るアドレスを登録するのがいちばん確実である。

**端末に割り当てられるアドレスをそのまま書かない。** 無線 LAN の内側
（`10.` で始まるものなど）と、ここに届くアドレスは、間に NAT があると別の
ものになる ── 実測でも `10.20.*` は 1 件も届いていなかった（2026-09-16）。

読めない行があると保存できない。黙って無視すると、抜けたことに気づかない
まま試験を迎える。変更は監査に残る（`campus_networks.updated`）。

**2. 担当教員が問題セットで切り替える**（問題セットの「受け付ける場所」）。
日程と同じでセット単位で決まり、その中の全課題に入る。

判定は 4 つに分かれる。**真偽値に畳んでいない。**

| 状態 | 意味 | 提出 |
|---|---|---|
| 学内 | 範囲の中から来た | 通す |
| 学外 | 範囲の外から来た | 断る |
| 未設定 | 範囲が 1 件も無い | **通す**（設定忘れで全員を止めない） |
| 判定できない | 範囲はあるが接続元が読めない | **断る**（制限が黙って抜けるほうが悪い） |

**接続元は `X-Forwarded-For` の右端**（逆プロキシが書いた値）で判定する
（`aijudge_telemetry.client_ip`）。左端はクライアントが自由に書けるので、
そこを見ると名乗るだけで通れる。**アプリを逆プロキシの外から直接叩けない
ようにしておくこと** ── 現構成では `127.0.0.1:8080` に閉じている。

**採点と確定には効かない。** 既に出された提出はそのまま扱われる。

### 試験を公開まで TA に見せない（ADR 0025）

公開前の問題セットは、**普段は TA にも見える**（質問対応と採点の準備のため・
#340・#102）。試験では、問題セットの画面の「公開前に見せる相手」で
**公開まで教員だけに見せる**に切り替える。

- 公開の時刻（`opens_at`）までは、TA の一覧（学生画面・コンソール）にも課題ページ
  にも出ず、TA は試しに提出できない。**教員が試しに出した提出も TA には見えない**
  （模範解答に近いので）
- **公開の時刻からは TA にも見える。** 試験監督をする TA は開始と同時に読める
- **公開の時刻が無いと効かない** ── 時刻の無い課題は最初から公開済みとして扱う。
  画面がそう警告するので、日程を先に入れる
- 変更は監査に残る（`task.updated`・`field=confidential`）

### 一部の学習者にだけ出す ── 出題先の名簿（ADR 0025）

追試・再試験は**別の問題セットとして作り**、出題先を名簿で絞る。同じ課題のまま
一部の学習者の日程だけ変える形は取らない（締切を学習者ごとに求めることになり、
遅延減点・自動確定・動画の保存期間のすべてに手が入るため）。本試験と追試の成績は
別の課題として残る。

1. **名簿を作る**（`/manage/courses/<id>/groups`、または下の API・CLI）。
   入れられるのは**そのコースの学習者だけ**。知らないアカウントが 1 つでも
   混じると**何も保存しない**（一部だけ登録されると、漏れた学生が追試を見られない
   まま気づかれない）。保存は常に**丸ごとの置き換え**で、追加・削除が画面に出る
2. **問題セットの「出題先」で名簿を選ぶ**。複数選べばどれかに入っている学習者に
   出る。何も選ばなければ受講者全員（従来どおり）

- 出題先の外の学習者には、一覧にも URL にも出ず、「未提出」とも数えない
- **TA と教員には名簿に関係なく見える**
- 名簿から外しても、その学生が既に出した提出と成績は本人に見える
- 出題先として使っている名簿は消せない（先に出題先から外す）
- 名簿の変更は `group.updated` / `group.deleted`、出題先の変更は `task.updated` に残る

**画面を使わずに登録する**（手元のスクリプトから。API トークンは教員のもの）:

```sh
T="Authorization: Bearer aij_..."
C=crs_...
# 名簿を丸ごと置き換える（無ければ作る）。同じ要求を何度流しても同じ結果
curl -X PUT -H "$T" -H 'Content-Type: application/json' \
  -d '{"members": ["y2400001", "y2400002"]}' "https://<host>/console/api/courses/$C/groups/前期追試"
# 問題セット（画面の URL と同じ鍵）の出題先を置き換える。空の配列で全員に戻す
curl -X PUT -H "$T" -H 'Content-Type: application/json' \
  -d '{"groups": ["前期追試"]}' "https://<host>/console/api/courses/$C/units/exam01r/audience"
curl -H "$T" "https://<host>/console/api/courses/$C/groups"          # 一覧
curl -X DELETE -H "$T" "https://<host>/console/api/courses/$C/groups/前期追試"
```

知らない login があると 422 で、`detail.unknown_logins` に並ぶ。

**運用機で直接流す**（操作者は `system` として監査に残る）:

```sh
aijudge-admin group set --course crs_... --name 前期追試 --members retake.txt  # 1 行 1 login
aijudge-admin unit audience --course crs_... --unit exam01r --group 前期追試
aijudge-admin unit audience --course crs_... --unit exam01r                  # 全員に戻す
aijudge-admin group list --course crs_...
```

### 動画置き場は unit の `ReadWritePaths` の中に

`deploy/systemd/*.service` は `ProtectSystem=strict` で、書けるのは
`ReadWritePaths=/srv/aijudge /var/lib/aijudge` だけである。`AIJUDGE_VIDEO_DIR`
を別ディスク（例: `/work/aijudge/video`）に置くなら、**web の unit に drop-in で
許可を足す**。unit 本体は deploy が配り直す（#261）ので、機械ごとのパスは
本体ではなく drop-in に書く:

```sh
sudo mkdir -p /etc/systemd/system/aijudge-web.service.d
printf '[Service]\nReadWritePaths=/work/aijudge\n' \
  | sudo tee /etc/systemd/system/aijudge-web.service.d/video.conf
sudo systemctl daemon-reload && sudo systemctl restart aijudge-web
```

書くのは **web だけ**である。review は動画を読むだけで、消す `video purge` は
admin CLI（サンドボックス外）で走る。

**これを忘れると web が起動しない。** 受け皿（下記）は起動時に `_incomplete/`
を作るので、v1.12.0 からは動画提出のときではなく**起動時に**
`Read-only file system` で落ちる。しかも uvicorn の親プロセスは生き残るので
`systemctl is-active` は `active` を返し、子だけが死に続けて nginx が 502 を
返す ── 2026-09-21 にこれで 4 時間止まった。起動時に `AIJUDGE_VIDEO_DIR` を
名指しした 1 行で落ちるようにしてあるので、`journalctl -u aijudge-web` の
末尾を見る。

### 切れても続きから送れる（#119）

学内 Wi-Fi 越しに数 GB を送る。**1 回の PUT だと、90% での切断が全部やり直し**
になり、試験のように時間窓が決まっている場面では「間に合わなかった」に直結する。

動画の提出は**分割して送る**。サーバが受け皿を 1 つ作り、クライアントは
8 MiB ずつ送る。切れたら「いま何バイト届いているか」を訊いて、その続きから
再開する ── 捨てるのは失敗した 1 分割だけである。

| | |
|---|---|
| 受け皿の場所 | `AIJUDGE_VIDEO_DIR/_incomplete/`。**動画ストアと同じ根の下** ── 確定が `os.replace` で済む（別の場所だと 3 GB のコピーが増える） |
| 期限 | 24 時間。**次に誰かが始めるとき**に古いものを消す（専用のタイマーを足すと、その停止に誰も気づかない） |
| 持ち主 | 受け皿に提出者を書いてある。id を知っていても、他人のものには書けない・見えない |
| 二度押し | 受け皿の id が冪等キー。確定を 2 回押しても提出は 1 つ |
| 中断からの再開 | 受け皿の id はブラウザに残る。ページを閉じても「続きから再開」が出る（**同じファイルを選び直す必要がある** ── `File` は再読込をまたげない） |

**受付の判定は確定の瞬間に行う。** 始めたときに開いていても、送っている間に
受付が終わることがある ── 提出が成立するのは確定の瞬間である。

1 発で送る経路（`POST /tasks/{id}/submit-video`）も残してある。JavaScript が
無い環境と、既存の連携のためである。**関門は 1 か所にまとめてあり**（`_video_gate`）、
どちらの経路も受付期間・学内限定・拡張子・上限の同じ判定を通る。

nginx では分割ぶんが通るだけの `client_max_body_size` と
`proxy_request_buffering off` が要る（`deploy/nginx/aijudge.conf.template` に
入れてある）。**全体の大きさ（数 GB）を通す必要は無い**のが、1 回の PUT で
送っていた頃との違いである。

### 動画の保存期間（ADR 0020）

**課題の締切から 6 ヶ月で消す。** 提出日でも成績確定でもない ── 同じ問題
セットの課題は締切が揃っているので、**回ごとにまとまって消える**。最も早い回
でもその学期の疑義より後になる（後期の 10 月締切が 4 月、疑義は 2〜3 月）。

消すのは人である。自動では走らない。

```fish
uv run aijudge-admin video purge                     # 下見（既定）。何件・何 GB か出す
uv run aijudge-admin video purge --course crs_...    # このコースだけ
uv run aijudge-admin video purge --apply             # 実際に消す
```

**置き場所は `AIJUDGE_VIDEO_DIR` から読む。** 提出物（`--artifacts`）とは
別のディレクトリなので、env ファイルを読み込ませずに叩くと、空の既定値を
見て「0 件」と言う。運用機では他のコマンドと同じ形で流すこと。

消えるのは**ファイルだけ**である。`Artifact` の行も採点結果も残るので、
その採点が何を見て付いたのかは読めるままになる（P8）。消した動画を開くと、
学習者にも教員にも「保存期間を過ぎたため消去されました」と出る ── 404 では
不具合と区別が付かない。

**締切の無い課題は提出から 1 年**である（砂場・自習用が該当する）。共通の起点
が無いので提出日から数え、回ごとにまとまっては消えない。長く置くぶん、
**受け付ける大きさを 256 MiB に絞ってある**（通常は 5 GiB・
`AIJUDGE_MAX_VIDEO_BYTES_WITHOUT_DEADLINE`）── 上限と期間は一対なので、
片方だけを変えないこと。長い録画を出させたい課題には締切を入れる。

**動画はバックアップしない**（2026-09-25 決定。restic が取るのは `/srv/aijudge` と
作業の記録だけ）。試験や演習の主な提出はブラウザのエディタと作業の記録に移り、動画は
主な提出物ではなくなった。本番が壊れると動画は戻らない前提で運用する。

### ブラウザ IDE を有効にする（2026-09-24）

runner（`aijudge-runner@1..4`）と受付終了時の自動提出（`aijudge-ide-close.timer`）は
`aijudge.target` に入っているので、デプロイで起動する。**運用機で 1 度だけ手で行う**
のは次の 2 つ（どちらもリポジトリの外のファイル）。

1. **作業の記録の置き場所。** `aijudge.env` に `AIJUDGE_ACTIVITY_DIR=/work/aijudge/activity`
   を足し、ディレクトリを `aijudge` の持ち物で作る。web は `/work/aijudge` に書ける
   （動画の drop-in）。コンソールは読むだけ。無いと作業の記録は 503 で断られるが、
   編集・実行・提出は止まらない
2. **エディタ本体を nginx から配る。** 雛形（`deploy/nginx/aijudge.conf.template`）の
   `location /static/vendor/` を、運用機の vhost に足して `nginx -t` のうえ再読み込みする。
   無くても動くが、Monaco（約 4.5 MB）を web のプロセスが圧縮せずに配ることになり、
   授業の始めに全員が一斉に開くと web が詰まる

restic の環境ファイルにも `AIJUDGE_ACTIVITY_DIR` を足すと、作業の記録もバックアップに
入る（入れなくても IDE は動く）。

有効にしたら、テスト用のコースで一巡確かめる（エディタで書く → 実行 → 提出 → 採点 →
コンソールの「作業の記録」）。IDE に問題が出たら、問題セットの「答え方」をファイル提出に
戻せばその場で従来どおりに戻る（エディタの課題でもファイルでの提出は使える）。

### IDE の作業の記録の保存期間（ADR 0023）

ブラウザ IDE の**作業の記録**（`AIJUDGE_ACTIVITY_DIR` の下のファイルと、DB の索引）と
**自動保存**（書きかけのコード）は、学習者のデータなので期限で消す。

- 作業の記録: **動画と同じ期間**。問題セットの締切から 6 ヶ月、無ければ IDE を開いた
  時刻から 1 年
- 自動保存: **受付終了から 1 ヶ月**（2026-09-24 決定）。終了時点の最新は自動提出で
  提出になっているので、残すのは複製である。受付終了の無い課題は最後の保存から 1 年

消すのは人である。自動では走らない（`video purge` と同じ判断）。

```fish
uv run aijudge-admin activity purge                     # 下見（既定）。何回分・何件か出す
uv run aijudge-admin activity purge --course crs_...    # このコースだけ
uv run aijudge-admin activity purge --apply             # 実際に消す
```

**置き場所は `AIJUDGE_ACTIVITY_DIR` から読む**（`--activity-dir` でも渡せる）。無いまま
流すと作業の記録は消さない ── 本体（ファイル）を消せないのに索引だけ消すと、誰にも
辿れないファイルが残る。自動保存は DB だけなので消える。消したことは監査ログに
`activity.purged` として残る。

提出の出どころの記録（`ide_submission_links`）は消さない。中身はコードではなく指紋で、
提出と同じだけ残る。

**バックアップとの整合（2026-09-24 決定）。** オフボックスの restic は月次を 6 本残す
（本番が壊れても過去 6 ヶ月には戻せる・`aijudge-restic-offbox.sh`）。作業の記録の本体も、
restic の環境ファイルに `AIJUDGE_ACTIVITY_DIR` を書けば一緒に取る。消したデータは
バックアップに最大 6 ヶ月残るので、学習者への告知はその合計で書いてある（作業の記録は
締切から最大 12 ヶ月、自動保存は受付終了から最大 7 ヶ月）。**オフボックスの本数を
増やすときは、告知も直すこと。**

### 習熟度を確かめる砂場（#328）

習熟度は「**知識要素の付いた課題に、学習者として提出し、採点が完了する**」まで
1 件も積まれない。学期の初めはどれも欠けているので、画面を開いても空である。
かといって本番のコースに試しの提出を入れると、精度と一致度の標本に混ざる。

そのための砂場を同梱してある。

```fish
# 1. 砂場用の科目プロファイルを運用の置き場所へ（測定を分ける鍵）
sudo -u aijudge cp /opt/aijudge/subjects/cs_sandbox_kc.yaml /srv/aijudge/subjects/

# 2. コースと課題（知識要素つき）を入れる
sudo -u aijudge sh -c 'set -a; . /srv/aijudge/config/aijudge.env; set +a; \
  exec /opt/aijudge/.venv/bin/aijudge-admin course apply \
  --file /opt/aijudge/subjects/sandbox/course.yaml'

# 3. 砂場用の学習者を作る（本物の学籍番号とまぎれない login にする）
sudo -u aijudge sh -c 'set -a; . /srv/aijudge/config/aijudge.env; set +a; \
  exec /opt/aijudge/.venv/bin/aijudge-admin staff \
  --login sandbox-a --name "砂場 A" --course <コース ID> --role learner'
```

あとはその利用者でログインして `.c` を数回出せば、リレーが拾って習熟度が積まれる。

**なぜ測定に混ざらないか。** 観測レコードは科目プロファイルごとのディレクトリに
貯まり、`aijudge-eval --subject <名前>` はその 1 つしか読まない
（`evalrunner.observations.iter_observations`）。砂場は `cs_sandbox_kc` という
別の名前を持つので、`cs_lang_c_intro` や `cs_network_python` の標本には 1 件も
入らない。**採点の中身は写し**なので、見える図は本物と同じ規則で描かれる。

**お試しコース（demo）では代わりにならない。** あちらへの提出は `is_trial` で、
習熟度からも意図的に除外されている（`skill_subscriber`・#108/#197）── 教員が
自分の課題を試した 1 件が学習者の記録を動かさないための仕組みで、迂回しない。

**教員が自分で出しても積まれない。** `is_trial` は「学習者でないか、お試し
コースか」で決まる（`Submission.is_trial`）。砂場でも**学習者として登録した
利用者**で提出すること。

用が済んだらコースごと消せる（提出があるので `course delete` は拒む ── 画面の
「このコースを削除する」も同じ）。残しておいても本番の数字には効かない。

### 既存の DB に入れるとき

**スキーマの更新は Alembic が持つ**（v0.10.0 から）。運用の DB では
`--create-schema` を使わない ── `Base.metadata.create_all` は**新しい表は作るが、
既存の表に列を足さない**ので、黙って半分だけ新しい DB ができる。

```fish
uv run alembic upgrade head
```

`deploy.sh` がタグの checkout の直後にこれを実行するので、**タグでデプロイする
限り手で走らせる必要はない**。手で上げるのは、デプロイ経路の外にある DB
（開発機・検証環境）を引き上げるときだけ。

#### Alembic より前の DB を引き上げる

以下は **v0.10.0 より前の DB を Alembic の管理下に入れるときだけ**必要な手順で、
いまの運用の手順ではない。`alembic upgrade head` が通る DB でこれを手で流すと、
Alembic の見ている状態と食い違う。

ADR 0010 で `courses` に列が 1 つ増えているので、既にデータのある DB では手で足す。

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
ALTER TABLE courses ADD COLUMN rubric JSONB;
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

**異議の窓は提出ごとに独立している。** 採点完了から数えるので、遅れて出した
提出にも同じだけの窓がある（締切起点だったころは、締切 + n の直前に採点された
提出に数分しか残らなかった）。

## 測定（Phase 1・任意）

```fish
uv run aijudge-eval --subject cs_lang_c_intro
```

記録済みの観測を読むだけで、**採点は行わない**。`packages/analytics` と
`apps/evalrunner` を削除しても採点は動く（ADR 0007）。
