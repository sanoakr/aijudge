#!/usr/bin/env bash
# プライマリ LLM の状態を確認し、**状態が変わったときだけ**メールする。
# 毎回送るとアラート疲れを起こし「無視する癖」がつく ── それが最大の害なので、
# OK→NG と NG→OK の遷移だけを通知する。
#
# **フォールバック不能（NG3）だけは、連続して起きたときに通知する**。従系（学外の
# slab-llm）は OS アップデートや再起動で数十分止まるのが普通で、主系が生きている間は
# 採点に影響しない。1 回の NG3 で毎回メールすると、本物の障害が埋もれる。
# 主系の停止（NG1）と両系の停止（NG2）は今までどおり即時に通知する。
set -uo pipefail
# 既定は運用機の実パス。テストでは環境変数で差し替える。
STATE=${AIJUDGE_LLM_CHECK_STATE:-/var/lib/aijudge/llm-primary-check.state}
STREAK_FILE=${AIJUDGE_LLM_CHECK_STREAK:-/var/lib/aijudge/llm-primary-check.ng3-streak}
PY=${AIJUDGE_LLM_CHECK_PY:-/opt/aijudge/.venv/bin/python}
CHECK=${AIJUDGE_LLM_CHECK_SCRIPT:-/usr/local/lib/aijudge/llm-primary-check.py}
NOTIFY=${AIJUDGE_LLM_CHECK_NOTIFY:-/usr/local/sbin/aijudge-notify}
APP_DIR=${AIJUDGE_LLM_CHECK_DIR:-/opt/aijudge}
# NG3 を何回続けて確認したら通知するか（30 分ごとの確認で 2 回 = 約 1 時間）
NG3_CONFIRM=${AIJUDGE_LLM_NG3_CONFIRM:-2}

cd "$APP_DIR" || exit 3
OUT=$("$PY" "$CHECK" 2>&1); RC=$?
# **状態は終了コードそのもの**（#426）。OK/NG の 2 値だと、「主系停止（1）」から
# 「両系停止（2）」への悪化が同じ NG のままで、通知されなかった。
NOW=$([ $RC -eq 0 ] && echo OK || echo "NG$RC")
PREV=$(cat "$STATE" 2>/dev/null || echo UNKNOWN)
# 状態の名前（番号だけだと意味が分からない）。件名と本文に添える。
label() {
  case $1 in
    OK) echo "OK" ;;
    NG1) echo "NG1 (primary down, fallback serving)" ;;
    NG2) echo "NG2 (both down)" ;;
    NG3) echo "NG3 (fallback down, primary serving)" ;;
    *) echo "$1" ;;
  esac
}

# NG3 の連続回数。NG3 以外を見たら数え直す。
STREAK=$(cat "$STREAK_FILE" 2>/dev/null || echo 0)
case $STREAK in ''|*[!0-9]*) STREAK=0 ;; esac
if [ "$NOW" = NG3 ]; then STREAK=$((STREAK + 1)); else STREAK=0; fi
printf "%s" "$STREAK" > "$STREAK_FILE"

printf "%s rc=%s %s\n" "$NOW" "$RC" "$OUT"
if [ "$NOW" = NG3 ] && [ "$PREV" != NG3 ] && [ "$STREAK" -lt "$NG3_CONFIRM" ]; then
  # 状態ファイルは触らない（=「最後に通知した状態」のまま）。回復すれば通知なしで終わる。
  printf "NG3 pending %s/%s (not notified yet)\n" "$STREAK" "$NG3_CONFIRM"
elif [ "$NOW" != "$PREV" ]; then
  {
    echo "host      : $(hostname -f)"
    echo "transition: $(label "$PREV") -> $(label "$NOW")"
    echo "exit code : $RC   (1=プライマリ不能・フォールバックで稼働中 / 2=両系不能 / 3=フォールバック不能)"
    echo "detail    : $OUT"
    [ "$NOW" = NG3 ] && echo "streak    : $STREAK checks in a row"
    echo
    # 影響とやること（2026-10-25）。状態の番号と参照先だけでは、採点が止まっている
    # のか・どちらの機械を見ればよいのかを毎回探すことになった。
    echo "== 影響 =="
    case $NOW in
      OK)  echo "主系が答えていて、従系も答えます。対応は不要です。" ;;
      NG1) echo "主系が答えず、従系（学外）で採点しています。採点は進みますが遅く、従系も止まれば AI の採点が止まります。今日中に主系を直す。" ;;
      NG2) echo "主系も従系も答えません。AI の観点と画像の書き起こしが止まり、その提出は合計点が出ません。授業中・締切前なら最優先で直す。" ;;
      NG3) echo "従系が答えません。主系が採点しているので、いまは影響がありません。主系が止まると AI の採点も止まります。" ;;
      *)   echo "検査の結果を判定できませんでした（detail を見る）。" ;;
    esac
    if [ "$NOW" != OK ]; then
      echo
      echo "== やること =="
      # URL は環境ファイルの値で埋める（**ホスト名をリポジトリに書かない**）。
      primary=${AIJUDGE_LLM_BASE_URL:-<AIJUDGE_LLM_BASE_URL>}
      fallback=${AIJUDGE_LLM_FALLBACK_BASE_URL:-<AIJUDGE_LLM_FALLBACK_BASE_URL>}
      case $NOW in
        NG1|NG2)
          echo "1. 主系の応答を見る（運用機で）: curl -sS --max-time 10 ${primary%/}/api/tags"
          echo "2. 答えないなら、主系（${primary}）のサーバを起き直す。プロキシ（PAIR など）なら、その下のノードが見えているかも確かめる"
          echo "3. 答えるのに NG なら、確定モデルが一覧にあるか: 1 の結果に detail のモデル名があるか（無ければ pull し直す）" ;;
      esac
      case $NOW in
        NG2|NG3)
          echo "従系を見る: curl -sS --max-time 10 ${fallback%/}/api/tags。OS の更新・再起動の途中なら、戻るまで待てば直る。戻らなければ従系の機械で LLM のサーバを起き直す" ;;
      esac
      echo "背景: docs/design/disk-and-recovery-plan.md §3.9.5-A"
      echo
      echo "== 直ったら =="
      echo "この検査（30 分ごと）が「LLM primary ... -> OK」のメールを送ります。"
    fi
  } | "$NOTIFY" "[$(hostname -s)] LLM primary $PREV -> $(label "$NOW")"
  printf "%s" "$NOW" > "$STATE"
fi
# **タイマーを failed にしない**（通知はメールで行う。赤いままだと他の障害が埋もれる）。
exit 0
