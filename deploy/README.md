# デプロイ資材（`deploy/`）

自分の機関で aiJudge を動かすためのテンプレート一式である。**このリポジトリは公開している**ので、
ここには機関固有の値（実ホスト名・実 IP・実アカウント名など）を一切書かない。
機関固有の値はすべて `/srv/aijudge/config/aijudge.env`（サーバ側・git 管理外）に置く。

具体的な導入手順と実際の値は、導入する機関が非公開のドキュメントで管理すること
（例: このリポジトリの運用元は、`docs/design/elite-deployment-proposal.md` を
git で追跡しないローカルファイルとして持っている）。

## 中身

| パス | 役割 |
|---|---|
| `bootstrap.sh` | 初回セットアップ（systemd unit・nginx・polkit の配置、有効化） |
| `deploy.sh` | 1 つのタグをデプロイする（手動のデプロイと CD の両方が呼ぶ本体） |
| `aijudge-autodeploy.sh` | origin の `v*` タグを見て、新しければ `deploy.sh` を呼ぶ（CD のポーラー） |
| `systemd/` | unit ファイル一式（`aijudge.target` が web/review/worker-* を束ねる） |
| `nginx/aijudge.conf.template` | リバースプロキシの雛形。`AIJUDGE_HOSTNAME` を置換して使う |
| `polkit/49-aijudge.rules` | `aijudge` グループが sudo なしで unit を起動停止できるようにする |
| `aijudge.env.example` | `EnvironmentFile` の雛形。値を埋めて `/srv/aijudge/config/aijudge.env` に置く |
| `aijudge-restic-backup.sh` | `/srv/aijudge` を restic でバックアップする（target 1・オンボックス） |
| `aijudge-restic-offbox.sh` | 同じものをオフボックスの受け先へ（`@target2` / `@target3`。月次を 6 本残す） |
| `aijudge-restic-check.sh` | 受け先の `restic check`（中身も一部読む）と最新スナップショットの鮮度（週次・失敗はメール、#427） |
| `aijudge-purge-preview.sh` | 保存期間を過ぎた動画・作業の記録の下見。あればメール（週次。消すのは人、#427） |
| `aijudge-restic.env.example` | restic 専用の `EnvironmentFile` の雛形。パスワードを本体の env から隔離する |
| `aijudge-db-backup.sh` | `pg_dump -Fc`（論理・日次）。**`deploy.sh` もデプロイ直前に呼ぶ** |
| `aijudge-pg-basebackup.sh` | 物理ベースバックアップ（PITR の土台・週次）と、不要になった WAL の掃除 |
| `aijudge-storage-check.sh` | 空き容量と WAL アーカイブの健全性（毎時・遷移時のみ通知） |
| `aijudge-llm-primary-check.sh` | プライマリ LLM が確定モデルを提供しているか（30 分ごと・状態が変わったときだけ通知。フォールバックもできない NG3 だけは、2 回続いてから通知する。`AIJUDGE_LLM_NG3_CONFIRM`） |
| `lib/llm-primary-check.py` | 上の判定の本体。**どちらのプロバイダが応答したか**で判定する（`/usr/local/lib/aijudge/`） |
| `aijudge-notify` | 日本語のメールを文字化けさせずに送る。上記の検査は、すべてこのコマンドで通知する |
| `journald/aijudge.conf` | 運用ログの保存期間とディスク上限（`/etc/systemd/journald.conf.d/` に置く） |

**スクリプトは `/usr/local/sbin/` に置く**（`lib/` のものは `/usr/local/lib/aijudge/`）。
初回は `bootstrap.sh` が、以後はデプロイのたびに `install-units.sh` が配置する。unit と同じく、
**中身が同じなら書き換えない**（#344）。`aijudge-config-check` が、配置した側と配る側の一致を確かめている。

**チェックアウトから直接実行するスクリプトは配置しない。** `aijudge-autodeploy.sh` と
`aijudge-config-check.sh` は、unit が `/opt/aijudge/deploy/` のファイルを直接指している。
`aijudge-vision-check.sh` は unit を持たない、**手で実行する診断**である
（画像モデルが「画像を読めると名乗るが、実際には読めない」構成を見つける。ADR 0021）。
コピーを増やすと、どちらが実行されているのか分からなくなる。

