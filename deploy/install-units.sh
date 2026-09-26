#!/usr/bin/env bash
# `deploy/systemd/` の unit を `/etc/systemd/system/` へ配る。**root で走る。**
#
# `aijudge-units.service` から呼ばれる（`deploy.sh` はそれを start するだけ
# ── デプロイは aijudge ユーザで走り、ファイルの書き込み権限を持たない）。
#
# なぜ要るか: これが無かったあいだ、`deploy/systemd/` は `bootstrap.sh` を
# 走らせた最初の一度しか機械に届いていなかった。2026-09-12 に測ったとき
# 11 個中 9 個がずれており、**書いてあるのに効いていない設定**が溜まって
# いた（AI ワーカー 4 本・ログの名札・サンドボックス化・LimitNOFILE）。
set -euo pipefail

REPO_DIR="${AIJUDGE_REPO_DIR:-/opt/aijudge}"
SRC="${REPO_DIR}/deploy/systemd"
DST=/etc/systemd/system

# unit の `ExecStart` が指すスクリプト（#344）。**unit だけ配っても動かない** ──
# 運用機ではこれらが手で置かれたままリポジトリに無く、機械を再構築しても
# 復元できなかった。取り込んだ以上、配るのもここでやる。
#
# **`/opt/aijudge` から直接実行される道具は入れない。** `aijudge-config-check.sh`
# と `aijudge-autodeploy.sh` は unit がチェックアウトのパスを指しており、
# `aijudge-vision-check.sh` は手で走らせる診断である。写しを増やすと、
# どちらが動いているのか分からなくなる。
SBIN_SCRIPTS=(
    aijudge-restic-backup.sh
    aijudge-restic-offbox.sh
    aijudge-db-backup.sh
    aijudge-pg-basebackup.sh
    aijudge-storage-check.sh
    aijudge-llm-primary-check.sh
    aijudge-http-check.sh
    aijudge-queue-check.sh
    aijudge-notify
)
SBIN=/usr/local/sbin
LIBDST=/usr/local/lib/aijudge

[ -d "${SRC}" ] || { echo "unit の置き場所がありません: ${SRC}" >&2; exit 1; }

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
    script="${REPO_DIR}/deploy/${name}"
    [ -e "${script}" ] || continue
    if ! cmp -s "${script}" "${SBIN}/${name}"; then
        install -m 0755 -o root -g root "${script}" "${SBIN}/${name}"
        echo "script updated: ${name}"
    fi
done

if [ -e "${REPO_DIR}/deploy/lib/llm-primary-check.py" ]; then
    install -d -m 0755 "${LIBDST}"
    if ! cmp -s "${REPO_DIR}/deploy/lib/llm-primary-check.py" "${LIBDST}/llm-primary-check.py"; then
        install -m 0644 -o root -g root \
            "${REPO_DIR}/deploy/lib/llm-primary-check.py" "${LIBDST}/llm-primary-check.py"
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
