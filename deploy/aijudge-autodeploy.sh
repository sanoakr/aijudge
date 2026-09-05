#!/usr/bin/env bash
# CD（方式 B・pull 型）のポーラー。origin の v* タグを見て、デプロイ済みより
# 新しければ deploy.sh を呼ぶ。**GitHub 側の設定・秘密・inbound は要らない**
# ── サーバが自分で git ls-remote して判断するだけ。
#
# 採用理由（GitHub Actions の自己ホスト runner を使わない理由）と代償
# （最大 5 分の遅延・CI 緑の gating が無い）は ../README.md 参照。
set -euo pipefail

REPO_DIR="${AIJUDGE_REPO_DIR:-/opt/aijudge}"
cd "${REPO_DIR}"

current="$(git describe --tags --exact-match 2>/dev/null || echo none)"
latest="$(git ls-remote --tags --refs origin 'v*' \
          | sed 's#.*refs/tags/##' | sort -V | tail -1)"

if [ -z "${latest}" ]; then
    echo "no v* tags on origin"
    exit 0
fi
if [ "${current}" = "${latest}" ]; then
    exit 0   # 最新。何もしない
fi

echo "autodeploy: ${current} -> ${latest}"
exec "${REPO_DIR}/deploy/deploy.sh" "${latest}"
