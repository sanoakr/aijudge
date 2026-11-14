#!/usr/bin/env bash
# リリースのタグが、許可した鍵で署名されているかを確かめる。**リポジトリの中で走らせる。**
#
#   deploy/lib/verify-release-tag.sh v1.2.3   # 0 = 署名を確かめた / 1 = 確かめられない
#
# 確かめるのは、`refs/tags/<tag>` が注釈タグであること、その署名が許可した鍵の
# ものであること、タグオブジェクトの中の名前が `<tag>` と同じであること。
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

# 名前の解決をタグに限る（同じ名前のブランチなどを確かめないように）。
REF="refs/tags/${TAG}"

# 軽量タグ（署名できない）も、知らない鍵の署名も、ここで落ちる。
if ! git -c gpg.format=ssh -c gpg.ssh.allowedSignersFile="${SIGNERS}" \
        verify-tag "${REF}" >/dev/null 2>&1; then
    echo "タグ ${TAG} の署名を確かめられません（署名が無いか、${SIGNERS} に無い鍵）" >&2
    exit 1
fi

# **署名したのは「その名前のタグ」であることも確かめる**（2026-11-14）。
#
# `git verify-tag` が見るのはタグオブジェクトの署名だけで、オブジェクトの中の
# `tag` 行（署名したときの名前）と参照名の一致は見ない。origin に push できる者が
# 古い署名済みのタグオブジェクトを `refs/tags/v99.0.0` として出し直すと、署名は
# 通り、autodeploy は参照名で新旧を決めるので**古い版を最新として入れていた**
# （`deployed-tag` に偽の名前が残り、以後の本物のリリースは #564 の検査で断られる）。
# 名前は署名の内側にあるので、ここが一致すれば参照名も署名者が付けたものである。
if [ "$(git cat-file -t "${REF}")" != "tag" ]; then
    echo "タグ ${TAG} は注釈タグではありません" >&2
    exit 1
fi
# ヘッダは最初の空行まで。本文の中の `tag ` で始まる行は読まない。
# sed は入力を最後まで読む（途中で閉じると pipefail で git 側が落ちる）。
signed_name=$(git cat-file tag "${REF}" | sed -n '1,/^$/s/^tag //p')
if [ "${signed_name}" != "${TAG}" ]; then
    echo "タグ ${TAG} の中の名前が ${signed_name:-（無し）} です。名前を付け替えたタグは信じません" >&2
    exit 1
fi
