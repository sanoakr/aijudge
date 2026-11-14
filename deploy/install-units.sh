#!/usr/bin/env bash
# `deploy/systemd/` の unit と、unit が呼ぶスクリプトを機械へ配る。**root で走る。**
#
# `aijudge-units.service` から呼ばれる（`deploy.sh` はそれを start するだけ
# ── デプロイは aijudge ユーザで走り、ファイルの書き込み権限を持たない）。
#
# なぜ要るか: これが無かったあいだ、`deploy/systemd/` は `bootstrap.sh` を
# 走らせた最初の一度しか機械に届いていなかった。2026-09-12 に測ったとき
# 11 個中 9 個がずれており、**書いてあるのに効いていない設定**が溜まって
# いた（AI ワーカー 4 本・ログの名札・サンドボックス化・LimitNOFILE）。
#
# ## 配るのは「署名を確かめたタグの中身」だけ（#417）
#
# 以前はチェックアウト（`/opt/aijudge`、所有者 aijudge）の中身をそのまま root が
# 配っていた。aijudge ユーザ（＝web の RCE）は、このスクリプトも、配られる unit
# （`User=root` を書ける）も書き換えられ、この unit の起動も polkit で許されて
# いる ── **aijudge から root への道**だった。
#
# いまは次のとおりにする。
#
# 1. チェックアウトから読むのは `.git/HEAD` の**中身（コミットのハッシュ）だけ**。
#    git をチェックアウトに対して実行しない（`.git/config` に `core.fsmonitor`
#    などを仕込めば、root の git が任意のコマンドを実行する）
# 2. root 所有のミラー（`MIRROR`）を origin から更新し、そのハッシュを指す
#    `v*` タグを探す
# 3. タグの署名を root 所有の許可リスト（`SIGNERS`）で確かめる。署名が無い・
#    知らない鍵なら**何も配らずに失敗する**（OnFailure でメールが届く）
# 4. 配る中身は、そのタグから `git archive` で取り出したもの
#
# aijudge に選べるのは「どの署名済みタグか」だけで、中身は選べない。
#
# ## 配ってよいタグには下限と順序がある（2026-11-14）
#
# 「署名済みならどれでも配る」だと、署名済みのタグのうち**この仕組みより前の版**
# も配れてしまう。古い版の unit やスクリプトは、上の 1〜4 を満たしていない。
# そこで 2 つを足した。
#
# 5. **下限**: `FLOOR_COMMIT`（#417 の修正）を祖先に持つタグだけを配る。値は
#    このスクリプト（root 所有の写し）に書いてあり、aijudge が書ける場所から読まない
# 6. **最後に配った版より古いタグは断る**: 配り終えたタグとコミットを root 所有の
#    `STATE` に記録し、次からはそのコミットを祖先に持つタグだけを配る。切り戻しは、
#    root が `ROLLBACK` にそのタグ名を書いたときだけ通す（手順は `deploy/README.md`
#    の「unit の配布と署名」）。記録がまだ無い機械は、下限だけで判断する
#
# **このスクリプト自身は `/usr/local/sbin/aijudge-install-units` の写しが走る**
# （配るものに自分も含める）。最初の 1 回だけは、人が中身を確かめて入れる ──
# 手順は `deploy/README.md` の「unit の配布と署名」。
set -euo pipefail
umask 022

REPO_DIR="${AIJUDGE_REPO_DIR:-/opt/aijudge}"
MIRROR="${AIJUDGE_RELEASE_MIRROR:-/var/lib/aijudge-release/repo.git}"
SIGNERS="${AIJUDGE_RELEASE_SIGNERS:-/etc/aijudge/allowed_signers}"
# 配ってよいタグの下限（#417 の修正のコミット）。これを祖先に持たないタグは配らない。
FLOOR_COMMIT="${AIJUDGE_RELEASE_FLOOR:-41c300d760b6b876b55993e040c21cf308bd43c2}"
# 最後に配った版の記録（「タグ コミット」の 1 行）と、root が書く切り戻しの指定。
STATE="${AIJUDGE_UNITS_STATE:-/var/lib/aijudge-release/installed-units}"
ROLLBACK="${AIJUDGE_UNITS_ROLLBACK:-/etc/aijudge/units-rollback-to}"
# 配る先。環境変数で変えられるのはテストのため（`aijudge-units.service` は渡さない）。
DST="${AIJUDGE_UNITS_DST:-/etc/systemd/system}"

