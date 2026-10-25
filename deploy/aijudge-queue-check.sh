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
# 書き起こしの列（#627）は決定的段階でも LLM を呼ぶので、AI 段階と同じに取る。
MAX_WAIT_DETERMINISTIC_MIN=${AIJUDGE_QUEUE_MAX_WAIT_DETERMINISTIC_MIN:-10}
MAX_WAIT_AI_MIN=${AIJUDGE_QUEUE_MAX_WAIT_AI_MIN:-60}
MAX_WAIT_TRANSCRIPTION_MIN=${AIJUDGE_QUEUE_MAX_WAIT_TRANSCRIPTION_MIN:-$MAX_WAIT_AI_MIN}
: "${AIJUDGE_DATABASE_URL:?AIJUDGE_DATABASE_URL not set}"
DB_URL=$(printf '%s' "$AIJUDGE_DATABASE_URL" | sed 's/+psycopg//')

problems=()

# 1. 滞留と、直近 1 時間の失敗（段階と列ごと。default の列は段階名だけで出す）
rows=$(psql "$DB_URL" -At -F '|' -c "
    select case when lane = 'default' then phase else phase || '/' || lane end,
           count(*) filter (where state = 'queued' and available_at <= now()),
           coalesce(floor(extract(epoch from now() - min(available_at)
               filter (where state = 'queued' and available_at <= now())) / 60), 0),
           count(*) filter (where state = 'failed' and updated_at > now() - interval '1 hour')
    from grading_jobs group by 1" 2>&1) || problems+=("psql: $rows")
while IFS='|' read -r phase queued oldest failed; do
    [ -n "${phase:-}" ] || continue
    limit=$MAX_WAIT_DETERMINISTIC_MIN
    [ "$phase" = "ai" ] && limit=$MAX_WAIT_AI_MIN
    [ "${phase#*/}" = "transcription" ] && limit=$MAX_WAIT_TRANSCRIPTION_MIN
    [ "${oldest%.*}" -gt "$limit" ] && problems+=("$phase: ${queued} queued, oldest waiting ${oldest%.*} min (> ${limit})")
    [ "$failed" -gt 0 ] && problems+=("$phase: ${failed} job(s) FAILED in the last hour")
done <<< "${rows}"

# 2. ワーカーとリレー（入れてあるものだけ）
for unit in aijudge-worker-det aijudge-relay $(systemctl list-units 'aijudge-worker-ai@*' 'aijudge-worker-transcribe@*' 'aijudge-runner@*' --all --plain --no-legend 2>/dev/null | awk '{print $1}'); do
    systemctl is-active --quiet "$unit" || problems+=("$unit is $(systemctl is-active "$unit")")
done

NOW=$([ ${#problems[@]} -eq 0 ] && echo OK || echo NG)

# メールに書く「影響とやること」（2026-10-25）。**見つけた問題の種類ごとに出す** ──
# 確認のコマンドを 1 行並べるだけでは、読んだ人が急ぐのか・何を打てば直るのかを
# 毎回探すことになった。
guide() {
    local all
    all=$(printf '%s\n' "${problems[@]:-}")
    if [ "$NOW" = OK ]; then
        echo "== 影響 =="
        echo "採点キューは捌けていて、ワーカーもすべて動いています。対応は不要です。"
        return
    fi
    echo "== 影響 =="
    echo "提出の採点が遅れているか、止まっています。学生には「採点中」のまま見えます。授業中・締切前なら最優先で直す。"
    echo
    echo "== やること（運用機で） =="
    local n=1
    step() { echo "${n}. $1"; n=$((n + 1)); }
    if printf '%s' "$all" | grep -q '^psql:'; then
        step "DB を読めていません。PostgreSQL を確かめる: systemctl status postgresql（止まっていれば学生の画面も使えない）"
    fi
    if printf '%s' "$all" | grep -q ' is '; then
        step "止まっている unit（上の issue の '... is inactive/failed'）を起き直す: sudo systemctl restart <unit 名>"
        step "すぐまた止まるなら原因を見る: journalctl -u <unit 名> -n 100 --no-pager"
    fi
    if printf '%s' "$all" | grep -q 'oldest waiting'; then
        step "ワーカーが動いているのに滞留しているなら、締切前の集中か LLM の遅れ。ai・transcription の列なら LLM の応答を見る: curl -sS --max-time 10 ${AIJUDGE_LLM_BASE_URL:-<AIJUDGE_LLM_BASE_URL>}/api/tags（主系・従系は aijudge-llm-primary-check のメール）"
        step "集中なら待てば捌ける。5 分ごとに検査していて、滞留の中身が変われば再びメールが来る。捌けなければワーカーを足す（docs/RUNNING.md「並列度を決める値は複数の場所にある」）"
    fi
    if printf '%s' "$all" | grep -q 'FAILED'; then
        step "失敗したジョブの原因を見る: journalctl -u aijudge-worker-det -u 'aijudge-worker-transcribe@*' -u 'aijudge-worker-ai@*' -p warning -n 100 --no-pager"
        step "コンソールの提出一覧で状態が「採点できませんでした」の提出を確かめ、原因（課題の設定・LLM）を直してから採点し直す（docs/RUNNING.md「確定済みも含めて採点し直す」）"
    fi
    echo
    echo "== 直ったら =="
    echo "この検査（5 分ごと）が「grading queue NG -> OK」のメールを送ります。"
}
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
        guide
    } | /usr/local/sbin/aijudge-notify "[$(hostname -s)] grading queue ${PREV%% *} -> $NOW"
    printf "%s" "$SIGNATURE" > "$STATE"
fi
exit 0
