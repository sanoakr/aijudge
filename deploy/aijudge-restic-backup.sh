#!/usr/bin/env bash
# /srv/aijudge を restic でバックアップする（§3.8「バックアップ」参照）。
# RESTIC_REPOSITORY と RESTIC_PASSWORD_FILE は EnvironmentFile 経由で渡す
# （restic 自身がこの環境変数名を見る。AIJUDGE_ プレフィックスは付けない）。
#
# 初期化（restic init）はここではやらない。誤ったマウント先やパス指定に
# 気づかないまま新規の空リポジトリを作ってしまう事故を避けるため、
# 初回は運用者が手で一度だけ `restic init` を実行する前提とする。
set -euo pipefail
: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY not set}"
: "${RESTIC_PASSWORD_FILE:?RESTIC_PASSWORD_FILE not set}"

# lost+found（ファイルシステム内部用、root 以外読めない）と、マウントポイント
# 直下に作られがちな .Trash-*（各ユーザーのゴミ箱。aijudge データではない）は
# aijudge から読めず警告終了（exit 3）の原因になるので除外する。
restic backup /srv/aijudge --tag aijudge-auto \
  --exclude /srv/aijudge/lost+found \
  --exclude '/srv/aijudge/.Trash-*'
restic forget --keep-daily 14 --keep-weekly 8 --prune
