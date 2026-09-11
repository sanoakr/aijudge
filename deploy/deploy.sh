#!/usr/bin/env bash
# 1 つのタグをデプロイする。手動実行でも CD（aijudge-autodeploy.sh）からでも
# 同じスクリプトを通す ── 別の手順を用意すると、片方でしか踏まない手順が
# 必ず出てくる。
#
#   sudo -u aijudge /opt/aijudge/deploy/deploy.sh v1.2.3
#
# 設計の背景は ../README.md と docs/design/00_システム設計方針と構築計画.md。
set -euo pipefail

TAG="${1:?tag required}"
REPO_DIR="${AIJUDGE_REPO_DIR:-/opt/aijudge}"
cd "${REPO_DIR}"

# 二重起動・手動実行との衝突を防ぐ。
exec 9>/run/lock/aijudge-deploy.lock
flock -n 9 || { echo "deploy already running"; exit 0; }

git fetch --tags --prune
git rev-parse "refs/tags/${TAG}^{commit}" >/dev/null   # タグの実在を先に確かめる

# デプロイ直前のダンプ（ロールバックの保険）。無ければスキップするだけにして、
# バックアップ未設定の環境でもデプロイ自体は止めない。
if command -v aijudge-db-backup.sh >/dev/null 2>&1; then
    aijudge-db-backup.sh
fi

git checkout --detach "refs/tags/${TAG}"
uv sync --frozen --extra dev
uv run --project "${REPO_DIR}" alembic upgrade head

# **unit ファイルも配る**（#261）。ここが無かったので、`deploy/systemd/` は
# `bootstrap.sh` を走らせた最初の一度しか機械に届いていなかった ── 以後どれ
# だけ直しても反映されない。2026-09-12 に測ったとき 11 個中 9 個がずれており、
# AI ワーカー 4 本の宣言も、ログの名札も、systemd のサンドボックス化も、
# 書いてあるのに効いていなかった。
#
# **root の仕事は root の service にやらせる。** このスクリプトは aijudge
# ユーザで走り、polkit が許しているのは unit の起動停止だけである
# （`deploy/polkit/49-aijudge.rules`）── `/etc/systemd/system/` への書き込みも
# `daemon-reload` も許されていない。**権限を広げるのではなく**、root で走る
# oneshot を 1 つ足して、それを「起動する」形にした。起動は既に許されている。
#
# 無い機械では黙って飛ばす ── この仕組みより前に入れた機械でも、デプロイ
# 自体は従来どおり通る（そこは 1 度だけ手で入れる）。
if systemctl list-unit-files aijudge-units.service >/dev/null 2>&1; then
    systemctl start aijudge-units.service || echo "unit の配布に失敗（続行）"
fi

# migration の後に restart。**ワーカーも必ず入れ替える** ── 古いワーカーが
# 新コードの採点行を読めずに詰まった事故が過去に 2 回ある
# （docs/RUNNING.md #60/#80）。
systemctl restart aijudge.target
if systemctl list-units 'aijudge-worker-ai@*' --state=loaded -q | grep -q .; then
    systemctl restart 'aijudge-worker-ai@*'
fi
systemctl try-restart aijudge-finalize.timer

# 疎通確認。落ちていたら非ゼロで終わり、timer のログに残る。
# **ホスト名はここに書かない** — EnvironmentFile の AIJUDGE_LEARNER_URL を使う
# （このリポジトリは公開物で、機関固有の値を含めない）。
if [ -n "${AIJUDGE_LEARNER_URL:-}" ]; then
    curl -fsS --max-time 10 "${AIJUDGE_LEARNER_URL%/}/login" >/dev/null
fi
systemctl is-active --quiet aijudge-web aijudge-review aijudge-worker-det

echo "deployed ${TAG} ($(git rev-parse --short HEAD))"
