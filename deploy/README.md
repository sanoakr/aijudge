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

設計の背景（なぜ pull 型 timer で GitHub Actions からの push 型にしないか等）は
`docs/design/00_システム設計方針と構築計画.md` と、運用元の非公開デプロイ手順書を参照。
