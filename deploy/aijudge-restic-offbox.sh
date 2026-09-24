#!/usr/bin/env bash
# /srv/aijudge を「オフボックス」の restic リポジトリへバックアップする
# （target 2 / target 3。受け先は EnvironmentFile が決める）。
#
# **target 1 と同じ独立バックアップを取る**（`restic copy` ではない）──
# 各リポジトリが自己完結し、target 1 の破損が伝播しない。データ量が小さい
# ので、読み直しのほうが安い。
#
# RESTIC_REPOSITORY / RESTIC_PASSWORD_FILE は EnvironmentFile（インスタンス毎）で渡す。
# init はここではやらない（誤ったパス指定で空リポジトリを作る事故を避ける。
# target 1 と同じ方針）。
set -euo pipefail
: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY not set}"
: "${RESTIC_PASSWORD_FILE:?RESTIC_PASSWORD_FILE not set}"

# IDE の作業の記録も一緒に取る（target 1 と同じ・aijudge-restic-backup.sh）。
paths=(/srv/aijudge)
if [ -n "${AIJUDGE_ACTIVITY_DIR:-}" ] && [ -d "${AIJUDGE_ACTIVITY_DIR}" ]; then
  paths+=("${AIJUDGE_ACTIVITY_DIR}")
fi
restic backup "${paths[@]}" --tag aijudge-offbox \
  --exclude /srv/aijudge/lost+found \
  --exclude "/srv/aijudge/.Trash-*"
# オフボックスは**本番が壊れても過去 6 ヶ月のどの時点にも戻せる**ように月次を
# 6 本残す（2026-09-24 決定。以前は 12 本）。
#
# **6 ヶ月より長くしない。** 学習者のデータは締切から 6 ヶ月で消す約束で
# （動画・作業の記録）、消したあともバックアップには残る ── ここを延ばすと、
# 学習者に告知した「消えきるまでの最大」（締切から 12 ヶ月）が偽りになる。
# 成績そのものは本番の DB に残り続けるので、学期をまたいだ照会には本番が応える。
restic forget --group-by host,tags --keep-daily 14 --keep-weekly 8 --keep-monthly 6 --prune
