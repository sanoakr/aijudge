"""サブエージェントの判定を、他モデルと同じ形（runs/*.json）に直す。

**段階の妥当性はここで確かめる。** 観点ごとに定義した範囲を外れた値、
欠けた観点、根拠の無い判定は、黙って通さず落として報告する ── ゲートウェイ
経路では `_clamp_level` と根拠検査がやっていることで、ここを省くと
「モデルが 9 を返したのを 8 として数えた」のような差が測定に紛れ込む。

確信度は入れない。**サンプルが 1 本なので自己一貫性が作れない。**
0 や 1.0 を入れると、確信度を根拠にした振り分けが意味を持ってしまう。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import rubric

FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def parse(path: Path) -> dict | None:
    text = path.read_text(encoding="utf-8").strip()
    # ``` で囲んで返してくることがある（qwen3.8 でも起きた失敗）。
    text = FENCE.sub("", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"  {path.name}: JSON として読めません — {exc}", file=sys.stderr)
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model", help="agent_out/ の下のディレクトリ名")
    parser.add_argument("--name", default=None, help="runs/ に置く名前（既定は model）")
    args = parser.parse_args()

    source = rubric.AGENT_OUT / args.model
    name = args.name or args.model
    top = dict(rubric.MAX_LEVEL)

    results = []
    for path in sorted(source.glob("*.json")):
        login = path.stem
        payload = parse(path)
        if payload is None:
            continue
        raw = payload.get("scores", payload)
        scores = {}
        unscored = []
        for code, limit in top.items():
            item = raw.get(code)
            if not isinstance(item, dict) or "level" not in item:
                unscored.append(code)
                continue
            level = item["level"]
            if not isinstance(level, int) or not 0 <= level <= limit:
                print(
                    f"  {login} {code}: 段階 {level!r} は 0〜{limit} の外です — 未採点にします",
                    file=sys.stderr,
                )
                unscored.append(code)
                continue
            if not item.get("evidence"):
                # 根拠を示せない判定は採用しない（設計原則 P4）。
                print(f"  {login} {code}: 根拠がありません — 未採点にします", file=sys.stderr)
                unscored.append(code)
                continue
            scores[code] = {
                "level": level,
                # サンプル 1 本なので自己一貫性が作れない。**確信度は入れない。**
                "confidence": None,
                "kind": "ai",
                "conclusive": False,
                "rationale": str(item.get("rationale", ""))[:2000],
            }
        results.append(
            {
                "login": login,
                # 合計比率は 23 点尺度から作る（他の実行と同じ意味にする）。
                "score_ratio": (
                    None
                    if unscored
                    else rubric.total({c: v["level"] for c, v in scores.items()})
                    / rubric.TOTAL_MAX
                ),
                # 確信度が無いので振り分けは決められない。
                "routing": "not_routed",
                "seconds": None,
                "unscored": unscored,
                "scores": scores,
                "evaluators": [],
            }
        )

    out = rubric.RUNS / f"{name}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    complete = sum(1 for r in results if not r["unscored"])
    print(f"[{rubric.DATASET}] {len(results)} 件（全観点そろい {complete} 件） → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
