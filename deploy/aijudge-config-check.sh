#!/usr/bin/env bash
# 運用機の構成が、リポジトリが決めたものと合っているかを見る（#260・#261）。
#
# **状態が変わったときだけ通知する。** 毎回送るとアラート疲れを起こし、
# 「無視する癖」がつく ── それが最大の害である
# （`aijudge-llm-primary-check.sh` と同じ作法）。
#
# 見るのは 5 つ。
#
#   1. unit がチェックアウトと同じか（届いているか）
#   2. AI ワーカーが `aijudge.target` の宣言どおり動いているか
#   3. 待ち時間の目安（`AIJUDGE_AI_WORKERS`）が実際の本数と合っているか
#   4. DB の `max_connections` が、いまのプロセス数の最悪値を上回るか
#   5. `/srv/aijudge` が `aijudge` から全部読めるか（バックアップの前提）
#   6. `/usr/local/sbin` のスクリプトがチェックアウトと同じか（届いているか）
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
# IDE の runner と受付終了時の自動提出は、枠を小さく取る（1 件ずつ順に処理する）。
# **`apps/runner/.../cli.py` の RUNNER_POOL_SIZE + RUNNER_MAX_OVERFLOW と揃える。**
RUNNER_PER_PROCESS=3

problems=()

# unit が**実際に持っている**環境変数を読む（#347）。
#
# `systemctl show -p Environment` は **unit の `Environment=` 行しか返さない**
# ── `EnvironmentFile=` で読み込まれた値は含まれず、そちらは
# `EnvironmentFiles` にファイル名が出るだけである。この配備の値はすべて
# `/srv/aijudge/config/aijudge.env` 側にあるので、**全部空として取れていた。**
# 既定値に落ちて 3 番目の検査が永久に NG を出し、4 番目は web を 1 プロセスと
# 数えて接続数を**少なく**見積もっていた（2026-09-20 に判明）。
#
# **動いているプロセスの環境を読む。** ファイルを読むのでは、
# 「書き換えたが再起動していない」を見逃す ── それはまさに、この検査が
# 見つけたい種類のずれである。
_unit_env() {
    local unit="$1" name="$2" pid="" value=""
    pid=$(systemctl show "${unit}" -p MainPID --value 2>/dev/null)
    if [ -n "${pid}" ] && [ "${pid}" != "0" ] && [ -r "/proc/${pid}/environ" ]; then
        value=$(tr '\0' '\n' < "/proc/${pid}/environ" | sed -n "s/^${name}=//p" | tail -1)
    fi
    if [ -z "${value}" ]; then
        value=$(systemctl show "${unit}" -p Environment --value 2>/dev/null \
                | tr ' ' '\n' | sed -n "s/^${name}=//p" | tail -1)
    fi
    printf '%s' "${value}"
}

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

# 2.5 イベントのリレーが動いているか（#328）
#
# **止まっていても採点は完了する。** だから気づけない ── 実際、購読者は
# 書かれていたのに誰も呼んでおらず、`grading.completed` が未送信のまま
# 積み上がって習熟度は 0 件のままだった。動いていないことが画面にも
# ログにも出ない種類の停止なので、ここで訊く。
if ! systemctl is-active --quiet aijudge-relay.service; then
    problems+=("イベントのリレーが動いていない（習熟度が更新されません）")
fi

# 3. 待ち時間の目安が、実際の本数と合っているか
#
# `AIJUDGE_AI_WORKERS` は学習者に出す「あと何分」の計算にしか使わない
# （`position // ai_workers`）が、**本数の 2 つ目の写し**である。ずれると
# 目安が本数の比だけ狂う ── 4 本動いているのに 1 と伝えると 4 倍になる。
# 数値を 1 か所にできない以上、**ずれたことに気づけるようにしておく。**
hint=$(_unit_env aijudge-web.service AIJUDGE_AI_WORKERS)
hint="${hint:-1}"
if [ "${running}" != "0" ] && [ "${hint}" != "${running}" ]; then
    problems+=("AIJUDGE_AI_WORKERS=${hint} が実際の ${running} 本と違う（待ち時間の目安がずれる）")
