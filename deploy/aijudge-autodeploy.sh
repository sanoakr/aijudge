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
if [ -s "${PIN_FILE}" ]; then
    latest="$(head -n 1 "${PIN_FILE}")"
    echo "autodeploy: pinned to ${latest} by ${PIN_FILE}"
else
    latest="$(git ls-remote --tags --refs origin 'v*' \
              | sed 's#.*refs/tags/##' | sort -V | tail -1)"
fi

if [ -z "${latest}" ]; then
    echo "no v* tags on origin"
    exit 0
fi
if [ "${current}" = "${latest}" ]; then
    exit 0   # 最新。何もしない
fi

echo "autodeploy: ${current} -> ${latest}"
exec "${REPO_DIR}/deploy/deploy.sh" "${latest}"
