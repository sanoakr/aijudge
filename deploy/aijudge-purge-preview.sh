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
        # やること（2026-10-25）。手順の場所だけでは、何を打てばよいのかを探すことになった。
        echo "== 影響 =="
        if [ "$vrc" -ne 0 ] || [ "$arc" -ne 0 ]; then
            echo "下見そのものが失敗しました（rc が 0 でない）。保存期間を過ぎたものがあっても分かりません。"
        else
            echo "学習者に告知した保存期間を過ぎた動画・作業の記録があります。消すまで告知が守られません。今週中に消す。"
        fi
        echo
        echo "== やること（運用機で） =="
        echo "1. 上の下見の一覧を読み、消してよいことを確かめる（消すと取り返せない）"
        echo "2. 動画を消す: sudo -u aijudge sh -c 'set -a; . /srv/aijudge/config/aijudge.env; set +a; exec $ADMIN video purge --apply'"
        echo "3. 作業の記録を消す: sudo -u aijudge sh -c 'set -a; . /srv/aijudge/config/aijudge.env; set +a; exec $ADMIN activity purge --apply'"
        echo "4. rc が 0 でなければ、上の出力のエラーを見て直す"
        echo "背景: docs/RUNNING.md「動画の保存期間」「IDE の作業の記録の保存期間」"
        echo
        echo "== 直ったら =="
        echo "消せば、次の下見（日次）からこのメールは来ません。"
    } | /usr/local/sbin/aijudge-notify "[$(hostname -s)] retention: items past their period"
fi
echo "video rc=$vrc activity rc=$arc due=$due"
exit 0
