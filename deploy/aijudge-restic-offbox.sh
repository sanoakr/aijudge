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

restic backup /srv/aijudge --tag aijudge-offbox \
  --exclude /srv/aijudge/lost+found \
  --exclude "/srv/aijudge/.Trash-*"
# オフボックスは**学期をまたいだ成績照会に応えられるよう**月次を 12 本残す。
restic forget --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune
