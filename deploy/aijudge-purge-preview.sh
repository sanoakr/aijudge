#!/usr/bin/env bash
# 保存期間を過ぎた動画・作業の記録を**下見だけ**して、あればメールする（#427）。
#
# 消すのは人（`aijudge-admin video purge --apply` / `activity purge --apply`）。
# 自動で消さないのは、消去が取り返せないからである。ただし下見まで人の記憶に
# 頼ると、学習者に告知した保存期間（締切から 6 か月）が守られない。
set -uo pipefail
ADMIN=/opt/aijudge/.venv/bin/aijudge-admin

video=$("$ADMIN" video purge 2>&1); vrc=$?
activity=$("$ADMIN" activity purge 2>&1); arc=$?
due=$(printf '%s\n%s\n' "$video" "$activity" \
      | grep -E '保存期間を過ぎた' | grep -oE '[0-9]+ (件|回分)' | grep -cvE '^0 ')
if [ "$vrc" -ne 0 ] || [ "$arc" -ne 0 ] || [ "$due" -gt 0 ]; then
    {
        echo "host: $(hostname -f)"
        echo
        echo "== aijudge-admin video purge（下見） rc=$vrc"
        echo "$video"
        echo
        echo "== aijudge-admin activity purge（下見） rc=$arc"
        echo "$activity"
        echo
        echo "消すときは docs/RUNNING.md の手順で --apply を付けて流す。"
    } | /usr/local/sbin/aijudge-notify "[$(hostname -s)] retention: items past their period"
fi
echo "video rc=$vrc activity rc=$arc due=$due"
exit 0
