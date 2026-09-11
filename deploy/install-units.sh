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
