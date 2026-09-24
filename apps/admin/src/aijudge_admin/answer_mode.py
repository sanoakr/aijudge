"""課題の答え方を `editor` にしてよいか（ADR 0026・設計書 §4.1）。

**エディタで書けるのは、課題が受け付ける提出形式のうち `.c`・`.py`・`.md` の
どれかである**（`aijudge_ide.formats`、2026-09-24 決定）。1 つも受け付けない
課題 ── PDF や画像だけの課題 ── をエディタにすると、学習者は何も提出できない。

`.md` だけの課題（オンラインのレポート試験）もエディタにできる。**実行でき
るかは別の問い**で、プログラムの形式が採点の言語と一致するときだけ画面に
「実行」が出る（runner がもう一度確かめる）。

規則をここに置くのは、画面と API の両方から同じ検査を通すため。**画面で
灰色にするだけでは境界にならない**（#146）── 保存する側がもう一度確かめる。
"""

from __future__ import annotations

from collections.abc import Sequence

from aijudge_core import Course, Task, TaskVersion, allowed_suffixes
from aijudge_ide import EDITOR_FORMATS, editor_formats


def editor_blockers(tasks: Sequence[tuple[Task, TaskVersion]], course: Course) -> tuple[str, ...]:
    """`editor` にできない理由。空ならできる。

    提出形式は画面と受付と同じ関数（`allowed_suffixes`：課題の指定、空なら
    コースの既定）で決める。別の決め方をすると、画面がエディタを出したのに
    提出で断られる。
    """
    wanted = "・".join(EDITOR_FORMATS)
    reasons: list[str] = []
    for task, _version in tasks:
        accepted = allowed_suffixes(task.accepted_suffixes, course.upload_suffixes)
        if not editor_formats(accepted):
            # 教員が読む文言なので、課題の名前で指す（ID では読めない）。
            reasons.append(
                f"{task.title}: 提出形式に {wanted} のどれも含まれていません"
                f"（いまは {'・'.join(accepted) or 'なし'}）"
            )
    return tuple(reasons)
