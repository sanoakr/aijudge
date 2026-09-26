#!/usr/bin/env bash
# restic の受け先を検査する（#427）。**失敗は OnFailure でメールになる。**
#
# - `restic check` でリポジトリの整合性を見る。`--read-data-subset` で中身も
#   一部読む（全部読むと数時間かかる。毎週違う部分が当たるので、月単位で全体に近づく）
# - 最新スナップショットの鮮度を見る。バックアップの timer が止まっていても
#   ここで気づく（timer の失敗は timer 自身が知らせるが、「走っていない」は知らせない）
#
# 受け先は EnvironmentFile が決める（RESTIC_REPOSITORY / RESTIC_PASSWORD_FILE）。
set -euo pipefail
: "${RESTIC_REPOSITORY:?RESTIC_REPOSITORY not set}"
: "${RESTIC_PASSWORD_FILE:?RESTIC_PASSWORD_FILE not set}"
SUBSET=${AIJUDGE_RESTIC_READ_SUBSET:-2%}
# オンボックスは 4 時間ごと、オフボックスは日次なので、日次に合わせて 36 時間。
MAX_AGE_HOURS=${AIJUDGE_RESTIC_MAX_AGE_HOURS:-36}

restic check --read-data-subset="${SUBSET}"

latest=$(restic snapshots --latest 1 --json | python3 -c '
import json, sys
snaps = json.load(sys.stdin)
print(max(s["time"] for s in snaps) if snaps else "")
')
if [ -z "${latest}" ]; then
    echo "no snapshots in ${RESTIC_REPOSITORY}" >&2
    exit 1
fi
age_hours=$(python3 -c '
import sys
from datetime import datetime, timezone
t = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
print(int((datetime.now(timezone.utc) - t).total_seconds() // 3600))
' "${latest}")
echo "latest snapshot ${latest} (${age_hours} h ago)"
if [ "${age_hours}" -gt "${MAX_AGE_HOURS}" ]; then
    echo "the latest snapshot is ${age_hours} h old (> ${MAX_AGE_HOURS} h); is the backup timer running?" >&2
    exit 1
fi
