#!/usr/bin/env bash
# aijudge DB を pg_dump -Fc で /srv/aijudge/backups/db/ に取り、14 日より
# 古いものを消す。OS ユーザ aijudge が peer 認証で dump する（所有者なので可）。
#
# **デプロイ直前にも呼ばれる**（`deploy.sh` のロールバックの保険）。
set -euo pipefail
dir=/srv/aijudge/backups/db
ts=$(date +%Y%m%d-%H%M%S)
out="$dir/aijudge-$ts.dump"
umask 027
pg_dump -Fc --file="$out" aijudge
find "$dir" -maxdepth 1 -name 'aijudge-*.dump' -mtime +14 -delete
echo "wrote $out ($(du -h "$out" | cut -f1))"
