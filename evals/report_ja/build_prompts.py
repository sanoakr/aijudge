"""API を使えない環境で、Sonnet 5 / Opus 5 に同じ判定をさせるための下ごしらえ。

**これは `rubric_ai_judge` の経路ではない。** ゲートウェイ（制約デコード・
温度 0・観点ごとに 1 回・自己一貫性）を通らず、サブエージェントに 1 回だけ
聞く。したがって比較は**同条件ではない**。違いは 3 つで、README にも書く。

  1. 観点を 6 つまとめて 1 回で聞く（本来は観点ごとに 1 回。§04 step 3）
  2. 自己一貫性のサンプルが 1 本（本来は 3 本）。確信度が作れない
  3. JSON スキーマによる制約デコードが無い

**文面と段階の記述は `rubric.py` から組む。** ここで書き直すと、測っている
ものがローカル 2 モデルと変わる。

本文は `bodies_anon/`（氏名・学籍番号を伏せたもの）を使う。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import rubric

HERE = Path(__file__).parent


def describe_levels(spec: dict) -> str:
    """`rubric_ai_judge.describe_levels` と同じ形にする。"""
    return "\n".join(
        f"  - {lv['level']}: {lv['label']} — {lv['descriptor']}" for lv in spec["levels"]
    )


def number_lines(source: str) -> str:
    """`rubric_ai_judge.number_lines` と同じ形にする。"""
    lines = source.replace("\r\n", "\n").split("\n")
    width = len(str(len(lines)))
    return "\n".join(f"{i:>{width}} | {line}" for i, line in enumerate(lines, 1))


CRITERIA_BLOCK = "\n\n".join(
    f"### 観点 `{c['code']}`: {c['title']}\n\n{c['description']}\n\n段階:\n"
    + describe_levels(c)
    for c in rubric.CRITERIA
)

TEMPLATE = """あなたは大学の演習レポートの採点者です。以下の観点だけを評価し、
根拠として本文の行範囲を必ず示します。JSON オブジェクトのみを出力します。

# 問題
{statement}

# 評価する観点

**観点ごとに独立に判断すること。** ある観点で高い段階を付けたことを、
別の観点の理由にしない。

{criteria}

# すでに確定していること

{prior}

# 学習者のレポート（行番号つき）
```
{body}
```

# 指示

- 上の 6 つの観点それぞれについて、段階の番号を 1 つ選ぶこと。
- すでに確定していることを再評価しないこと。
- `evidence` には本文の行範囲を 1 つ以上入れること。根拠を示せない判定はしない。
- `rationale` は日本語で、学習者が次に何をすればよいか分かるように書くこと。
- 行番号は上に示したものを使うこと。
- 段階は各観点で定義された範囲の整数であること。

出力する JSON の形（この形だけを出力し、他の文字は書かない）:

{{"scores": {{
  "format": {{"level": 整数, "evidence": [{{"start_line": 整数, "end_line": 整数}}], "rationale": "..."}},
  "target": {{...}}, "conditions": {{...}},
  "originality": {{...}}, "discussion": {{...}}, "results": {{...}}
}}}}
"""


def main() -> int:
    index = json.loads((HERE / "index.json").read_text(encoding="utf-8"))
    out = HERE / "prompts"
    out.mkdir(exist_ok=True)

    for row in sorted(index, key=lambda r: r["login"]):
        body = (HERE / "bodies_anon" / f"{row['login']}.txt").read_text(encoding="utf-8")
        # 体裁は決定的評価器が確定させる観点なので、ローカル経路と同じく
        # 「すでに確定していること」として渡す ── ここを空にすると、
        # 他モデルだけが体裁を自分で判定することになり比較にならない。
        prior = (
            "（体裁は機械が判定します。ここでは体裁についても段階を選んで"
            "ください。他に確定していることはありません。）"
        )
        text = TEMPLATE.format(
            statement=rubric.STATEMENT,
            criteria=CRITERIA_BLOCK,
            prior=prior,
            body=number_lines(body),
        )
        (out / f"{row['login']}.md").write_text(text, encoding="utf-8")
        print(f"  {row['login']:9} {len(text):6} 字")
    print(f"\n{len(index)} 件 → prompts/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
