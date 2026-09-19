#!/usr/bin/env bash
# 画像を読むモデルが、**指したホストで本当に読めるか**を確かめる（#341）。
#
#   aijudge-vision-check.sh                         # env の主系と従系を見る
#   aijudge-vision-check.sh http://node1:11434 ...  # ホストを直接指定する
#
# ## なぜ要るか
#
# `ProviderCapabilities.vision` は**構成値でしかない**。`OllamaProvider` は
# `vision=True` を既定で名乗るので、`gemma4:e4b`（vision 無し）を指したまま
# 画像を渡す構成が作れてしまう。そのとき画像は**黙って捨てられ**、本文だけで
# 答えが返る ── 応答は返り、スキーマにも合い、内容だけが根拠のない作り話に
# なる。落ちるより悪い。
#
# ## proxy の後ろは、これでも足りない
#
# PAIR のような複数ノードの前段では、**答えたノードの一覧しか返らない**
# ことがある。一部のノードにしかモデルが無い構成は、振り分け先によって
# 成否が変わり、症状が間欠的になる。**各ノードを直接指して 1 台ずつ**
# 確かめること ── それがこのスクリプトに引数でホストを渡せる理由である。
set -uo pipefail

MODEL="${AIJUDGE_LLM_VISION_MODEL:-qwen3-vl:8b}"

hosts=("$@")
if [ "${#hosts[@]}" -eq 0 ]; then
    [ -n "${AIJUDGE_LLM_VISION_BASE_URL:-}" ] && hosts+=("${AIJUDGE_LLM_VISION_BASE_URL}")
    [ -n "${AIJUDGE_LLM_VISION_FALLBACK_BASE_URL:-}" ] \
        && hosts+=("${AIJUDGE_LLM_VISION_FALLBACK_BASE_URL}")
fi
if [ "${#hosts[@]}" -eq 0 ]; then
    echo "見るホストがありません（AIJUDGE_LLM_VISION_BASE_URL を設定するか、引数で渡す）" >&2
    exit 2
fi

status=0
for host in "${hosts[@]}"; do
    body="$(curl -s -m 10 "${host%/}/api/tags" 2>/dev/null)"
    if [ -z "${body}" ]; then
        printf 'NG  %-48s 応答なし\n' "${host}"
        status=1
        continue
    fi
    # モデルが在るか、そして vision を名乗るか。**在るだけでは足りない。**
    verdict="$(printf '%s' "${body}" | MODEL="${MODEL}" python3 -c '
import json, os, sys
want = os.environ["MODEL"]
try:
    models = json.load(sys.stdin).get("models", [])
except json.JSONDecodeError:
    print("NG 応答が JSON ではない")
    raise SystemExit
for entry in models:
    if want in (entry.get("name"), entry.get("model")):
        caps = entry.get("capabilities") or []
        if "vision" in caps:
            print("OK vision あり")
        else:
            print("NG vision を名乗らない（" + ", ".join(map(str, caps)) + "）")
        break
else:
    names = ", ".join(sorted(str(entry.get("name")) for entry in models))
    print("NG モデルが無い（" + names + "）")
')"
    printf '%-3s %-48s %s\n' "${verdict%% *}" "${host}" "${verdict#* }"
    [ "${verdict%% *}" = "OK" ] || status=1
done

if [ "${status}" -ne 0 ]; then
    echo
    echo "画像を読めないホストがあります。そこへ振り分けられた提出は採点されず、"
    echo 'その観点は人のレビューに回ります（CapabilityMismatch）。'
fi
exit "${status}"