## 前提（`docs/RUNNING.md` と共通）

- プロセスは system ユーザ `aijudge`（nologin）で実行する。管理操作をする人は `aijudge` グループに
  所属し、polkit を通じて sudo なしで unit を操作する。
- コードは `/opt/aijudge` に clone する（読み取り専用の deploy key で足りる。CD は push しない）。
- 提出物・DB ダンプ・restic ターゲットなどのデータは `/srv/aijudge` の下に置く（`EnvironmentFile` もここ）。
- PostgreSQL にはローカルソケットで接続する。SQLite は使わない（複数のワーカーが行ロックを必要とするため）。

## 初回セットアップ（`bootstrap.sh`）

```fish
sudo useradd --system --no-create-home --shell /usr/sbin/nologin aijudge
sudo usermod -aG aijudge (whoami)   # 管理操作をする人間を aijudge グループへ
# 一度ログアウト/ログインしてグループを反映

sudo mkdir -p /srv/aijudge/config
sudo cp deploy/aijudge.env.example /srv/aijudge/config/aijudge.env
sudo $EDITOR /srv/aijudge/config/aijudge.env   # 機関固有の値をここに埋める
sudo chown aijudge:aijudge /srv/aijudge/config/aijudge.env
sudo chmod 640 /srv/aijudge/config/aijudge.env

set -gx AIJUDGE_HOSTNAME judge.example.ac.jp   # 自分のホスト名に置き換える
sudo -E deploy/bootstrap.sh
```

`bootstrap.sh` は次の処理を行う。

1. `deploy/systemd/*` を `/etc/systemd/system/` へコピーし `daemon-reload`。
2. `deploy/nginx/aijudge.conf.template` の `__AIJUDGE_HOSTNAME__` を
   `$AIJUDGE_HOSTNAME` に置換して `/etc/nginx/sites-available/` へ配置
   （**証明書は取得しない**。Let's Encrypt などは、導入する機関の既存の運用に従う）。
3. `deploy/polkit/49-aijudge.rules` を `/etc/polkit-1/rules.d/` へ配置。
4. `aijudge.target` を enable するが、**start はしない**
   （初回はコード・DB・証明書が揃ってから `deploy.sh` で上げる）。

## 通常運用

**手でコマンドを実行するときは、先に環境ファイルを読み込む**（#345）。unit は
`EnvironmentFile=/srv/aijudge/config/aijudge.env` を読み込むが、コマンドを直接実行すると
環境ファイルは読み込まれない。`AIJUDGE_DATABASE_URL` が無いまま `alembic upgrade head` を実行すると、
**意図しない DB を変更するおそれがある**。環境ファイルを読み込まずに実行した場合、`deploy.sh` は最初に実行を拒否する。

```fish
# 手動デプロイ（CI が緑になったタグで）
sudo -u aijudge sh -c 'set -a; . /srv/aijudge/config/aijudge.env; set +a; \
    exec /opt/aijudge/deploy/deploy.sh v1.2.3'

# CD を有効化（5 分ごとに origin の v* タグを見に行く）
sudo systemctl enable --now aijudge-autodeploy.timer

# 監視
journalctl -u aijudge-autodeploy -f
```

### リリースのとき ── `pyproject` と `uv.lock` を必ず揃える

タグを付けるコミットでは、**版を 2 か所とも上げる**。

```fish
# pyproject.toml の version を上げ、ロックファイルを追随させる
uv lock
git add pyproject.toml uv.lock
git commit -m "chore: release 0.44.0"
git tag -a v0.44.0 -m v0.44.0 && git push origin main v0.44.0   # 署名付きの注釈タグ（下の「unit の配布と署名」）
```

**片方だけ上げると、CD が何も知らせずに止まる。** `deploy.sh` の `uv sync --frozen` は
ロックファイルを書き換えない。しかし、それ以前にサーバで一度でも `uv sync` を実行していると、
`uv.lock` のうち aiJudge 自身の版だけが書き換わり、作業ツリーに変更が残る。すると次のデプロイからは

```
error: Your local changes to the following files would be overwritten by checkout:
        uv.lock
```

というエラーで `git checkout` が中断し、**`deploy.sh` はそこで終了する**。古い版のアプリが動き続け、
学生にも教員にも何も起きないので、誰も気づかない。実際に v0.35.1 でこれが起き、
**8 リリース分（v0.36.0 〜 v0.43.0）がデプロイされないまま 2 日間運用していた**。

この失敗に気づくための仕組みは次のとおりである。**失敗した unit は、メールで通知する**（`OnFailure=aijudge-notify@%n`、#422）。
また、学生画面とコンソールの `/login` に 5 分ごとに外からアクセスし、状態が変わったときに
通知する（`aijudge-http-check.timer`）。手で確かめるときは、次のコマンドを使う。

```fish
systemctl is-failed aijudge-autodeploy.service        # failed なら止まっている
sudo -u aijudge git -C /opt/aijudge describe --tags   # 実際に動いている版
git ls-remote --tags --refs origin 'v*' | sed 's#.*/##' | sort -V | tail -1
```

デプロイが止まったときは、サーバ側で書き換わった `uv.lock` を元に戻してから、デプロイし直す。

```fish
sudo -u aijudge git -C /opt/aijudge checkout -- uv.lock
sudo -u aijudge sh -c 'set -a; . /srv/aijudge/config/aijudge.env; set +a; \
    exec /opt/aijudge/deploy/deploy.sh v0.44.0'
```

**いまは、この問題を 3 か所で防いでいる**（#423）。`deploy.sh` は `UV_FROZEN=1` で
ロックファイルを書き換えず、作業ツリーに変更が残っていれば checkout の前に止まる。CI は
`uv sync --locked` を実行し、`pyproject` と `uv.lock` がずれた時点で失敗する。

### デプロイ済みの版と、失敗したときの再試行

`deploy.sh` は、**最後の疎通確認まで成功したときだけ** `/var/lib/aijudge/deployed-tag`
にタグを書き込む（#421）。autodeploy はこのファイルを見てデプロイ済みかを判定する。そのため、checkout の後で
migration や `uv sync` が失敗しても「最新がデプロイ済み」とは判定されず、次の周回
（5 分後）に同じタグをもう一度試す。

```fish
sudo cat /var/lib/aijudge/deployed-tag                 # 最後まで通った版
sudo -u aijudge git -C /opt/aijudge describe --tags   # 作業ツリーの版（途中で落ちると先に進んでいる）
```

2 つが違うなら、デプロイは途中で失敗している。`journalctl -u aijudge-autodeploy` で原因を確かめる。

### 切り戻し（#425）

新しい版に問題があったときは、**固定ファイルに前の版を書く**。autodeploy は、
固定ファイルがあればそのタグをデプロイし、最新の版を追わない。固定しないまま古いタグを
手でデプロイしても、5 分以内に最新の版へ戻される。

```fish
# 1. 前の版に固定する（次の周回で deploy.sh がその版を入れる）
echo v1.28.0 | sudo -u aijudge tee /var/lib/aijudge/deploy-pin
# すぐ入れたいときは手で流してもよい
sudo -u aijudge sh -c 'set -a; . /srv/aijudge/config/aijudge.env; set +a; \
    exec /opt/aijudge/deploy/deploy.sh v1.28.0'

# 2. 直ったタグを出したら固定を外す（最新を追う状態に戻る）
sudo -u aijudge rm /var/lib/aijudge/deploy-pin
```

**migration は戻さない。** 移行は進める方向にしか書いていない（`downgrade` は
あっても試していない）。そのため、スキーマを変えた版から戻すときは、次のどちらかを選ぶ。コードだけを
戻し、古いコードが新しいスキーマで動くかを確かめる。または、デプロイ直前の
ダンプ（`aijudge-db-backup.sh`）から DB を戻す。ダンプから戻すと、その後の提出と
採点は失われる。どちらを選ぶかは、運用者が判断する。

