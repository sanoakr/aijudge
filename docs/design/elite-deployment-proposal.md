# elite へのデプロイ方針（提案）

対象ホスト: `elite.math.ryukoku.ac.jp`（数理・情報科学課程の共用アプリサーバ）
学生/教員 UI のホスト名: `judge.math.ryukoku.ac.jp`（elite を指す。TLS 証明書取得済み・自動更新設定済み 2026-09-04）
状態: **設計は確定済み。§4 の作業は AI ワーカー稼働・deploy 材配置・CD 自動化
（autodeploy timer）・restic target 1 稼働まで完了。
残るは restic target 2・初回データ投入(#10)・§7 の判断待ち 2 件（2026-09-06）**

## 決定事項（2026-09-04）

- **ホスト名**: `judge.math.ryukoku.ac.jp` 一本。443=学生 UI、8443=教員コンソール。
  8443 は大学ファイアウォールで閉じているため学外からは到達不可＝事実上の学内限定
  （学外の教員は VPN 経由）。**教員コンソールに nginx の IP 制限はかけない。**
- **TLS**: `elite.math.ryukoku.ac.jp`（既存）と `judge.math.ryukoku.ac.jp`（新規・webroot・ECDSA）
  の Let's Encrypt 証明書を取得済み。`certbot.timer` で自動更新、更新後 nginx reload の
  deploy hook 設置済み。`certbot renew --dry-run` 両方成功。
- **ユーザ / グループ**: 実行 owner は `aijudge` ユーザ。`aijudge` グループに `sano` も入れ、
  デプロイ・起動停止を sano も行えるようにする。`aijudge` と `sano` の両方を `docker`
  グループに入れる（サンドボックスに必須）。詳細は §3.2。
- **LLM モデル**: **`gemma4:e4b` に確定（2026-09-05）。** `docs/design/01_モデル評価実験計画.md`
  〜`03_モデル評価_具体例.md` の実測（cs_intro_c の合成優劣サンプルでの判別性、
  `report_ja` での採点者間一致、速度・頑健性・コスト）に基づく。コードの既定値と
  一致するため、`AIJUDGE_LLM_MODEL` は**明示的に**この値をピン留めする
  （将来コード側の既定が変わっても本番の挙動を変えないため）。
  Bedrock 経由の候補（Sonnet 5 / GPT-5.6 / gpt-oss 等）は判別性で現行既定を
  上回らず、かつ学習者データを学外へ出す（P7 違反）ため不採用。

---

## 1. elite の現況（実地調査 2026-09-03）

| 項目 | 値 |
|---|---|
| OS | Ubuntu 26.04.1 LTS / kernel 7.0 / x86_64 |
| CPU・メモリ | 16 コア / 30 GiB |
| ディスク | `/` に 259 GB 空き（`/dev/nvme0n1p5`, 350 GB） |
| GPU | NVIDIA RTX 3070（8 GB VRAM） |
| ログインユーザ | `sano`（**パスワードなし sudo 可**）。`docker` グループ非所属 |
| Docker | 29.1.3 稼働。`runsc`（**gVisor**）導入済み。cgroup v2 |
| Python / uv | system Python 3.14 / `uv` 0.6.16（`~/.local/bin/uv`） |
| PostgreSQL | **未導入**（apt に `postgresql` あり） |
| nginx | 1.28.3 稼働。`elite.math.ryukoku.ac.jp` の 1 vhost のみ |
| TLS | certbot（authenticator=nginx）。`/etc/letsencrypt/live/elite.math.ryukoku.ac.jp/`。HSTS は `snippets/security-headers.conf` |
| 既存の同居アプリ | `/`（静的 "Slab demos"）, `/pdf2xlsx/`・`/strawscan/`（Flask+uWSGI）, `/meeting-scheduler/`（Next.js, pm2, :3000） |
| LLM 到達性 | `http://slab-llm.math.ryukoku.ac.jp:11434` に 12 ms / 200。モデル `qwen3.8:27b-mlx` 提供中（`slab-llm` 短名は elite では名前解決不可） |
| systemd linger | 無効 |

**評価:** 容量・隔離基盤（gVisor + cgroup v2）ともに aiJudge の要件を満たす。
不足は PostgreSQL の導入と、学生 UI 用のホスト名・証明書だけ。

---

## 2. 動かすもの（`docs/RUNNING.md` 準拠）

| プロセス | ポート | 役割 |
|---|---|---|
| `aijudge-web` | 8080 | 学習者 UI |
| `aijudge-review` | 8765 | 教員コンソール + `/manage` |
| `aijudge-worker --phase deterministic` ×1 | — | テスト実行（サンドボックス） |
| `aijudge-worker --phase ai` ×4 | — | LLM 評価 |
| `aijudge-finalize --once` | — | 締切超過分の確定（systemd timer, 1h ごと） |
| `aijudge-admin` | — | 学期頭の一括操作・利用者作成（CLI のみ） |
| PostgreSQL | local socket | 提出・成績・キュー。**行ロックが要る**ので SQLite 不可（ワーカー複数） |

提出ファイルの実体は MinIO ではなくローカルディレクトリ（`AIJUDGE_ARTIFACT_DIR`）で足りる。

ワーカー本数は RUNNING.md の実測（受講 91 名同時提出で「30 秒以内」に AI 4 本必要、
決定的専用 1 本で p95 0.8 秒）に基づく既定値。受講規模が判明したら調整（§7-5）。

---

## 3. 推奨構成

### 3.1 コードの置き場所とデプロイ方式 — **ネイティブ uv + systemd**

- リポジトリは private。**読み取り専用 deploy key** で `/opt/aijudge` に clone。
- `uv sync --frozen` で環境構築（uv は導入済み）。
- **タグ単位でデプロイ**（`[[release-tagging]]` の運用: ルート `pyproject` の version を上げて `v<version>` タグ）。`main` の HEAD は追わない。
- デプロイスクリプト `deploy.sh`:
  ```fish
  cd /opt/aijudge
  git fetch --tags
  git checkout <tag>
  uv sync --frozen
  uv run alembic upgrade head          # ← --create-schema は使わない（RUNNING.md #24）
  sudo systemctl restart aijudge.target # ← web/review/worker を全部入れ替える（#60/#80）
  ```
- 将来 GitHub Actions からの SSH デプロイに載せ替え可能。まずは手動 + タグ。

> コンテナ化しない理由: aiJudge は「1 リポジトリ・1 デプロイ単位のモジュラモノリス」で、
> RUNNING.md も全面的にネイティブ `uv run` を前提に書かれている。加えてワーカーは
> サンドボックスのために結局ホストの Docker を呼ぶ（docker-out-of-docker）ので、
> アプリだけコンテナに入れると隔離設計と二重管理になる。

### 3.2 ユーザ / グループ（2026-09-04 確定）

- **`aijudge` ユーザ**（system, `nologin`, home `/var/lib/aijudge`）— 全 unit・ワーカーの実行 owner（`User=aijudge Group=aijudge`）、ファイル所有者。
- **`aijudge` グループ** — `aijudge` と `sano` が所属。`/opt/aijudge` と `/var/lib/aijudge` を
  setgid（`2775` / データは `2770`）にして、`sano` も `sudo` なしで `git pull` / `uv sync` /
  デプロイ操作ができるようにする。
- **`docker` グループ** — `aijudge` と `sano` の両方を追加。gVisor 経路も実体は
  `docker run --runtime=runsc`（`packages/sandbox/.../selection.py`）で docker socket が要る。
  docker グループは当該アカウントにとって root 相当だが、`sano` は既に passwordless sudo を
  持つため実効権限は変わらない。管理操作（docker, systemd）は `aijudge` と `sano` が共同で行う。
- **polkit ルール**（`/etc/polkit-1/rules.d/49-aijudge.rules`）— `aijudge` グループのメンバーは
  `aijudge-*.service` / `aijudge.target` の start/stop/restart をパスワードなしで実行可。
  これで `sano` は `sudo` なしにサーバ・ワーカーを起動停止できる。
- ディレクトリ配置は §3.2.1（物理ディスクにマップ）。`/etc/aijudge/aijudge.env` は `root:aijudge` 0640。

### 3.2.1 ディスク構成とデータ配置（2026-09-04 実地調査）

elite には使えるファイルシステムが 3 つある。

| デバイス | 種別 | 容量 / 空き | 現在のマウント | 性質 |
|---|---|---|---|---|
| `nvme0n1p5` | NVMe SSD（KIOXIA） | 356 G / **259 G 空き** | `/` | 最速。OS と他アプリ（meeting-scheduler 等）と共用。Windows とデュアルブート（同ディスク・触らない） |
| `sda1` | SATA SSD（Samsung 860 EVO） | **466 G / 空** | **`/srv/aijudge`（2026-09-04 マウント済み）** | LABEL=`aijudge`、UUID `114c6dc9-6e3f-4b22-b5e9-ca33c3fe877a`。fstab: `defaults,noatime,nofail 0 2` |
| `sdb1` | HDD（WD30EZRZ, 5400rpm/SMR） | 2.7 T / **2.6 T 空き** | `/work`（既存 `archive/` 35 G あり） | 大容量・安価。**大きな順次読み書き向き**、小ファイル多数の常時ランダム I/O は苦手 |

**システム / データの分離原則:** `/`（NVMe）＝**システム**（OS・`/opt/aijudge` のコード・PG エンジン・`/etc` の設定）。
すべて git ＋ 短い runbook から再生成できる。`/srv/aijudge`（sda1）＝**データ**（提出物・観測・git に無い設定・DB ダンプ）。
これ 1 ディスク＋ git タグで系全体を別 PC に復元できる（§3.9）。`/work/aijudge`（HDD）＝バックアップ出力と将来の動画（別クラス）。

| 用途 | 置き場所 | 区分 | 理由 |
|---|---|---|---|
| コード `/opt/aijudge` | `/`（NVMe） | システム | git から再生成。デプロイで作り直す |
| **PostgreSQL 実データ `pgdata`** | `/`（NVMe、配布既定 `/var/lib/postgresql/…`） | システム（DR はダンプから） | 生 `pgdata` は PG バージョン依存で非可搬。NVMe が速い。復元は必ず `pg_restore` |
| **DB ダンプ** `/srv/aijudge/backups/db/*.dump` | `sda1`（SSD） | **データ** | `pg_dump -Fc` 夜次。これが DB の復旧点 |
| **DB WAL アーカイブ**（任意）`/srv/aijudge/backups/wal/` | `sda1`（SSD） | **データ** | 有効化すれば PITR で無損失復旧 |
| **観測レコード** `/srv/aijudge/observations` | `sda1`（SSD） | **データ** | 学生の作業を含む・κ 測定の根拠（ADR 0005/0007） |
| **Artifact ストア** `/srv/aijudge/artifacts`（テキスト＋PDF＋画像） | `sda1`（SSD） | **データ** | KB〜数十 MB。教員レビューで即読み。キーは相対パス＝可搬 |
| **git に無い設定** `/srv/aijudge/config/aijudge.env` | `sda1`（SSD） | **データ** | DB 資格情報・LLM URL・ALLOWED_HOSTS。systemd `EnvironmentFile` はここを指す |
| 動画 Artifact（将来・学生あたり数 GB） | `/work/aijudge/video`（HDD） | データ（別クラス） | 数百 GB〜TB。順次ストリーム。**routine バックアップ対象外**、保持ポリシー |
| サンドボックス作業域 `AIJUDGE_SANDBOX_WORKDIR` | `/var/lib/aijudge/work`（`/` NVMe） | 揮発 | ビルド・実行の一時 I/O。バックアップ不要 |
| restic リポジトリ（ローカル）／動画の一時アップロード先 | `/work/aijudge/{restic,tmp}`（HDD） | 揮発／中継 | tmp は動画と同一 FS に置き `rename` を atomic に |

**`/srv/aijudge` の構成（すべて `aijudge:aijudge`、作成済み）:**
```
/srv/aijudge/          2770
├── artifacts/         2770   AIJUDGE_ARTIFACT_DIR
├── observations/      2770   AIJUDGE_OBSERVATION_DIR
├── config/            2750   aijudge.env（0640 root:aijudge・後で設置）
└── backups/
    ├── db/            2770   pg_dump -Fc 出力
    └── wal/           2770   （任意）WAL アーカイブ
```
`pgdata` を `/srv/aijudge` に置かないので `postgres` ユーザの traverse 問題が無く、`/srv/aijudge` を丸ごと 2770 にできる。

**Artifact ストアの型別分割について（要注意）:**
`FilesystemArtifactStore` は **単一ルート**で、キーは `{tenant}/{submission}/{artifact}/{filename}`
（型・MIME でのディレクトリ分割は無い。`packages/submission/.../protocols.py::artifact_storage_key`）。
「PDF・画像とテキストを別デバイスに分ける」をアプリ設定だけで実現する口は**無い**。選択肢:

- **(A) 当面は単一ルート**：`AIJUDGE_ARTIFACT_DIR=/srv/aijudge/artifacts`（SSD）。現状の提出は
  コード・テキスト・PDF・画像だけで量も小さい。`/work/aijudge` は動画機能が入るまで予約。コード変更ゼロ。
- **(B) 単一ルートを HDD に**：`AIJUDGE_ARTIFACT_DIR=/work/aijudge/artifacts`、観測は SSD。増加に強いが PDF/画像表示が HDD 速度。
- **(C) 型別ルート（小 PR）**：`FilesystemArtifactStore` に `{ArtifactKind: root}` のマップを持たせ、
  `put`/`get` 時に既知の `ArtifactKind`（拡張子から決まる）でルートを選ぶ。テキスト→SSD、
  PDF/画像→SSD or HDD、動画→HDD。`storage_key`（DB 保存値）は不変で、変わるのはパス解決だけ。
  既存ファイルの一度きりの移設が要る。→ **動画対応 PR と同時にやるのが自然**（§R7）。

推奨: **(A) で開始**し、動画対応と一緒に **(C)** へ。

### 3.3 PostgreSQL

```fish
sudo apt install postgresql
sudo -u postgres createuser aijudge
sudo -u postgres createdb -O aijudge aijudge
```
- TCP では公開しない。unix socket + scram/peer 認証。
- `AIJUDGE_DATABASE_URL=postgresql+psycopg://aijudge@/aijudge?host=/var/run/postgresql`
- スキーマは初回・毎デプロイとも `uv run alembic upgrade head`。
- **`pgdata` は配布既定（`/`・NVMe）のまま**。生ディレクトリはバックアップせず、`pg_dump -Fc` を
  `/srv/aijudge/backups/db/` へ夜次（systemd timer）。無損失が要るなら WAL アーカイブを
  `/srv/aijudge/backups/wal/` に有効化 → PITR 可。採点メタデータ主体で数十万提出でも数 GB。
- DR では新 PG に `createdb` → `pg_restore`（→ 必要なら WAL 再生）。詳細は §3.9。

### 3.4 サンドボックス（ADR 0006）

- gVisor 導入済みなので `AIJUDGE_SANDBOX=auto` で `docker:runsc`（`KERNEL_ISOLATED`）が選ばれる想定。
- **実提出を通す前に**、コンテナテストが **skip ではなく pass** することを確認:
  ```fish
  AIJUDGE_SANDBOX=docker uv run pytest packages/sandbox/tests/test_container.py -v
  uv run python -c "from aijudge_sandbox import build_sandbox; s=build_sandbox(); print(s.name, s.isolation.value)"
  ```
- 作業域は `/srv/aijudge/work`（SSD）。macOS の `/var/folders` 問題は Linux では起きない。

### 3.5 LLM（P7）

**2026-09-06 変更: slab-llm（学外 Mac、リモート）から elite ローカルの PAIR クラスタへ切替。**

- `AIJUDGE_LLM_BASE_URL=http://localhost:11435`（elite 上、NVIDIA PAIR＝Personal AI Router の
  `ollama-proxy`。`/opt/PAIR/resources/cli-bin/ollama-proxy --port 11435` が sano セッションで常駐）。
  **bare の ollama（`localhost:11434`）ではない** — こちらは `gemma4:e2b`（5.1B）しか持たず、
  評価で確定した `gemma4:e4b` を提供するのは PAIR プロキシ側（11435）。
- `AIJUDGE_LLM_MODEL=gemma4:e4b` は変更なし（**確定（2026-09-05）**、
  `docs/design/01_`〜`03_モデル評価_具体例.md` の実測に基づく。Bedrock 経由の候補
  （Sonnet 5・GPT-5.6・gpt-oss 系）を含めて判別性・採点者間一致・頑健性を比較した上での結論）。
- **なぜ切替えたか**: PAIR は elite 単体（RTX 3070・8 GB VRAM）ではなく、
  `elite` / `envy1` / `envy2` / `euterpe` / `mnemosyne.math.ryukoku.ac.jp` / `taf15` の
  6 ノードクラスタ（`~/.config/Nvidia Corporation/Personal AI Router/cluster/members.json`
  で確認、全ノード `133.83.8x.x` = 学内サブネット）で GPU を束ねて `gemma4:e4b` を提供する。
  単一リモートホスト（slab-llm、Mac 1 台）依存の R3 リスクが解消し、
  AI ワーカー複数本（既定 4 本）を立てても輻輳しにくい（本人談: 「4 本でも無理のない
  クラスタ規模」）。
- 全ノードが学内サブネット（`133.83.x.x`）なので、slab-llm 同様
  「学習者データはローカルモデルのみ」（P7）を満たす。
- elite の RTX 3070 自体は PAIR クラスタの一員として引き続き使われる（未使用ではなくなった）。
- 2026-09-06、elite の `AIJUDGE_LLM_BASE_URL` を書き換え、
  `aijudge-worker-ai@1` を再起動して反映済み（`/proc/<pid>/environ` で確認）。
- **可用性（R8、2026-09-06 確認）**: `ollama-proxy` は `sano` の対話セッションの
  直接の子ではなく、`nvpair-tui.service`（systemd `--user`、`enabled`・`Restart=on-failure`）
  が張る tmux セッション → `nvpair-tui` → `nvpair-ui-broker` の子プロセス。
  `loginctl show-user sano` で `Linger=yes` も確認済みで、sano のログアウト・
  elite 再起動のどちらにも耐える構成になっている。
  残る未確認点は `ollama-proxy` 単体がクラッシュした場合に `nvpair-ui-broker` が
  再起動するかのみ（本番稼働中に落とす検証は避けた。保守時間帯に確認すること）。
- **slab-llm 側の常駐設定（2026-09-05 変更）**: 従来は `qwen3.8:27b-mlx` を
  launchd（`org.ollama.pin-qwen38`、4 分毎に `keep_alive:-1` で再ピン）で
  常駐させ、`blume` の Ask AI 等のコールドロード回避に使っていた。
  `AIJUDGE_LLM_MODEL` 確定に伴いこれを無効化し、`gemma4:e4b` を同方式で
  常駐に切替え（`org.ollama.pin-gemma4`、`/Users/sano/.local/bin/` 配下）。
  **`blume` の Ask AI は以後コールドロード（実測 ~11 秒/回）に戻る**
  ── 承知の上での判断。無効化した旧設定は
  `~/Library/LaunchAgents/disabled/org.ollama.pin-qwen38.plist` に保管。
  **残作業**: 新ジョブは SSH 経由では launchd の GUI 監査セッション制約で
  `bootstrap`/`load` できず、スクリプトを直接実行して即時ロードのみ済ませた
  （`expires_at` 確認済み）。**slab-llm のコンソールで一度だけ**
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.ollama.pin-gemma4.plist`
  を実行すると、4 分毎の再ピンと次回ログイン時の自動起動が有効になる
  （実行するまでは、何かが gemma4 を明示的にアンロードすると再ロードされない）。

### 3.6 リバースプロキシ / TLS

学生 UI（8080）と教員 UI（8765）の **2 つ**を TLS で出す。既存 vhost の `/` は静的サイト
かつ aiJudge のアプリは**サブパス動作（root_path）に未対応**なので、専用サブドメイン
`judge.math.ryukoku.ac.jp` を使う（作成済み・証明書取得済み）。

```
https://judge.math.ryukoku.ac.jp/       (443)  → 127.0.0.1:8080  学生
https://judge.math.ryukoku.ac.jp:8443/  (8443) → 127.0.0.1:8765  教員（学内のみ／FW で 8443 閉）
```

- 同一ホスト名なので **1 回のログインで両方に入れる**（RUNNING.md #103。Cookie はホスト単位、ポートは無視される）。
- 教員コンソールに nginx の IP 制限はかけない（FW が事実上の学内制限）。
- 新規 vhost ファイル `/etc/nginx/sites-available/judge.math.ryukoku.ac.jp`。
  証明書は取得済み（`/etc/letsencrypt/live/judge.math.ryukoku.ac.jp/`）。
- `conf.d/tls.conf` と `snippets/security-headers.conf` を再利用。
- 結果は SSE で段階配信されるので、該当 location に `proxy_buffering off;` と長め（600s）の `proxy_read_timeout`。
- `client_max_body_size 25M;`（レポート PDF 等）。
- **8443 の ssl server ブロックにも** `ssl_certificate` を明示（同じ lineage を使う）。
- アプリ側 env:
  ```
  AIJUDGE_SECURE_COOKIES=1
  AIJUDGE_ALLOWED_HOSTS=judge.math.ryukoku.ac.jp,judge.math.ryukoku.ac.jp:8443   # #116: 逆プロキシ必須
  AIJUDGE_LEARNER_URL=https://judge.math.ryukoku.ac.jp
  AIJUDGE_CONSOLE_URL=https://judge.math.ryukoku.ac.jp:8443
  ```
  nginx が `X-Forwarded-Proto https` を渡すので Cookie の `Secure` は自動で付く。
- アプリは `127.0.0.1` のみ bind（既定）。

### 3.7 systemd ユニット

unit ファイルはリポジトリの `deploy/` に版管理し、elite へはそこからコピーする（システム部分＝再生成可能）。

```
EnvironmentFile = /srv/aijudge/config/aijudge.env    （データ側に置く。DR で一緒に戻る）
aijudge-web.service       ExecStart=… aijudge-web --host 127.0.0.1 --port 8080 --workers 4
aijudge-review.service    （--workers は不要。教員数は少なく、大きなアップロード経路も無い）
aijudge-worker-det.service          --phase deterministic --name det
aijudge-worker-ai@.service          --phase ai --name ai%i     （ai@1..ai@4 を enable）
aijudge-finalize.service            --once（Type=oneshot）
aijudge-finalize.timer              OnCalendar=hourly
aijudge.target                      web / review / worker-* を Wants= でまとめる
```
- `systemctl restart aijudge.target` で**ワーカーも含めて全入れ替え**（RUNNING.md #60/#80: 古いワーカーが新コードの採点行を読めず溜まる事故が 2 回）。
- `aijudge-web` は `--workers 4`（uvicorn 子プロセス 4）。1 本のアップロードの
  `write`/`fsync` ブロッキングが他の接続を巻き込まないため（動画の同時アップロード対策）。
  子は設定を **env からしか受け取らない**ので `EnvironmentFile` を必ず設定する。
  `LimitNOFILE=65536` を付ける（同時アップロードで fd を食う）。

### 3.8 バックアップ

- **DB ダンプ**：`pg_dump -Fc` を systemd timer で夜次 → `/srv/aijudge/backups/db/aijudge-<日時>.dump`（世代保持 14 日）。
  無損失が要件なら `archive_command` で WAL を `/srv/aijudge/backups/wal/` へ → PITR。
- **restic**：`/srv/aijudge` を丸ごと 1 リポジトリで（暗号化・重複排除・増分）。
  - target 1: `/work/aijudge/restic`（同一機・別ディスク。誤削除・SSD 故障向け）
  - target 2: **オフボックス**（復旧先 PC / NAS / 学内ストレージ。機体全損向け）
  - 頻度: 1〜数時間ごと。`restic forget --keep-daily 14 --keep-weekly 8` で剪定。
  - `/srv/aijudge` には `pgdata` を置かないので除外設定は不要。ダンプ・WAL・artifacts・observations・config が丸ごと入る。
- **動画** `/work/aijudge/video`：restic 対象外。保持ポリシー（成績確定＋異議期間の経過後に削除、または外部アーカイブ）。
- ディスク監視: `/`・`/srv/aijudge`・`/work` の空きを 85% で警告（`/work` が動画で埋まっても FS が別なので DB と `/` は無事）。

### 3.9 障害復旧（別 PC で復元）

前提: `deploy/bootstrap.sh`（ユーザ・グループ・ディレクトリ・polkit・fstab 断片を作る）と
unit / nginx / polkit のコピーをリポジトリの `deploy/` に置いておく。

1. 依存導入: `postgresql`, `docker` + `runsc`, `uv`, `nginx`, `certbot`, `restic`。
2. `git clone <repo> /opt/aijudge && cd /opt/aijudge && git checkout <稼働タグ> && uv sync --frozen`
3. `deploy/bootstrap.sh` を実行（`aijudge` ユーザ/グループ、`/srv/aijudge` 用マウントポイント、polkit）。
4. データ復元: `restic -r <offbox> restore latest --target /srv/aijudge`
   （新しいデータディスクを `/srv/aijudge` にマウントしてから）。
5. DB 復元: `createdb -O aijudge aijudge` → `pg_restore -d aijudge /srv/aijudge/backups/db/<最新>.dump`
   （WAL があれば PITR 手順）。
6. `deploy/` から unit / nginx vhost / polkit を配置。`aijudge.env` は restore 済み。
7. `cd /opt/aijudge && uv run alembic upgrade head`（ダンプがタグより古い場合の差分を当てる）。
8. `systemctl enable --now aijudge.target`。証明書は新ホストで `certbot`（または DNS を新 IP へ）。
9. `curl` 疎通確認 → 提出 1 件を通す。

**「データ部分」= restic リポジトリ 1 個 ＋ git タグ 1 個。** これだけで系全体が別 PC に戻る。

---

## 4. 作業手順

- [x] **DNS**: `judge.math.ryukoku.ac.jp` → elite（133.83.82.119）。設定済み。
- [x] **TLS**: `elite.math` / `judge.math` の証明書取得・自動更新設定・dry-run 成功。
- [x] 1. `aijudge` ユーザ（uid 996）/ `aijudge` グループ（gid 992）作成、`sano` を `aijudge` グループへ（次回ログインで有効）。
- [x] 1b. `sda1` を fstab に UUID 追加（`defaults,noatime,nofail 0 2`）→ `/srv/aijudge` マウント → `aijudge:aijudge` 2770、`artifacts/ observations/ config/ backups/{db,wal}/` 作成。`findmnt --verify` / `mount -a` クリーン。
- [x] 1c. `aijudge` と `sano` を `docker` グループへ（2026-09-06 実地確認: 両ユーザとも groups に `docker(142)` あり）。`/work/aijudge/{video,media,restic,tmp}` 作成済み。polkit ルール本体は `/etc/polkit-1/rules.d/` が非 root から読めず未確認（root 権限が要る）。
- [x] 2. `/opt/aijudge`（2026-09-06 実地確認: `drwxrwsr-x aijudge`）、`/var/lib/aijudge/work`（`drwxrws--- aijudge`）、`/srv/aijudge/config/aijudge.env`（`-rw-r----- root`、17 変数設定済み）とも存在確認。
- [x] 3. PostgreSQL 18.6 導入、role/db `aijudge`（peer 認証、socket `/var/run/postgresql`）、`pgdata` は既定 `/var/lib/postgresql/18/main`（`/`）。`aijudge-db-backup.{service,timer}`（毎 02:30、14 日保持）稼働。
- [x] 4. uv 0.12.9 を `/usr/local/bin` に、`/opt/aijudge` に public repo を clone（detached `v0.19.2` / 9b24243）、`uv sync --frozen --extra dev`。5 エントリポイント `--help` OK。
- [x] 5. `alembic upgrade head` → `a1c4e77b90d2`、21 テーブル作成。
- [x] 6. **サンドボックス検証**: `test_container.py` が `docker` / `gvisor` とも **16 passed / 0 skipped**。`build_sandbox()` は `auto` で `docker:runsc`（`kernel_isolated`）を選択。
- [x] 7. systemd unit（web / review / worker-det / worker-ai@ テンプレ / finalize.{service,timer} / `aijudge.target`）配置。web・review・worker-det・finalize.timer を enable+start。`127.0.0.1:8080`/`:8765` で 303（→ /login）。
- [x] 8. nginx vhost `judge.math.ryukoku.ac.jp`（80→301 / 443→:8080 / 8443→:8765）を sites-enabled に。`nginx -t` OK、ECDSA 証明書 full chain verify OK、HSTS・security-headers 継承、`elite.math` 無影響。
- [x] 9. LLM モデルを評価し `AIJUDGE_LLM_MODEL=gemma4:e4b` に確定（2026-09-05、
      `docs/design/01_`〜`03_`）。slab-llm 側は `qwen3.8:27b-mlx` の常駐ピン
      （blume 用）を無効化し `gemma4:e4b` を常駐に切替え済み（下記注記）。
      2026-09-06 実地確認: elite 側の環境変数も設定済みで **`aijudge-worker-ai@1` が稼働中**
      （`systemctl list-units` で `active running`）。AI 観点の縮退運転は解消。
- [ ] 10. `aijudge-admin` でコース・利用者を作成、提出 1 件を通す（決定的採点は現時点で可）。
      2026-09-06: DB への直接確認は sudo 経由でしかできず（自動運転の権限で未実施）、
      未確認のまま。sudo なしで確認できる手段（例: `aijudge-admin course list` 相当）があれば
      それで検証する。
- [x] 11. `deploy/`（bootstrap.sh・unit・nginx・polkit のコピー）と `deploy.sh` をリポジトリに追加（2026-09-05、`feat/deploy-cd`）。
      **リポジトリ側は汎用テンプレート**（`AIJUDGE_HOSTNAME` 等で機関非依存）。
      elite 固有の値はこのファイルと `/srv/aijudge/config/aijudge.env` にだけ置く。
      2026-09-06 実地確認: elite 上 `/opt/aijudge/deploy/` に配置済み（bootstrap.sh・deploy.sh・
      aijudge-autodeploy.sh・systemd/・nginx/・polkit/ すべて存在）。
      **`aijudge-autodeploy.timer` を 2026-09-06 01:15 JST に `enable --now`。
      `active (waiting)`、5 分毎トリガ、初回実行は正常終了（`v0.25.3` が既に最新のため
      実デプロイは走らず no-op）。CD（方式B）は稼働開始。**
      restic は `/usr/bin/restic` は入っているが `/work/aijudge/restic` は空で
      リポジトリ未初期化、systemd timer もなし → target 1（オンボックス）・
      target 2（オフボックス）とも未設定だった（2026-09-06 時点）。
      **target 1 の資材をリポジトリに追加**（`deploy/aijudge-restic-backup.sh`・
      `deploy/aijudge-restic.env.example`・`deploy/systemd/aijudge-restic-backup.{service,timer}`、
      手順は `deploy/README.md`「バックアップ（restic、target 1）」）。パスワードは
      本体の `aijudge.env` と隔離した専用ファイル（`/srv/aijudge/config/aijudge-restic.env`・
      `restic-password`）に置く設計。repository は `/work/aijudge/restic`、頻度は 4 時間毎。
      **2026-09-06 elite に設置・稼働確認済み。** リポジトリ `efa5c40d76` を
      `/work/aijudge/restic` に `restic init`、手動バックアップで疎通確認
      （途中 `lost+found`・`.Trash-1000`（sano 自身のゴミ箱、データではない）が
      aijudge から読めず exit 3 になったため、スクリプトに `--exclude` を追加して解消。
      再実行で `snapshot ... saved` / exit 0）。`aijudge-restic-backup.timer` を
      `enable --now`、`active (waiting)`・4 時間毎トリガ確認済み。
      target 2（オフボックス）はまだ着手できていない — 復旧先 PC / NAS / 学内ストレージの
      どれを使うか決まっていない。
      README / RUNNING.md への elite 手順追記も未着手。

### 現在の状態（2026-09-06 実地確認）

`https://judge.math.ryukoku.ac.jp/`（学生・200）と `https://judge.math.ryukoku.ac.jp:8443/`（教員・学内のみ・200）
とも `/login` を返す。決定的パイプラインに加え **AI ワーカーも稼働中**（`gemma4:e4b`）。
elite 上のデプロイタグは `v0.25.3`（`git -C /opt/aijudge describe --tags` で確認、リポジトリ HEAD と一致・遅延なし）。
残るのは §7-5・§7-7 の判断待ちと、CD 自動化（autodeploy timer 有効化）・restic 2 target・
初回データ投入（#10）・ドキュメント追記（#11 残）。

---

## 5. リスク・注意

| # | 事項 | 対応 |
|---|---|---|
| R1 | `aijudge`・`sano` が docker グループ = root 相当 | `sano` は既に passwordless sudo 持ちで実効権限は不変。実行 owner は `aijudge` に固定 |
| R2 | elite は共用機で他アプリと同居 | 容量は十分（16C/30GB/259GB 空き）。§7-7 は要判断のまま |
| R3 | ~~LLM が単一ホスト（slab-llm、Mac）依存~~ → **2026-09-06: elite ローカルの PAIR クラスタ（6 ノード）へ切替、単一ホスト依存は解消** | クラスタ全体が落ちた場合は従来どおり AI 観点が unscored・総点は withheld（P2/P3 の設計どおり）で縮退継続 |
| R8 | ~~`ollama-proxy`（PAIR）が対話セッション頼み~~ → **2026-09-06 確認: 実際は `nvpair-tui.service`（systemd `--user`、`enabled`・`Restart=on-failure`）配下の tmux セッションの子プロセス。`loginctl show-user sano` で `Linger=yes` も確認済みで、sano のログアウト・elite 再起動どちらにも耐える** | 残る未確認点のみ: `ollama-proxy` 個別プロセスがクラッシュした際、親の `nvpair-ui-broker`（NVIDIA 製、他 8 プロセスの管理が本来の役目）が再起動するか。本番稼働中に落として確認するのは避けた。保守時間帯に一度検証すること |
| R4 | 8443 は大学 FW で閉 → 学外教員は到達不可 | 承知の上。学外は VPN 経由。IP 制限は不要 |
| R5 | サブパス未対応 | `judge.math` サブドメイン方式で回避（確定） |
| R6 | certbot lineage | `judge.math` を別 lineage で取得済み（webroot・ECDSA）。`elite.math` は非改変 |
| **R7** | **動画提出は現状のコードでは不可** | `ArtifactKind` に動画が無く（`.mp4` 等 未登録）、アップロードは `await upload.read()` で**全体を RAM に載せる**（`MAX_UPLOAD_BYTES=1 MiB` 固定、CLI から変更不可）。数 GB 動画には次が要る: ①動画 `ArtifactKind` と拡張子・MIME 追加 ②アップロードのストリーミング（ディスクへ逐次、RAM に載せない）③`ArtifactStore.put/get` のストリーム対応 ④型別・課題別のサイズ上限（コード）⑤nginx `client_max_body_size` 引き上げ + `proxy_request_buffering off` ⑥レジューム可能アップロード（tus 等、3 GB を学内 Wi-Fi で送るため）⑦動画観点は human-scored（PDF/画像と同様、モデルに渡さない）。§3.2.1(C) の型別ルートもこの PR に含める |

---

## 6. 見送るもの（v1 では入れない）

- MinIO（ローカル FS で足りる）
- アプリのコンテナ化 / k8s
- elite ローカル LLM（27B が VRAM に載らない。縮退用の任意オプション）
- 自己ホスト GitHub Actions runner（**public リポなので危険** — §8 参照）

---

## 7. 残課題

- [x] 1. DNS → `judge.math.ryukoku.ac.jp` で確定。
- [x] 2. 教員コンソール公開範囲 → 同一サブドメイン 8443、IP 制限なし、FW で学内限定。
- [x] 3. `docker` グループ → `aijudge` + `sano` を追加、実行 owner は `aijudge`。
- [x] 4. **LLM モデル名** → `gemma4:e4b` に確定（2026-09-05、`docs/design/01_`〜`03_`）。
- [ ] 5. **今学期の規模**（受講者数・コース数）。ワーカー本数（既定: 決定的 1 + AI 4）の調整判断。
      2026-09-06: LLM を PAIR クラスタ（§3.5）へ切替えたことで「単一ホストの容量不足で
      本数を絞る」制約は外れた（4 本でも無理のない規模、と判断済み）。ただし受講者数・
      コース数そのものはまだ未確定で、本数判断の全体は残課題のまま。
- [x] 6. **デプロイ契機 / CD**: **方式 B（pull 型 systemd timer）で確定**（2026-09-04）。§8 参照。
- [ ] 7. **同居の可否**: 学生向け本番を他アプリ同居の elite で運用してよいか（専用機の要否）。

---

## 8. 継続デプロイ（CD）— 方式 B（pull 型 systemd timer）で確定

契機は **`v*` タグの push**（`[[release-tagging]]` の運用: ルート `pyproject` の version を上げて `v<version>`）。
elite が 5 分ごとに origin の `v*` タグを見て、デプロイ済みより新しければ入れ替える。
**GitHub 側の設定・秘密・inbound は一切不要。**

前提: §4-9〜11 が済み、`deploy/` がリポジトリにあること（下記スクリプト・bootstrap・unit/nginx/polkit のコピー）。

### 8.1 なぜ B か

- **リポジトリは public。** GitHub Actions の自己ホスト runner は fork の PR やワークフロー注入で
  elite 上に任意コードが走りうるので採らない（GitHub 自身が非推奨）。
- **elite は学内共用機。inbound は増やさない**（80/443/8443 のみ。8443 は FW で学外遮断）。
- B は elite が **自分で `git ls-remote` して判断**するだけ。GitHub が elite への到達手段を持たない。
- 代償: CI 緑の gating が無い（タグ命名の規律に依存）、最大 5 分の遅延、
  デプロイログは elite の journald だけ（`journalctl -u aijudge-autodeploy`）。
- （検討して不採用にした案: GH-hosted runner + Tailscale + SSH。即時だが GitHub に SSH 鍵と
  Tailscale key を預ける。B で運用してみて遅延が問題になったら再検討。）

### 8.2 `deploy/deploy.sh`（elite 上、`aijudge` で実行 — B/手動 共通の心臓部）

```sh
#!/usr/bin/env bash
set -euo pipefail
TAG="${1:?tag required}"
cd /opt/aijudge
exec 9>/run/lock/aijudge-deploy.lock; flock -n 9 || { echo "deploy already running"; exit 0; }

git fetch --tags --prune
git rev-parse "refs/tags/${TAG}^{commit}" >/dev/null       # タグの実在を確認
/usr/local/sbin/aijudge-db-backup.sh                        # デプロイ直前のダンプ（ロールバックの保険）
git checkout --detach "refs/tags/${TAG}"
/usr/local/bin/uv sync --frozen --extra dev
/usr/local/bin/uv run --project /opt/aijudge alembic upgrade head
systemctl restart aijudge.target                            # web/review/worker-det（polkit）
systemctl list-units 'aijudge-worker-ai@*' --state=loaded -q \
  && systemctl restart 'aijudge-worker-ai@*' || true        # AI ワーカーも入れ替える（#60/#80）
systemctl try-restart aijudge-finalize.timer
# 疎通確認。落ちていたら非ゼロで終わる（timer のログに残る）
curl -fsS --max-time 10 https://judge.math.ryukoku.ac.jp/login >/dev/null
systemctl is-active --quiet aijudge-web aijudge-review aijudge-worker-det
echo "deployed ${TAG} ($(git rev-parse --short HEAD))"
```

- **migration は restart の直前**。`alembic upgrade head` が失敗したら `set -e` で中断し、
  古いプロセスは動いたまま（列を足しかけて放置しない）。
- **ワーカーも必ず入れ替える**（RUNNING.md #60/#80: 古いワーカーが新コードの採点行を読めず溜まった事故が 2 回）。
- `flock` で timer の重複起動・手動実行との衝突を防ぐ。

### 8.3 `deploy/aijudge-autodeploy.sh`（poller・`aijudge` で実行）

```sh
#!/usr/bin/env bash
set -euo pipefail
cd /opt/aijudge
current="$(git describe --tags --exact-match 2>/dev/null || echo none)"
latest="$(git ls-remote --tags --refs origin 'v*' \
          | sed 's#.*refs/tags/##' | sort -V | tail -1)"
[ -n "${latest}" ] || { echo "no v* tags on origin"; exit 0; }
[ "${current}" = "${latest}" ] && exit 0                    # 最新。何もしない
echo "autodeploy: ${current} -> ${latest}"
exec /opt/aijudge/deploy/deploy.sh "${latest}"
```

### 8.4 systemd（`deploy/` に置き、bootstrap で配置）

```
# aijudge-autodeploy.service
[Unit]
Description=aiJudge pull-deploy latest v* tag
After=network-online.target postgresql.service

[Service]
Type=oneshot
User=aijudge
Group=aijudge
WorkingDirectory=/opt/aijudge
ExecStart=/opt/aijudge/deploy/aijudge-autodeploy.sh

# aijudge-autodeploy.timer
[Unit]
Description=Poll for a new aiJudge release every 5 min

[Timer]
OnBootSec=2min
OnUnitInactiveSec=5min
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
```

### 8.5 運用

- **一時停止 / ピン留め**: `sudo systemctl disable --now aijudge-autodeploy.timer`。
  再開は `enable --now`。停止中も手動で `deploy/deploy.sh <tag>` は打てる。
- **ロールバック**: `deploy/deploy.sh <前のタグ>` を手動実行（down-migration は基本無いので
  前進修正が原則。DB が絡む場合は直前ダンプから `pg_restore`）。
  timer が動いていると次のポーリングで最新へ戻すので、ロールバック中は timer を止める。
- **CI 緑の担保**: **タグは CI が緑になってから打つ**（`main` のマージ後、Actions の緑を確認して
  `git tag v… && git push --tags`）。B にはこれを機械で止める仕組みが無い。
- **監視**: `journalctl -u aijudge-autodeploy -f`。失敗した回は非ゼロ終了で残る。
  必要なら timer に `OnFailure=` で通知ユニットを足す。

### 8.6 リスク

| # | 事項 | 対応 |
|---|---|---|
| CD1 | タグが CI 緑でないコミットを指す | 運用規律（緑を見てから打つ）。将来 §8.1 の GH-hosted 案に移れば `gh api` で機械化できる |
| CD2 | migration 失敗で中途半端な DB | `deploy.sh` は `set -e`＋restart 前に migrate。直前ダンプを保持 |
| CD3 | timer とロールバックの競合 | ロールバック中は timer を停止（§8.5） |
| CD4 | 重複実行 | `flock`（`deploy.sh`）＋ `Type=oneshot`（同一ユニットは多重起動しない） |
| CD5 | `git ls-remote` が届かない（GitHub 障害） | ポーリングが空振りするだけ。次回リトライ。稼働中のサービスに影響なし |

---

https://claude.ai/code/session_01XnU3ihHEw7ssmFjNeDC4vF
