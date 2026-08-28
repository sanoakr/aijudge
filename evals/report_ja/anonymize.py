"""本文から氏名と学籍番号を落とす。

**採点に氏名は要らない。** 実際、ローカル 2 モデルの判定根拠はどれも本文の
中身を引いており、氏名を見た形跡はない。落としておけば、学外のモデルに
渡すときに設計原則 P7 の例外を作らずに済む。

落とすのは**直接の識別子だけ**で、本文は削らない。レポートの中身を削ると
採点そのものが変わってしまい、他モデルとの比較にならない。

完全な匿名化ではない（本文に自分の氏名を書き込んだ箇所までは追えない）。
残りを検出するために、除去後に学籍番号の形が残っていれば報告する。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent

# 学籍番号の形。実データは Y+6 桁。
STUDENT_ID = re.compile(r"[YyＹｙ][\s　]*[0-9０-９][\s　]*(?:[0-9０-９][\s　]*){5}")
# 氏名の行。「名前：」「氏名：」に続くもの、または学籍番号と同じ行に並ぶもの。
NAME_LABEL = re.compile(r"^[\s　]*(?:名前|氏名|Name)[\s　]*[:：].*$", re.MULTILINE)
# PDF 抽出で 1 文字ずつ割れた見出し（「名 前 ： 柏 田  聡」）も拾う。
NAME_LABEL_SPACED = re.compile(
    r"^[\s　]*(?:名[\s　]*前|氏[\s　]*名)[\s　]*[:：].*$", re.MULTILINE
)


def anonymize(text: str) -> tuple[str, int]:
    """氏名・学籍番号を伏せた本文と、伏せた箇所の数を返す。"""
    hits = 0

    def redact_line(match: re.Match[str]) -> str:
        nonlocal hits
        hits += 1
        return "（氏名は伏せてあります）"

    out = NAME_LABEL_SPACED.sub(redact_line, text)
    out = NAME_LABEL.sub(redact_line, out)

    # 学籍番号の直後に続く氏名（「Y230056 三宅驍」）も同じ行なので、
    # 学籍番号から行末までを伏せる ── ただし本文中の言及は消さないよう、
    # **行の先頭 30 字以内に現れる場合だけ**にする（表紙の署名の形）。
    lines = []
    for line in out.split("\n"):
        match = STUDENT_ID.search(line)
        if match is not None and match.start() < 30 and len(line.strip()) < 60:
            hits += 1
            lines.append("（学籍番号と氏名は伏せてあります）")
            continue
        lines.append(STUDENT_ID.sub("（学籍番号）", line))
        if match is not None:
            hits += 1
    return "\n".join(lines), hits


def main() -> int:
    index = json.loads((HERE / "index.json").read_text(encoding="utf-8"))
    out_dir = HERE / "bodies_anon"
    out_dir.mkdir(exist_ok=True)
    total = 0
    for row in sorted(index, key=lambda r: r["login"]):
        text = (HERE / "bodies" / f"{row['login']}.txt").read_text(encoding="utf-8")
        cleaned, hits = anonymize(text)
        (out_dir / f"{row['login']}.txt").write_text(cleaned, encoding="utf-8")
        total += hits
        left = STUDENT_ID.findall(cleaned)
        flag = f"  ← 学籍番号らしきものが {len(left)} 件残っています" if left else ""
        removed = len(text) - len(cleaned)
        print(f"  {row['login']:9} 伏せた箇所 {hits:2}  差 {removed:+5} 字{flag}")
    print(f"\n合計 {total} 箇所を伏せました → bodies_anon/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
