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
# IDE の作業の記録（ADR 0023）。**本体は /srv の外**（動画と同じストレージ、
# `/work/aijudge/activity`）にあるので、置き場所が設定されていれば一緒に取る。
# 本番が壊れても、成績への問い合わせに作業の記録を示せるようにする。動画は
# 取らない（大きさの検討が済んでいない・docs/RUNNING.md）。
paths=(/srv/aijudge)
if [ -n "${AIJUDGE_ACTIVITY_DIR:-}" ] && [ -d "${AIJUDGE_ACTIVITY_DIR}" ]; then
  paths+=("${AIJUDGE_ACTIVITY_DIR}")
fi
# 試験中の画面の静止画（作業の記録の下の `stills/`）は**取らない**（ADR 0027 §5・
# 動画と同じ 2026-09-25 の決定）。通知や他のアプリが写り込むので、消したあとに
# バックアップに残すと、学習者に告知した保存期間が偽りになる。
restic backup "${paths[@]}" --tag aijudge-auto \
  --exclude /srv/aijudge/lost+found \
  --exclude '/srv/aijudge/.Trash-*' \
  --exclude 'stills'
# **`--group-by host,tags`**。restic の既定は「対象のパスの組」ごとに世代を数える
# ので、対象に作業の記録を足した日に古い組ができ、その組は新しいスナップショットが
# 来ないまま最後の世代が残り続ける（保持期間を過ぎても消えない）。タグで束ねる。
restic forget --group-by host,tags --keep-daily 14 --keep-weekly 8 --prune