fi

# 4. 接続数が足りるか
web=$(_unit_env aijudge-web.service AIJUDGE_WEB_WORKERS)
web="${web:-1}"
# web + review 1 + det 1 + ai N + finalize 1
processes=$(( web + 1 + 1 + running + 1 ))
# IDE の runner（K 本）と受付終了時の自動提出（1）。枠が小さいので別に数える。
runners=$(systemctl list-units 'aijudge-runner@*.service' --state=running --no-legend --plain \
          2>/dev/null | wc -l | tr -d ' ')
needed=$(( processes * PER_PROCESS + (runners + 1) * RUNNER_PER_PROCESS ))
limit=$(sudo -u postgres psql -At -c 'show max_connections' 2>/dev/null \
        || psql -At -c 'show max_connections' 2>/dev/null || echo 0)
if [ "${limit}" != "0" ] && [ "${limit}" -lt "${needed}" ]; then
    problems+=("max_connections=${limit} が ${processes} プロセスと runner ${runners} 本の最悪値 ${needed} を下回る")
fi

# 5. バックアップが読めないファイルが無いか（#344）
#
# **restic は 1 ファイル読めないだけで exit 3 で終わる。** スナップショット
# 自体は保存されるので気づきにくいが、`aijudge-restic-backup.sh` は
# `set -e` なので**後段の `restic forget --prune` に到達しない** ── 世代整理が
# 止まったままリポジトリが増え続ける。しかも failed が常態化すると、
# 本物の失敗が埋もれる。
#
# 2026-09-20 に 3 系統が同時に failed になった。原因は環境ファイルの控えが
# 1 つだけ `root:root` で置かれたこと（他は `root:aijudge`）。**置き方が
# 1 回違っただけ**で、バックアップの世代整理が止まっていた。
#
# `find` の `-readable` は**実行中のユーザで判定する**ので、root で走る
# このスクリプトではすべて読めてしまう。`aijudge` に成り代わって訊く。
if [ -d /srv/aijudge ]; then
    unreadable=$(sudo -u aijudge find /srv/aijudge -type f ! -readable -printf '%p\n' \
                 2>/dev/null | head -5)
    if [ -n "${unreadable}" ]; then
        problems+=("aijudge が読めないファイルがある（restic が exit 3 で failed になります）: $(echo "${unreadable}" | tr '\n' ' ')")
    fi
fi

# 6. unit の先のスクリプトが届いているか（#344）
#
# **unit だけ見ても足りない。** `ExecStart` が指す先は `/usr/local/sbin` の
# 写しで、配るようになった（`install-units.sh`）後も、機械を直接触れば
# ずれる ── 1 の検査が unit に対してやっていることを、その先にもやる。
for name in aijudge-restic-backup.sh aijudge-restic-offbox.sh aijudge-db-backup.sh \
            aijudge-pg-basebackup.sh aijudge-storage-check.sh aijudge-llm-primary-check.sh \
            aijudge-notify; do
    src="${REPO_DIR}/deploy/${name}"
    [ -e "${src}" ] || continue
    cmp -s "${src}" "/usr/local/sbin/${name}" || problems+=("スクリプトがずれている: ${name}")
done
if [ -e "${REPO_DIR}/deploy/lib/llm-primary-check.py" ]; then
    cmp -s "${REPO_DIR}/deploy/lib/llm-primary-check.py" \
        /usr/local/lib/aijudge/llm-primary-check.py \
        || problems+=("スクリプトがずれている: lib/llm-primary-check.py")
fi

if [ "${#problems[@]}" -eq 0 ]; then
    NOW=OK
    detail="unit 一致・AI ワーカー ${running} 本・目安 ${hint}・max_connections ${limit}（要 ${needed}）・/srv/aijudge は全部読める・スクリプト一致"
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
