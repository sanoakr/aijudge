#!/usr/bin/env bash
# リリースのタグが、許可した鍵で署名されているかを確かめる。**リポジトリの中で走らせる。**
#
#   deploy/lib/verify-release-tag.sh v1.2.3   # 0 = 署名を確かめた / 1 = 確かめられない
#
# ## なぜ要るか（2026-09-29）
#
# 署名を確かめていたのは unit とスクリプトを配る側（`install-units.sh`・root）
# だけで、**コードのデプロイ**（`deploy.sh`・aijudge）は origin の最新の `v*`
# タグを確かめずにチェックアウトして動かしていた。GitHub にタグを push できる
# 者（または漏れたトークン）なら、署名の無いコードを運用機で動かせた。実際に
# 署名の無い v1.37.0 がそのまま入った（unit の配布だけが失敗し「続行」された）。
#
# 許可リストは `install-units.sh` と同じもの（root 所有の
# `/etc/aijudge/allowed_signers`）を使う。**デプロイする者が書き換えられる
# 許可リストは信じない** ── 書けるなら、その者が任意の鍵を足せる。
set -euo pipefail

TAG="${1:?tag required}"
SIGNERS="${AIJUDGE_RELEASE_SIGNERS:-/etc/aijudge/allowed_signers}"

if [ ! -f "${SIGNERS}" ] || [ ! -r "${SIGNERS}" ]; then
    echo "${SIGNERS} がありません（初回の設定: deploy/README.md「unit の配布と署名」）" >&2
    exit 1
fi
if [ -w "${SIGNERS}" ] && [ "$(id -u)" -ne 0 ]; then
    echo "${SIGNERS} をこの利用者（$(id -un)）が書き換えられます。署名の確認に使えません" >&2
    exit 1
fi

# 軽量タグ（署名できない）も、知らない鍵の署名も、ここで落ちる。
if ! git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile="${SIGNERS}" \
        verify-tag "${TAG}" >/dev/null 2>&1; then
    echo "タグ ${TAG} の署名を確かめられません（署名が無いか、${SIGNERS} に無い鍵）" >&2
    exit 1
fi
