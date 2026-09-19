#!/usr/bin/env bash
# プライマリ LLM の状態を確認し、**状態が変わったときだけ**メールする。
# 毎回送るとアラート疲れを起こし「無視する癖」がつく ── それが最大の害なので、
# OK→NG と NG→OK の遷移だけを通知する。
set -uo pipefail
STATE=/var/lib/aijudge/llm-primary-check.state
PY=/opt/aijudge/.venv/bin/python
CHECK=/usr/local/lib/aijudge/llm-primary-check.py

cd /opt/aijudge || exit 3
OUT=$("$PY" "$CHECK" 2>&1); RC=$?
NOW=$([ $RC -eq 0 ] && echo OK || echo NG)
PREV=$(cat "$STATE" 2>/dev/null || echo UNKNOWN)
printf "%s rc=%s %s\n" "$NOW" "$RC" "$OUT"
if [ "$NOW" != "$PREV" ]; then
  {
    echo "host      : $(hostname -f)"
    echo "transition: $PREV -> $NOW"
    echo "exit code : $RC   (1=プライマリ不能・フォールバックで稼働中 / 2=両系不能)"
    echo "detail    : $OUT"
    echo
    echo "参照: docs/design/disk-and-recovery-plan.md §3.9.5-A"
    echo "確認: curl -sS http://localhost:11435/api/tags"
  } | /usr/local/sbin/aijudge-notify "[$(hostname -s)] LLM primary $PREV -> $NOW"
  printf "%s" "$NOW" > "$STATE"
fi
# **タイマーを failed にしない**（通知はメールで行う。赤いままだと他の障害が埋もれる）。
exit 0
