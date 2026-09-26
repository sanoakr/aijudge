#!/usr/bin/env bash
# 採点キューの滞留・失敗と、ワーカーの停止を見る。**状態が変わったときだけ**メールする（#426）。
#
# なぜ要るか: キューが詰まっても、ワーカーが死んでも、画面は「採点中」を出し
# 続けるだけで誰にも知らせない。config-check は日次なので、締切直前の詰まりに
# 間に合わない。
set -uo pipefail
STATE=/var/lib/aijudge/queue-check.state
# 最古の「いま取れる」ジョブがこれより待っていたら詰まっている（分）。
# 決定的段階は数秒で終わるので短く、AI 段階は締切前に並ぶので長く取る。
MAX_WAIT_DETERMINISTIC_MIN=${AIJUDGE_QUEUE_MAX_WAIT_DETERMINISTIC_MIN:-10}
MAX_WAIT_AI_MIN=${AIJUDGE_QUEUE_MAX_WAIT_AI_MIN:-60}
: "${AIJUDGE_DATABASE_URL:?AIJUDGE_DATABASE_URL not set}"
DB_URL=$(printf '%s' "$AIJUDGE_DATABASE_URL" | sed 's/+psycopg//')

problems=()

# 1. 滞留と、直近 1 時間の失敗（段階ごと）
rows=$(psql "$DB_URL" -At -F '|' -c "
    select phase,
           count(*) filter (where state = 'queued' and available_at <= now()),
           coalesce(floor(extract(epoch from now() - min(available_at)
               filter (where state = 'queued' and available_at <= now())) / 60), 0),
           count(*) filter (where state = 'failed' and updated_at > now() - interval '1 hour')
    from grading_jobs group by phase" 2>&1) || problems+=("psql: $rows")
while IFS='|' read -r phase queued oldest failed; do
    [ -n "${phase:-}" ] || continue
    limit=$MAX_WAIT_DETERMINISTIC_MIN
    [ "$phase" = "ai" ] && limit=$MAX_WAIT_AI_MIN
    [ "${oldest%.*}" -gt "$limit" ] && problems+=("$phase: ${queued} queued, oldest waiting ${oldest%.*} min (> ${limit})")
    [ "$failed" -gt 0 ] && problems+=("$phase: ${failed} job(s) FAILED in the last hour")
done <<< "${rows}"

# 2. ワーカーとリレー（入れてあるものだけ）
for unit in aijudge-worker-det aijudge-relay $(systemctl list-units 'aijudge-worker-ai@*' 'aijudge-runner@*' --all --plain --no-legend 2>/dev/null | awk '{print $1}'); do
    systemctl is-active --quiet "$unit" || problems+=("$unit is $(systemctl is-active "$unit")")
done

NOW=$([ ${#problems[@]} -eq 0 ] && echo OK || echo NG)
# 同じ NG でも中身が変われば知らせる（滞留から失敗へ、など）。
SIGNATURE="$NOW $(printf '%s\n' "${problems[@]:-}" | sed 's/[0-9]\+/N/g' | sort | tr '\n' ';')"
PREV=$(cat "$STATE" 2>/dev/null || echo UNKNOWN)
printf "%s\n" "$NOW" "${problems[@]:-}"
if [ "$SIGNATURE" != "$PREV" ]; then
    {
        echo "host  : $(hostname -f)"
        echo "state : ${PREV%% *} -> $NOW"
        for line in "${problems[@]:-}"; do [ -n "$line" ] && echo "issue : $line"; done
        echo
        echo "確認: コンソールの「採点の状況」、journalctl -u aijudge-worker-det -u 'aijudge-worker-ai@*' -n 50"
    } | /usr/local/sbin/aijudge-notify "[$(hostname -s)] grading queue ${PREV%% *} -> $NOW"
    printf "%s" "$SIGNATURE" > "$STATE"
fi
exit 0
