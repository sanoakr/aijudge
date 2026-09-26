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
        for line in "${failures[@]:-}"; do echo "failed    : $line"; done
        echo
        echo "確認: systemctl status aijudge-web aijudge-review; journalctl -u aijudge-web -n 50"
    } | /usr/local/sbin/aijudge-notify "[$(hostname -s)] web $PREV -> $NOW"
    printf "%s" "$NOW" > "$STATE"
fi
# 通知はメールで行う。timer を failed にしない（赤いままだと他の障害が埋もれる）。
exit 0