# root の git が、aijudge の書ける設定を読まないようにする。
export GIT_CONFIG_NOSYSTEM=1
export HOME=/root
mirror() {
    git --git-dir="${MIRROR}" \
        -c gpg.format=ssh -c gpg.ssh.allowedSignersFile="${SIGNERS}" "$@"
}

# root 以外が書ける場所に置かれたファイルは信じない。
require_root_owned() {
    local path="$1" owner mode
    owner=$(stat -c '%U' "${path}")
    mode=$(stat -c '%a' "${path}")
    if [ "${owner}" != "root" ] || [ $(( 0${mode} & 022 )) -ne 0 ]; then
        echo "${path} は root 所有かつ他者が書けない状態でなければならない（${owner} ${mode}）" >&2
        exit 1
    fi
}
for path in "${MIRROR}" "${SIGNERS}"; do
    [ -e "${path}" ] || {
        echo "${path} がありません（初回の設定: deploy/README.md「unit の配布と署名」）" >&2
        exit 1
    }
    require_root_owned "${path}"
done

# 1. チェックアウトのコミット（**ファイルの中身を読むだけ**）
# シンボリックリンクは辿らない（root だけが読めるファイルを指させて、中身を
# ログに漏らさせない）。中身もログに出さない。
head_file="${REPO_DIR}/.git/HEAD"
if [ -L "${head_file}" ] || [ ! -f "${head_file}" ]; then
    echo "${head_file} が通常のファイルではありません" >&2
    exit 1
fi
commit=$(head -c 41 "${head_file}" | tr -d '\n')
if ! printf '%s' "${commit}" | grep -Eq '^[0-9a-f]{40}$'; then
    echo "チェックアウトがタグの上にありません（HEAD がコミットのハッシュではない）" >&2
    exit 1
fi

# 2〜3. ミラーを更新し、そのコミットの署名済みタグを確かめる
mirror fetch --quiet --prune --tags origin
tag=$(mirror tag --points-at "${commit}" --list 'v*' | sort -V | tail -1)
if [ -z "${tag}" ]; then
    echo "コミット ${commit} を指す v* タグが origin にありません" >&2
    exit 1
fi
if ! mirror verify-tag "${tag}" >/dev/null 2>&1; then
    echo "タグ ${tag} の署名を確かめられません（署名が無いか、${SIGNERS} に無い鍵）" >&2
    exit 1
fi
echo "verified ${tag} (${commit})"

# 5. 下限。署名済みでも、下限のコミットを含まない版は配らない。
if ! mirror merge-base --is-ancestor "${FLOOR_COMMIT}" "${commit}" 2>/dev/null; then
    echo "タグ ${tag} は配ってよい版の下限（${FLOOR_COMMIT:0:12}）より古いので配りません" >&2
    exit 1
fi

# 6. 最後に配った版より古いタグは断る。切り戻しは root の明示（ROLLBACK）があるときだけ。
if [ -e "${STATE}" ]; then
    require_root_owned "${STATE}"
    read -r installed_tag installed_commit < "${STATE}" || true
    if ! printf '%s' "${installed_commit:-}" | grep -Eq '^[0-9a-f]{40}$'; then
        echo "${STATE} の記録が読めません（「タグ コミット」の 1 行のはず）" >&2
        exit 1
    fi
    if [ "${installed_commit}" != "${commit}" ] \
            && ! mirror merge-base --is-ancestor "${installed_commit}" "${commit}" 2>/dev/null; then
        rollback_to=""
        if [ -e "${ROLLBACK}" ]; then
            require_root_owned "${ROLLBACK}"
            rollback_to=$(head -n 1 "${ROLLBACK}" | tr -d '[:space:]')
        fi
        if [ "${rollback_to}" != "${tag}" ]; then
            echo "タグ ${tag} は最後に配った ${installed_tag} より古いので配りません（戻すなら root が ${ROLLBACK} に ${tag} と書く）" >&2
            exit 1
        fi
        echo "rollback to ${tag} as written in ${ROLLBACK} (installed was ${installed_tag})"
    fi
fi

