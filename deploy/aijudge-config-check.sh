#!/usr/bin/env bash
# 運用機の構成が、リポジトリが決めたものと合っているかを見る（#260・#261）。
#
# **状態が変わったときだけ通知する。** 毎回送るとアラート疲れを起こし、
# 「無視する癖」がつく ── それが最大の害である
# （`aijudge-llm-primary-check.sh` と同じ作法）。
#
# 見るのは 4 つ。
#
#   1. unit がチェックアウトと同じか（届いているか）
#   2. AI ワーカーが `aijudge.target` の宣言どおり動いているか
#   3. 待ち時間の目安（`AIJUDGE_AI_WORKERS`）が実際の本数と合っているか
#   4. DB の `max_connections` が、いまのプロセス数の最悪値を上回るか
#
# 4 を見るのは、**プロセス数だけ上げて DB を忘れる**のが最も起きやすい
# 壊し方だからである。混んだときに接続が取れずに落ちるので、落ちる瞬間は
# 最も混んでいる瞬間になる。
set -uo pipefail

REPO_DIR="${AIJUDGE_REPO_DIR:-/opt/aijudge}"
STATE=/var/lib/aijudge/config-check.state
# 1 プロセスが取りうる接続数（`packages/persistence/.../engine.py`）。
# **ここを写しているので、engine.py を変えたらここも変える。**
PER_PROCESS=30

problems=()

# 1. unit が届いているか
for unit in "${REPO_DIR}"/deploy/systemd/*.service "${REPO_DIR}"/deploy/systemd/*.target \
            "${REPO_DIR}"/deploy/systemd/*.timer; do
    [ -e "${unit}" ] || continue
    name="$(basename "${unit}")"
    cmp -s "${unit}" "/etc/systemd/system/${name}" || problems+=("unit がずれている: ${name}")
done

# 2. AI ワーカーが宣言どおりか
declared=$(grep -o 'aijudge-worker-ai@[0-9]*\.service' "${REPO_DIR}/deploy/systemd/aijudge.target" \
           2>/dev/null | sort -u | wc -l | tr -d ' ')
running=$(systemctl list-units 'aijudge-worker-ai@*.service' --state=running --no-legend --plain \
          2>/dev/null | wc -l | tr -d ' ')
if [ "${declared}" != "0" ] && [ "${running}" != "${declared}" ]; then
    problems+=("AI ワーカーが ${running} 本（宣言は ${declared} 本）")
fi

# 3. 待ち時間の目安が、実際の本数と合っているか
#
# `AIJUDGE_AI_WORKERS` は学習者に出す「あと何分」の計算にしか使わない
# （`position // ai_workers`）が、**本数の 2 つ目の写し**である。ずれると
# 目安が本数の比だけ狂う ── 4 本動いているのに 1 と伝えると 4 倍になる。
# 数値を 1 か所にできない以上、**ずれたことに気づけるようにしておく。**
hint=$(systemctl show aijudge-web.service -p Environment --value 2>/dev/null \
       | tr ' ' '\n' | sed -n 's/^AIJUDGE_AI_WORKERS=//p')
hint="${hint:-1}"
if [ "${running}" != "0" ] && [ "${hint}" != "${running}" ]; then
    problems+=("AIJUDGE_AI_WORKERS=${hint} が実際の ${running} 本と違う（待ち時間の目安がずれる）")
fi

# 4. 接続数が足りるか
web=$(systemctl show aijudge-web.service -p Environment --value 2>/dev/null \
      | tr ' ' '\n' | sed -n 's/^AIJUDGE_WEB_WORKERS=//p')
web="${web:-1}"
# web + review 1 + det 1 + ai N + finalize 1
processes=$(( web + 1 + 1 + running + 1 ))
needed=$(( processes * PER_PROCESS ))
limit=$(sudo -u postgres psql -At -c 'show max_connections' 2>/dev/null \
        || psql -At -c 'show max_connections' 2>/dev/null || echo 0)
if [ "${limit}" != "0" ] && [ "${limit}" -lt "${needed}" ]; then
    problems+=("max_connections=${limit} が ${processes} プロセスの最悪値 ${needed} を下回る")
fi

if [ "${#problems[@]}" -eq 0 ]; then
    NOW=OK
    detail="unit 一致・AI ワーカー ${running} 本・目安 ${hint}・max_connections ${limit}（要 ${needed}）"
else
    NOW=NG
    detail=$(printf '%s\n' "${problems[@]}")
fi

printf "%s %s\n" "${NOW}" "${detail}"

PREV=$(cat "${STATE}" 2>/dev/null || echo UNKNOWN)
if [ "${NOW}" != "${PREV}" ] && command -v /usr/local/sbin/aijudge-notify >/dev/null 2>&1; then
    {
        echo "host      : $(hostname -f)"
        echo "transition: ${PREV} -> ${NOW}"
        echo
        echo "${detail}"
        echo
        echo "配り直し: sudo systemctl start aijudge-units.service"
        echo "参照    : docs/RUNNING.md「並列度は 4 か所に散っている」"
    } | /usr/local/sbin/aijudge-notify "[$(hostname -s)] aiJudge config ${PREV} -> ${NOW}"
fi
mkdir -p "$(dirname "${STATE}")" 2>/dev/null || true
printf "%s" "${NOW}" > "${STATE}" 2>/dev/null || true

# **タイマーを failed にしない。** 赤いままだと他の障害が埋もれる。
exit 0
