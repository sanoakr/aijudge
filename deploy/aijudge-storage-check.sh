#!/usr/bin/env bash
# ストレージと WAL アーカイブの健全性を確認し、**状態が変わったときだけ**メールする。
#
# 監視する対象と理由:
#  1. / の空き ── archive_command が失敗し続けると WAL が pg_wal に溜まり、
#     **/ が満杯になって DB が停止する**。本番停止に直結するので最優先。
#  2. /srv/aijudge の空き ── データ正本。ここが埋まると提出を受けられない。
#  3. /work の空き ── restic target 1 と動画。
#  4. **WAL アーカイブがいま失敗しているか** ── 1 の先行指標。
#     last_failed_time > last_archived_time なら「いま失敗している」。
#     ※ アーカイブの「鮮度」は監視しない。DB が無活動なら WAL は生成されず、
#       archive_timeout でも切り替わらないため、鮮度は誤警報の元になる。
#
# 毎回送るとアラート疲れで無視する癖がつく（それが最大の害）ので、遷移時のみ通知する。
set -uo pipefail

STATE=/var/lib/aijudge/storage-check.state
THRESHOLD=85
PROBLEMS=()

check_fs() {
  local mp="$1"
  local use
  use=$(df --output=pcent "$mp" 2>/dev/null | tail -1 | tr -dc '0-9')
  [ -z "$use" ] && { PROBLEMS+=("$mp: df が読めない"); return; }
  if [ "$use" -ge "$THRESHOLD" ]; then
    PROBLEMS+=("$mp: 使用率 ${use}% (閾値 ${THRESHOLD}%)")
  fi
  echo "  $mp ${use}%"
}

echo "filesystems:"
check_fs /
check_fs /srv/aijudge
check_fs /work

# WAL アーカイブが「いま失敗しているか」
ARCH=$(runuser -u postgres -- psql -Atc \
  "select coalesce(archived_count,0) || ' ' || coalesce(failed_count,0) || ' ' || case when last_failed_time is not null and (last_archived_time is null or last_failed_time > last_archived_time) then 'FAILING' else 'ok' end from pg_stat_archiver" 2>/dev/null)
if [ -z "$ARCH" ]; then
  PROBLEMS+=("pg_stat_archiver を読めない（PostgreSQL 停止中の可能性）")
else
  echo "archiver: $ARCH  (archived failed status)"
  case "$ARCH" in
    *FAILING*) PROBLEMS+=("WAL アーカイブが失敗している: $ARCH") ;;
  esac
fi

if [ "${#PROBLEMS[@]}" -eq 0 ]; then NOW=OK; else NOW=NG; fi
PREV=$(cat "$STATE" 2>/dev/null || echo UNKNOWN)
echo "state: $PREV -> $NOW"

if [ "$NOW" != "$PREV" ]; then
  {
    echo "host      : $(hostname -f)"
    echo "transition: $PREV -> $NOW"
    echo
    if [ "${#PROBLEMS[@]}" -gt 0 ]; then
      echo "問題:"
      printf '  - %s\n' "${PROBLEMS[@]}"
    else
      echo "すべて正常に戻りました。対応は不要です。"
    fi
    echo
    df -h / /srv/aijudge /work 2>/dev/null
    # 影響とやること（2026-10-25）。参照先を 1 行書くだけでは、急ぐのか・何を消して
    # よいのかを毎回探すことになった。**問題の種類ごとに出す。**
    if [ "${#PROBLEMS[@]}" -gt 0 ]; then
      all=$(printf '%s\n' "${PROBLEMS[@]}")
      echo
      echo "== 影響 =="
      echo "/ が満杯になると DB が止まり、/srv/aijudge が満杯になると提出を受けられません。WAL アーカイブの失敗は / を埋める先行指標です。使用率が 95% を超えていれば最優先で直す。"
      echo
      echo "== やること（運用機で） =="
      n=1
      step() { echo "${n}. $1"; n=$((n + 1)); }
      if printf '%s' "$all" | grep -q 'WAL\|pg_stat_archiver'; then
        step "WAL アーカイブの失敗の原因を見る: journalctl -u postgresql@18-main --no-pager | grep -i archive | tail -20（受け先の空き・権限が多い）"
        step "溜まっている量を見る: sudo du -sh /var/lib/postgresql/18/main/pg_wal（pg_wal の中は手で消さない。アーカイブが通れば PostgreSQL が自分で消す）"
      fi
      if printf '%s' "$all" | grep -q '使用率'; then
        step "何が増えたかを見る: sudo du -xh --max-depth=2 <埋まっているマウント> | sort -h | tail -15"
        step "/srv/aijudge なら、保存期間を過ぎた動画・作業の記録を下見する: sudo -u aijudge sh -c 'set -a; . /srv/aijudge/config/aijudge.env; set +a; exec /opt/aijudge/.venv/bin/aijudge-admin video purge'（消すのは --apply。docs/RUNNING.md「動画の保存期間」）"
        step "/ なら、journal と古い DB ダンプ: journalctl --disk-usage; ls -lh /srv/aijudge/backups/db | tail"
      fi
      if printf '%s' "$all" | grep -q 'df が読めない\|PostgreSQL 停止中'; then
        step "マウントか PostgreSQL が落ちている: findmnt /srv/aijudge /work; systemctl status postgresql --no-pager"
      fi
      step "背景と判断の基準: docs/design/disk-and-recovery-plan.md §3.9.18"
      echo
      echo "== 直ったら =="
      echo "この検査が「storage NG -> OK」のメールを送ります。"
    fi
  } | /usr/local/sbin/aijudge-notify "[$(hostname -s)] storage $PREV -> $NOW"
  printf '%s' "$NOW" > "$STATE"
fi

# **タイマーを failed にしない**（通知はメールで行う。赤いままだと他の障害が埋もれる）。
exit 0
