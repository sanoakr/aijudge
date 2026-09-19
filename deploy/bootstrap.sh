#!/usr/bin/env bash
# 初回セットアップ。systemd unit・nginx・polkit を配置して有効化する。
# **証明書の取得はしない**（Let's Encrypt 等は導入側の既存運用に従う）。
# **aijudge.target は enable するが start しない** ── コード・DB・証明書が
# 揃ってから `deploy.sh` で上げる。
#
#   set -gx AIJUDGE_HOSTNAME judge.example.ac.jp
#   sudo -E deploy/bootstrap.sh
#
# 詳しい前提・手順は README.md 参照。
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "root で実行してください（sudo -E deploy/bootstrap.sh）" >&2
    exit 1
fi
: "${AIJUDGE_HOSTNAME:?AIJUDGE_HOSTNAME を設定してください（例: judge.example.ac.jp）}"

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== systemd unit を配置 =="
install -m 644 "${DEPLOY_DIR}"/systemd/*.service "${DEPLOY_DIR}"/systemd/*.timer \
    "${DEPLOY_DIR}"/systemd/*.target /etc/systemd/system/
systemctl daemon-reload

echo "== nginx vhost を配置（${AIJUDGE_HOSTNAME}） =="
sed "s/__AIJUDGE_HOSTNAME__/${AIJUDGE_HOSTNAME}/g" \
    "${DEPLOY_DIR}/nginx/aijudge.conf.template" \
    > "/etc/nginx/sites-available/${AIJUDGE_HOSTNAME}"
echo "  → /etc/nginx/sites-available/${AIJUDGE_HOSTNAME}"
echo "  （sites-enabled への symlink・証明書取得・nginx -t は導入側で行うこと）"

echo "== polkit ルールを配置 =="
install -m 644 "${DEPLOY_DIR}/polkit/49-aijudge.rules" /etc/polkit-1/rules.d/

# **unit から呼ばれるスクリプトも配る**（#344）。unit だけ配っても
# `ExecStart` の先が無ければ何も動かない ── 運用機ではこれらが手で置かれた
# まま**リポジトリに無かった**ので、機械を再構築しても復元できなかった。
#
# デプロイ時の配布（`install-units.sh`）は unit だけを見ている。ここで配るのは
# 初回構築のぶんで、既存機の更新は別に判断する（中身が変わるとバックアップの
# 挙動が変わるので、一致を確かめてから配布に載せること）。
echo "== /usr/local/sbin のスクリプトを配置 =="
install -m 755 \
    "${DEPLOY_DIR}/aijudge-restic-backup.sh" \
    "${DEPLOY_DIR}/aijudge-restic-offbox.sh" \
    "${DEPLOY_DIR}/aijudge-db-backup.sh" \
    "${DEPLOY_DIR}/aijudge-pg-basebackup.sh" \
    "${DEPLOY_DIR}/aijudge-storage-check.sh" \
    "${DEPLOY_DIR}/aijudge-llm-primary-check.sh" \
    "${DEPLOY_DIR}/aijudge-notify" \
    /usr/local/sbin/
install -d -m 755 /usr/local/lib/aijudge
install -m 644 "${DEPLOY_DIR}/lib/llm-primary-check.py" /usr/local/lib/aijudge/
# 検査が書く状態ファイルの置き場所（無いと遷移が毎回 UNKNOWN になる）。
install -d -m 755 -o aijudge -g aijudge /var/lib/aijudge

echo "== aijudge.target を enable（start はしない） =="
systemctl enable aijudge.target

# **構成のずれを見る検査を入れる**（#260・#261）。unit がデプロイで配られる
# ようになった後も、機械を直接触れば ずれる ── 誰も何も言わない状態に
# 戻さないため、1 日 1 回見て、状態が変わったときだけ通知する。
echo "== 構成のずれの検査を enable =="
systemctl enable aijudge-config-check.timer

cat <<'EOF'

完了。次の手順（README.md も参照）:

  1. /srv/aijudge/config/aijudge.env を deploy/aijudge.env.example から作成する。
  2. nginx の sites-enabled へ symlink を張り、証明書を取得して nginx -t / reload。
  3. 初回デプロイ:  sudo -u aijudge deploy/deploy.sh <tag>
  4. CD を有効化:    sudo systemctl enable --now aijudge-autodeploy.timer
  5. 構成の検査:     sudo systemctl start aijudge-config-check.service  （1 度出力を見る）
EOF
