#!/usr/bin/env bash
# PITR の土台となる物理ベースバックアップを取り、不要になった WAL を掃除する。
#
# なぜ必要か: WAL アーカイブだけでは復旧できない。WAL 再生の土台は**物理**
# バックアップであり、`pg_dump`（論理）は土台にならない。「ベースバックアップ
# ＋ それ以降の WAL」で任意時刻まで戻せる（PITR）。
#
# 併存する `pg_dump -Fc`（日次）は役割が違う ── PG のバージョンを跨いで
# 別マシンへ移植できる論理復旧点。**どちらも捨てない。**
set -euo pipefail

BASE=/srv/aijudge/backups/base
WAL=/srv/aijudge/backups/wal
BIN=/usr/lib/postgresql/18/bin
# 残す世代数。**2 世代あれば「最新が壊れていた」に耐えられる。**
KEEP=2

TS=$(date +%Y%m%d-%H%M%S)
DST="$BASE/$TS"

# pg_basebackup は空ディレクトリを要求する。postgres が書ける所有にしておく。
install -d -o postgres -g aijudge -m 2750 "$DST"

# -Ft      : tar 形式（restic の重複排除が効くよう -z は付けない。restic 側で圧縮される）
# -X stream: 取得中に生成された WAL を同梱する。アーカイブ側の欠けに耐える
# -c fast  : 即時チェックポイント（タイマー実行なので待たない）
runuser -u postgres -- "$BIN/pg_basebackup" -D "$DST" -Ft -X stream -c fast \
  -l "aijudge weekly $TS"

# restic は aijudge ユーザで走るので、生成物を group 読み取り可にする。
chmod 0640 "$DST"/* 2>/dev/null || true

# 古い世代を落とす（KEEP 世代を残す）
ls -1d "$BASE"/*/ 2>/dev/null | sort | head -n -"$KEEP" | xargs -r rm -rf

# 残した中で最古のベースが必要とする WAL より古いものだけを掃除する。
# **これより新しい WAL を消すと PITR の連続性が切れる**ので、判定は
# backup_label に従う。
OLDEST=$(ls -1d "$BASE"/*/ 2>/dev/null | sort | head -1 || true)
if [ -n "${OLDEST:-}" ] && [ -f "$OLDEST/base.tar" ]; then
  # backup_label の書式は版によって違う。PG 18 は
  #   START WAL LOCATION: 0/3000028 (file 000000010000000000000003)
  # で、古い版にある「START WAL FILE:」行は存在しない。**両方に対応する。**
  START_WAL=$(tar -xOf "$OLDEST/base.tar" backup_label 2>/dev/null | awk '
    /^START WAL FILE:/     { print $4; exit }
    /^START WAL LOCATION:/ { gsub(/[()]/, ""); print $NF; exit }
  ')
  if [ -n "${START_WAL:-}" ]; then
    echo "archivecleanup: keeping WAL from $START_WAL onward (base $OLDEST)"
    runuser -u postgres -- "$BIN/pg_archivecleanup" "$WAL" "$START_WAL"
  else
    echo "WARN: backup_label から START WAL FILE を取得できず、WAL 掃除を見送った" >&2
  fi
fi

echo "basebackup done: $DST"
du -sh "$BASE" "$WAL"