移行は、ロックを 10 秒までしか待たない（`migrations/env.py` の `lock_timeout`）。
長いトランザクションの後ろで ALTER が待っている間に、ほかのすべてのクエリが ALTER の後ろに
並び、画面全体が止まるのを防ぐためである。10 秒待ってもロックを取れなければ移行は失敗し、上で述べた再試行の対象になる。

### unit の配布と署名（#417）

unit と、unit が呼ぶ `/usr/local/sbin` のスクリプトは、**root が配置する**
（`aijudge-units.service`）。root はチェックアウト（aijudge 所有）の中身を信用せず、
**署名を確かめたタグ**の中身だけを配置する。

- root 所有のミラー `/var/lib/aijudge-release/repo.git` を origin から更新する
- チェックアウトの `.git/HEAD`（コミットのハッシュ）を指す `v*` タグを探す
- タグの署名を `/etc/aijudge/allowed_signers` で確かめる。**無ければ何も配らずに失敗**
  し、`OnFailure` でメールが届く
- そのタグから `git archive` で取り出した `deploy/` を配る

**リリースのタグは署名する**（`git tag -s`。このリポジトリでは `tag.gpgSign=true`
にしてあるので `git tag -a` でも署名される。**`git tag v1.2.3` のような軽量タグは
署名されない**）。許可する鍵は `deploy/release-signers` にも置いてある（公開鍵。
**運用機が信じるのは `/etc/aijudge/allowed_signers` の方**）。

**コードのデプロイも、署名を確かめたタグだけを対象にする**（2026-09-29）。以前は
unit の配布だけが署名を確かめていて、署名していないタグでもコードのデプロイは進んだ。
GitHub にタグを push できる人なら、署名の無いコードを運用機で動かせたことになる（実際に、
署名の無い v1.37.0 がデプロイされた）。いまは次のように動く。

- `deploy.sh` は、チェックアウトの前に `deploy/lib/verify-release-tag.sh` で署名を
  確かめる（手で流すときも同じ）。確かめられなければ何もせずに失敗する
- `aijudge-autodeploy.sh` は、**署名を確かめられたタグの中で最新のもの**を選ぶ。
  それより新しい署名の無いタグは飛ばし、名前をログに出す
  （`journalctl -u aijudge-autodeploy`）。出したはずの版がデプロイされないときは、このログを見る
- 固定していなければ、**デプロイ済みより古い版へは戻らない**（#564）。origin で最新の
  タグが消えても 1 つ前の版へ自動で戻らない。戻すときは「切り戻し」の固定を使う
- 許可リストは unit の配布と同じ `/etc/aijudge/allowed_signers`。デプロイする利用者
  （aijudge）が書き換えられる許可リストは信じない

最初の 1 回だけは、人が root で配置する（中身を確かめてから実行すること）。

```sh
# 1. 許可する署名鍵（公開鍵）。deploy/release-signers と同じ内容を、目で確かめて置く
sudo install -d -m 755 /etc/aijudge
sudo install -m 644 /dev/stdin /etc/aijudge/allowed_signers < release-signers

# 2. root 所有のミラー
sudo install -d -m 700 /var/lib/aijudge-release
sudo git clone --bare --quiet https://github.com/sanoakr/aijudge.git /var/lib/aijudge-release/repo.git

# 3. 署名済みタグから配る側を取り出して置き、1 度走らせる（以後は deploy が起動する）
TAG=v1.2.3   # 署名済みのタグ
M='git --git-dir=/var/lib/aijudge-release/repo.git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile=/etc/aijudge/allowed_signers'
sudo $M verify-tag "$TAG"
sudo sh -c "$M show $TAG:deploy/install-units.sh > /usr/local/sbin/aijudge-install-units"
sudo chmod 755 /usr/local/sbin/aijudge-install-units
sudo /usr/local/sbin/aijudge-install-units
```

鍵を替えるときは、`/etc/aijudge/allowed_signers` に新しい鍵を**追加してから**、新しい鍵で
署名したタグを出す（古い鍵を先に消すと、その間は配布が止まる）。

