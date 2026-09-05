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

echo "== aijudge.target を enable（start はしない） =="
systemctl enable aijudge.target

cat <<'EOF'

完了。次の手順（README.md も参照）:

  1. /srv/aijudge/config/aijudge.env を deploy/aijudge.env.example から作成する。
  2. nginx の sites-enabled へ symlink を張り、証明書を取得して nginx -t / reload。
  3. 初回デプロイ:  sudo -u aijudge deploy/deploy.sh <tag>
  4. CD を有効化:    sudo systemctl enable --now aijudge-autodeploy.timer
EOF