# 4. タグの中身を取り出す
work=$(mktemp -d)
trap 'rm -rf "${work}"' EXIT
mirror archive "${tag}" deploy | tar -x -C "${work}"
DEPLOY="${work}/deploy"
SRC="${DEPLOY}/systemd"

# unit の `ExecStart` が指すスクリプト（#344）。**unit だけ配っても動かない** ──
# 運用機ではこれらが手で置かれたままリポジトリに無く、機械を再構築しても
# 復元できなかった。取り込んだ以上、配るのもここでやる。
#
# **このスクリプト自身と config-check も配る**（#417）。root で走るものを
# チェックアウトから直接実行しない。`aijudge-autodeploy.sh` はチェックアウトから
# 走るが、aijudge ユーザで走るので構わない。`aijudge-vision-check.sh` は手で
# 走らせる診断である。
SBIN_SCRIPTS=(
    aijudge-restic-backup.sh
    aijudge-restic-offbox.sh
    aijudge-db-backup.sh
    aijudge-pg-basebackup.sh
    aijudge-storage-check.sh
    aijudge-llm-primary-check.sh
    aijudge-http-check.sh
    aijudge-queue-check.sh
    aijudge-restic-check.sh
    aijudge-purge-preview.sh
    aijudge-config-check.sh
    aijudge-notify
    aijudge-failure-guide
)
SBIN="${AIJUDGE_UNITS_SBIN:-/usr/local/sbin}"
LIBDST="${AIJUDGE_UNITS_LIB:-/usr/local/lib/aijudge}"

[ -d "${SRC}" ] || { echo "タグ ${tag} に deploy/systemd がありません" >&2; exit 1; }

changed=0
for unit in "${SRC}"/*.service "${SRC}"/*.target "${SRC}"/*.timer; do
    [ -e "${unit}" ] || continue
    name="$(basename "${unit}")"
    # **中身が同じなら触らない。** 毎回書き換えると mtime だけが動き、
    # 「何がいつ変わったか」がログから読めなくなる。
    if ! cmp -s "${unit}" "${DST}/${name}"; then
        install -m 0644 -o root -g root "${unit}" "${DST}/${name}"
        echo "unit updated: ${name}"
        changed=1
    fi
done

# スクリプトを配る。**中身が同じなら触らない**のは unit と同じ理由。
#
# `daemon-reload` は要らない（systemd が読むのは unit であって、その先の
# ファイルではない）。次にタイマーが起きたときから新しいものが走る。
for name in "${SBIN_SCRIPTS[@]}"; do
    script="${DEPLOY}/${name}"
    [ -e "${script}" ] || continue
    if ! cmp -s "${script}" "${SBIN}/${name}"; then
        install -m 0755 -o root -g root "${script}" "${SBIN}/${name}"
        echo "script updated: ${name}"
    fi
done

# このスクリプト自身（次からはこの写しが走る）。
if ! cmp -s "${DEPLOY}/install-units.sh" "${SBIN}/aijudge-install-units"; then
    install -m 0755 -o root -g root "${DEPLOY}/install-units.sh" "${SBIN}/aijudge-install-units"
    echo "script updated: aijudge-install-units"
fi

if [ -e "${DEPLOY}/lib/llm-primary-check.py" ]; then
    install -d -m 0755 "${LIBDST}"
    if ! cmp -s "${DEPLOY}/lib/llm-primary-check.py" "${LIBDST}/llm-primary-check.py"; then
        install -m 0644 -o root -g root \
            "${DEPLOY}/lib/llm-primary-check.py" "${LIBDST}/llm-primary-check.py"
        echo "script updated: lib/llm-primary-check.py"
    fi
fi

if [ "${changed}" = "1" ]; then
    systemctl daemon-reload
    # **enable を貼り直す。** `Wants=` を足しただけでは、既に enable 済みの
    # target の `.wants/` に symlink は増えない ── AI ワーカー @2..@4 が
    # 宣言されているのに起動していなかったのがこれである。
    systemctl reenable aijudge.target
    echo "daemon-reload / reenable 済み"
else
    echo "unit に変更なし"
fi

# 配り終えたときだけ記録する。途中で落ちれば記録は前の版のまま残る。
printf '%s %s\n' "${tag}" "${commit}" > "${STATE}.tmp"
chmod 0644 "${STATE}.tmp"
mv "${STATE}.tmp" "${STATE}"
echo "recorded ${tag} in ${STATE}"
