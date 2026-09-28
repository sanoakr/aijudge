#!/usr/bin/env bash
# CD（方式 B・pull 型）のポーラー。origin の v* タグを見て、デプロイ済みより
# 新しければ deploy.sh を呼ぶ。**GitHub 側の設定・秘密・inbound は要らない**
# ── サーバが自分で git ls-remote して判断するだけ。
#
# 採用理由（GitHub Actions の自己ホスト runner を使わない理由）と代償
# （最大 5 分の遅延・CI 緑の gating が無い）は ../README.md 参照。
set -euo pipefail

REPO_DIR="${AIJUDGE_REPO_DIR:-/opt/aijudge}"
STATE_FILE="${AIJUDGE_DEPLOY_STATE:-/var/lib/aijudge/deployed-tag}"
# 切り戻し用の固定（#425）。この版を入れたままにする。中身はタグ 1 行。
# 消せば最新に戻る。手順は ../README.md の「切り戻し」。
PIN_FILE="${AIJUDGE_DEPLOY_PIN:-/var/lib/aijudge/deploy-pin}"
cd "${REPO_DIR}"

# **最後まで通った版**で判定する（#421）。記録がまだ無い機械（この仕組みより
# 前に入れたもの）だけ、作業ツリーのタグに落とす。
if [ -s "${STATE_FILE}" ]; then
    current="$(head -n 1 "${STATE_FILE}")"
else
    current="$(git describe --tags --exact-match 2>/dev/null || echo none)"
fi
# **署名を確かめたタグの中で最新のもの**を選ぶ（2026-09-29）。origin の最新を
# そのまま選ぶと、署名の無いタグが一番新しいときに deploy.sh が毎周期断られ、
# その後に出た署名済みの版まで入らなくなる（失敗の知らせも 5 分おきに届く）。
# 飛ばしたタグはログに名指しする ── 黙って飛ばすと、出したつもりの版が入らない
# 理由が分からない。
VERIFY="${REPO_DIR}/deploy/lib/verify-release-tag.sh"
git fetch --quiet --tags --prune origin
if [ -s "${PIN_FILE}" ]; then
    latest="$(head -n 1 "${PIN_FILE}")"
    echo "autodeploy: pinned to ${latest} by ${PIN_FILE}"
    "${VERIFY}" "${latest}"   # 固定した版も、署名が無ければ入れない
else
    latest=""
    skipped=()
    while read -r tag; do
        [ -n "${tag}" ] || continue
        if "${VERIFY}" "${tag}" 2>/dev/null; then
            latest="${tag}"
            break
        fi
        skipped+=("${tag}")
    done < <(git tag --list 'v*' | sort -rV)
    if [ "${#skipped[@]}" -gt 0 ]; then
        echo "autodeploy: 署名を確かめられないタグを飛ばしました: ${skipped[*]}" >&2
    fi
fi

if [ -z "${latest}" ]; then
    echo "no signed v* tags on origin"
    exit 0
fi
if [ "${current}" = "${latest}" ]; then
    exit 0   # 最新。何もしない
fi
# **固定していないのに古い版へは戻らない**（#564）。`--prune` なので、origin で
# 最新のタグが消えると 1 つ前の署名済みの版が「最新」になる ── 誤って消しても、
# push できる者が消しても、自動で戻る。migration は戻らないので、新しいスキーマの
# 上で古いコードが動く。意図した切り戻しは固定（${PIN_FILE}）でする。
if [ ! -s "${PIN_FILE}" ] && [ "${current}" != "none" ] \
        && [ "$(printf '%s\n%s\n' "${current}" "${latest}" | sort -V | tail -n 1)" = "${current}" ]; then
    echo "autodeploy: ${latest} はデプロイ済みの ${current} より古いので入れません（戻すなら ${PIN_FILE}）" >&2
    exit 0
fi

echo "autodeploy: ${current} -> ${latest}"
exec "${REPO_DIR}/deploy/deploy.sh" "${latest}"
