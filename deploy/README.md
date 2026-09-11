# デプロイ資材（`deploy/`）

自分の機関で aiJudge を動かすためのテンプレート一式。**このリポジトリは公開物**なので、
ここに機関固有の値（実ホスト名・実 IP・実アカウント名など）は一切書かない。
機関固有の値はすべて `/srv/aijudge/config/aijudge.env`（サーバ側・git 管理外）に置く。

具体的な導入手順・実際の値は、導入する側が非公開のドキュメントで管理すること
（例: このリポジトリの運用元は `docs/design/elite-deployment-proposal.md` を
未追跡のローカルファイルとして持っている）。

## 中身

| パス | 役割 |
|---|---|
| `bootstrap.sh` | 初回セットアップ（systemd unit・nginx・polkit の配置、有効化） |
| `deploy.sh` | 1 つのタグをデプロイする（手動 / CD 共通の心臓部） |
| `aijudge-autodeploy.sh` | origin の `v*` タグを見て、新しければ `deploy.sh` を呼ぶ（CD のポーラー） |
| `systemd/` | unit ファイル一式（`aijudge.target` が web/review/worker-* を束ねる） |
| `nginx/aijudge.conf.template` | リバースプロキシの雛形。`AIJUDGE_HOSTNAME` を置換して使う |
| `polkit/49-aijudge.rules` | `aijudge` グループが sudo なしで unit を起動停止できるようにする |
| `aijudge.env.example` | `EnvironmentFile` の雛形。値を埋めて `/srv/aijudge/config/aijudge.env` に置く |
| `aijudge-restic-backup.sh` | `/srv/aijudge` を restic でバックアップするスクリプト（`/usr/local/sbin/` に置く） |
| `aijudge-restic.env.example` | restic 専用の `EnvironmentFile` の雛形。パスワードを本体の env から隔離する |
| `journald/aijudge.conf` | 運用ログの保存期間とディスク上限（`/etc/systemd/journald.conf.d/` に置く） |

## 前提（`docs/RUNNING.md` と共通）

- 実行 owner は system ユーザ `aijudge`（nologin）。管理操作をする人間は `aijudge` グループに
  所属し、polkit 経由で sudo なしに unit を操作する。
- コードは `/opt/aijudge` に clone（読み取り専用 deploy key で足りる。CD は push しない）。
- 提出物・DB ダンプ・restic ターゲットなどのデータは `/srv/aijudge` 配下（`EnvironmentFile` もここ）。
- PostgreSQL はローカルソケット。SQLite は使わない（複数ワーカーが行ロックを要る）。

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

`bootstrap.sh` がやること:

1. `deploy/systemd/*` を `/etc/systemd/system/` へコピーし `daemon-reload`。
2. `deploy/nginx/aijudge.conf.template` の `__AIJUDGE_HOSTNAME__` を
   `$AIJUDGE_HOSTNAME` に置換して `/etc/nginx/sites-available/` へ配置
   （**証明書の取得はしない** — Let's Encrypt 等は導入側の既存運用に従う）。
3. `deploy/polkit/49-aijudge.rules` を `/etc/polkit-1/rules.d/` へ配置。
4. `aijudge.target` を enable するが、**start はしない**
   （初回はコード・DB・証明書が揃ってから `deploy.sh` で上げる）。

## 通常運用

```fish
# 手動デプロイ（CI が緑になったタグで）
sudo -u aijudge /opt/aijudge/deploy/deploy.sh v1.2.3

# CD を有効化（5 分ごとに origin の v* タグを見に行く）
sudo systemctl enable --now aijudge-autodeploy.timer

# 監視
journalctl -u aijudge-autodeploy -f
```

### リリースのとき ── `pyproject` と `uv.lock` を必ず揃える

タグを切るコミットでは、**版を 2 か所とも上げる**。

```fish
# pyproject.toml の version を上げ、ロックファイルを追随させる
uv lock
git add pyproject.toml uv.lock
git commit -m "chore: release 0.44.0"
git tag v0.44.0 && git push origin main --tags
```

**片方だけ上げると CD が黙って止まる。** `deploy.sh` の `uv sync --frozen` は
ロックを書き換えないが、その前にサーバで一度でも `uv sync` が走っていると
`uv.lock` の自分自身の版だけが書き換わり、作業ツリーが dirty になる。次からは

```
error: Your local changes to the following files would be overwritten by checkout:
        uv.lock
```

で `git checkout` が中断し、**`deploy.sh` はそこで終わる** ── アプリは動き続け、
学生にも教員にも何も起きないので、気づかない。実際に v0.35.1 でこれが起き、
**8 リリース分（v0.36.0 〜 v0.43.0）がデプロイされないまま 2 日走っていた**。

気づく側の手当てはこれ。CD は失敗を journal にしか残さないので、たまに見る。

```fish
systemctl is-failed aijudge-autodeploy.service        # failed なら止まっている
sudo -u aijudge git -C /opt/aijudge describe --tags   # 実際に動いている版
git ls-remote --tags --refs origin 'v*' | sed 's#.*/##' | sort -V | tail -1
```

詰まったときは、生成物である `uv.lock` を捨ててからデプロイし直す。

```fish
sudo -u aijudge git -C /opt/aijudge checkout -- uv.lock
sudo -u aijudge /opt/aijudge/deploy/deploy.sh v0.44.0
```

### ログを読む（ADR 0016）

運用では `AIJUDGE_LOG_FORMAT=json` を設定する（1 行 1 イベント）。
unit には `SyslogIdentifier` が付いているので、サービス単位で引ける。

```fish
# 採点ワーカーの失敗だけ
journalctl -u aijudge-worker-ai@1 -o cat | jq 'select(.level == "ERROR")'

# **1 つの提出について、web とワーカーの両方の行を集める。**
# 突き合わせの鍵は submission_id ── これが無かったので、#60 / #80 では
# 画面から「採点が遅い」としか見えなかった（docs/RUNNING.md）。
journalctl -t aijudge-web -t aijudge-worker-det -t aijudge-worker-ai1 -o cat \
  | jq 'select(.submission_id == "SUB-ID")'

# 1 リクエストの中で起きたこと（学生の問い合わせに付いてくる X-Request-ID から）
journalctl -t aijudge-web -o cat | jq 'select(.request_id == "REQ-ID")'
```

保存期間は 90 日（`journald/aijudge.conf`）。**成績に関わる「誰が何を変えたか」は
ここには無い** ── それは DB の監査ログに残り、DB ダンプごと restic で守られる。
運用ログは消えてよい層である。

```fish
sudo install -m 0644 /opt/aijudge/deploy/journald/aijudge.conf \
    /etc/systemd/journald.conf.d/aijudge.conf
sudo systemctl restart systemd-journald
```

設計の背景（なぜ pull 型 timer で GitHub Actions からの push 型にしないか等）は
`docs/design/00_システム設計方針と構築計画.md` と、運用元の非公開デプロイ手順書を参照。

## バックアップ（restic、target 1 = オンボックス）

`bootstrap.sh` はこれを自動化しない（DB ダンプの `aijudge-db-backup.{service,timer}` 同様、
初回投入は運用者が手で行う一回限りの操作のため）。

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
```

target 2（オフボックス）は同じ形の env ファイルをもう 1 組（別リポジトリ・別パスワード）用意し、
別名の timer をもう一つ足すこと（v1 では target 1 のみをここに含める）。