### ログを読む（ADR 0016）

運用では `AIJUDGE_LOG_FORMAT=json` を設定する（1 行に 1 イベント）。
unit には `SyslogIdentifier` が付いているので、サービスごとにログを絞り込める。

```fish
# 採点ワーカーの失敗だけ
journalctl -u aijudge-worker-ai@1 -o cat | jq -R 'fromjson? | select(.level == "ERROR")'

# **1 つの提出について、web とワーカーの両方の行を集める。**
# 突き合わせの鍵は submission_id ── これが無かったので、#60 / #80 では
# 画面から「採点が遅い」としか見えなかった（docs/RUNNING.md）。
journalctl -t aijudge-web -t aijudge-worker-det -t aijudge-worker-ai1 -o cat \
  | jq -R 'fromjson? | select(.submission_id == "SUB-ID")'

# 1 リクエストの中で起きたこと（学生の問い合わせに付いてくる X-Request-ID から）
journalctl -t aijudge-web -o cat | jq -R 'fromjson? | select(.request_id == "REQ-ID")'
```

保存期間は 90 日である（`journald/aijudge.conf`）。**成績に関わる「誰が何を変えたか」は、
運用ログには記録しない。** その記録は DB の監査ログに残り、DB ダンプとともに restic でバックアップされる。
運用ログは、消えても困らない記録として扱う。

```fish
sudo install -m 0644 /opt/aijudge/deploy/journald/aijudge.conf \
    /etc/systemd/journald.conf.d/aijudge.conf
sudo systemctl restart systemd-journald
```

設計の背景（GitHub Actions から push する方式ではなく、timer で pull する方式にした理由など）は、
`docs/design/00_システム設計方針と構築計画.md` と、運用元の非公開のデプロイ手順書を参照すること。

## バックアップ（restic、target 1 = オンボックス）

`bootstrap.sh` は、バックアップの準備を自動化しない（DB ダンプの `aijudge-db-backup.{service,timer}` と同様に、
初回の設定は運用者が手で行う一度きりの操作だからである）。

```fish
# パスワードファイル・env（root:aijudge 0640。restic のパスワードは本体の
# aijudge.env とは別ファイルに置く — web/review/worker の環境に触れさせないため）
sudo install -d -m 0750 -o root -g aijudge /srv/aijudge/config
sudo sh -c 'umask 077; openssl rand -base64 32 > /srv/aijudge/config/restic-password'
sudo chown root:aijudge /srv/aijudge/config/restic-password
sudo chmod 640 /srv/aijudge/config/restic-password

sudo cp deploy/aijudge-restic.env.example /srv/aijudge/config/aijudge-restic.env
sudo $EDITOR /srv/aijudge/config/aijudge-restic.env   # RESTIC_REPOSITORY を自分の target 1 の場所に
sudo chown root:aijudge /srv/aijudge/config/aijudge-restic.env
sudo chmod 640 /srv/aijudge/config/aijudge-restic.env

sudo install -m 0755 deploy/aijudge-restic-backup.sh /usr/local/sbin/aijudge-restic-backup.sh

# 初期化は手動で一度だけ（誤ったパスに気づかず新規リポジトリを作る事故を避ける）
sudo -u aijudge bash -c 'set -a; source /srv/aijudge/config/aijudge-restic.env; set +a; restic init'

sudo cp deploy/systemd/aijudge-restic-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aijudge-restic-backup.timer

# 受け先の検査（週次・#427）。受け先ごとに 1 本。失敗はメールで届く
sudo systemctl enable --now aijudge-restic-check@aijudge-restic.timer
```

`aijudge-config-check` は、受け先の env ファイルがあるのに timer が有効になって
いない系統を NG として通知する（timer の有効化を忘れても何も失敗しないので、ほかの方法では気づけない）。

target 2（オフボックス）を使うときは、同じ形の env ファイルをもう 1 組（別のリポジトリ・別のパスワード）用意し、
別名の timer をもう 1 つ追加すること（ここでは target 1 の手順だけを示す）。
