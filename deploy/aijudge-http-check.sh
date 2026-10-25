#!/usr/bin/env bash
# 学生画面とコンソールの `/login` を外から叩く。**状態が変わったときだけ**メールする（#422）。
#
# なぜ要るか: 2026-09-21、web の起動時の mkdir が失敗して 502 が 4 時間続いた。
# uvicorn の親プロセスが残るので `systemctl` は active のままで、誰も気づかな
# かった（docs/RUNNING.md）。プロセスの状態ではなく、利用者と同じ入口で確かめる。
#
# 見る URL は EnvironmentFile の AIJUDGE_LEARNER_URL（とコンソールの接頭辞）。
# **ホスト名はここに書かない**（このリポジトリは公開物）。
set -uo pipefail
STATE=/var/lib/aijudge/http-check.state
: "${AIJUDGE_LEARNER_URL:?AIJUDGE_LEARNER_URL not set}"
BASE="${AIJUDGE_LEARNER_URL%/}"
CONSOLE="${BASE}${AIJUDGE_CONSOLE_ROOT_PREFIX:-}"

failures=()
for url in "${BASE}/login" "${CONSOLE}/login"; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$url" || true)
    [ "$code" = "200" ] || failures+=("$url -> ${code:-000}")
done
NOW=$([ ${#failures[@]} -eq 0 ] && echo OK || echo NG)
PREV=$(cat "$STATE" 2>/dev/null || echo UNKNOWN)
printf "%s %s\n" "$NOW" "${failures[*]:-}"
if [ "$NOW" != "$PREV" ]; then
    {
        echo "host      : $(hostname -f)"
        echo "transition: $PREV -> $NOW"
        for line in "${failures[@]:-}"; do [ -n "$line" ] && echo "failed    : $line"; done
        echo
        # 影響とやること（2026-10-25）。確認のコマンドを 1 行並べるだけでは、どちらの
        # 画面が落ちていて何を打てば直るのかを毎回探すことになった。
        echo "== 影響 =="
        if [ "$NOW" = OK ]; then
            echo "学生の画面とコンソールの /login が 200 を返しています。対応は不要です。"
        else
            echo "上の URL が開けません（000 は接続できない・502 はアプリが応答しない）。学生か教員が画面を使えません。授業中・締切前なら最優先で直す。"
            echo
            echo "== やること（運用機で） =="
            echo "1. どちらが落ちているか: systemctl status aijudge-web aijudge-review --no-pager（学生の URL なら aijudge-web、コンソールの URL なら aijudge-review）"
            echo "2. 起き直す: sudo systemctl restart aijudge-web（または aijudge-review）"
            echo "3. active なのに 502 なら、プロセスが残ったまま応答していない（2026-09-21 の形）。journal の最後の例外を見る: journalctl -u aijudge-web -n 100 --no-pager"
            echo "4. 両方 000 なら、nginx か DB: systemctl status nginx postgresql --no-pager"
            echo
            echo "== 直ったら =="
            echo "この検査（5 分ごと）が「web NG -> OK」のメールを送ります。"
        fi
    } | /usr/local/sbin/aijudge-notify "[$(hostname -s)] web $PREV -> $NOW"
    printf "%s" "$NOW" > "$STATE"
fi
# 通知はメールで行う。timer を failed にしない（赤いままだと他の障害が埋もれる）。
exit 0
