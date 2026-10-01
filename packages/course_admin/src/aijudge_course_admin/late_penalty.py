"""遅延の減点ルール（`Course.late_penalty_steps`）を画面の入力から組む。

ADR 0013 は減点ルールを「猶予と同じ性質の運用値で、教員が学期中に決める」と
置いたが、設定する画面が無いまま運用に入り、2026-10-01 に network へ DB を
直接書き換えて入れた（監査に残らない）。その穴を塞ぐための読み取りである。

**入力は % で受ける。** 保存の形は割合（0〜1）だが、教員は「50% 減」と考える。
変換は 1 か所（ここ）だけで行い、画面と記録とで単位が食い違わないようにする。
"""

from __future__ import annotations

from collections.abc import Sequence

from aijudge_core import LatePenaltyStep
from aijudge_course_admin.errors import AdminError

# % を割合に直す。
PERCENT = 100.0


def parse_steps(rows: Sequence[tuple[str, str]]) -> tuple[LatePenaltyStep, ...]:
    """（超過時間, 減点 %）の行から減点ルールを組む。

    - **両方空の行は無視する**（画面は空の行を余分に出す）
    - 片方だけの行は断る ── 黙って捨てると、入れたつもりの段が消える
    - 時間は 0 以上、減点は 0 より大きく 100 以下
    - 同じ時間の段は断る。並びは時間の昇順に整える（`Course` の検査と同じ規則）
    """
    steps: list[LatePenaltyStep] = []
    for hours_raw, percent_raw in rows:
        hours_text, percent_text = hours_raw.strip(), percent_raw.strip()
        if not hours_text and not percent_text:
            continue
        if not hours_text or not percent_text:
            raise AdminError("減点ルールの行は、超過時間と減点の両方を入れてください")
        try:
            hours = float(hours_text)
            percent = float(percent_text)
        except ValueError:
            raise AdminError(
                f"減点ルールの値が数ではありません: {hours_text!r} / {percent_text!r}"
            ) from None
        if hours < 0:
            raise AdminError("減点ルールの超過時間は 0 以上にしてください")
        if not 0 < percent <= PERCENT:
            raise AdminError("減点は 0 より大きく 100 以下の % で入れてください")
        steps.append(LatePenaltyStep(after_hours=hours, ratio=round(percent / PERCENT, 6)))

    hours_seen = [step.after_hours for step in steps]
    if len(set(hours_seen)) != len(hours_seen):
        raise AdminError("減点ルールに同じ超過時間の段が 2 つあります")
    return tuple(sorted(steps, key=lambda step: step.after_hours))


def to_rows(steps: Sequence[LatePenaltyStep]) -> list[dict[str, str]]:
    """画面に出す行（時間と %）。整数で表せる値は小数点を付けない。"""
    return [
        {"hours": _plain(step.after_hours), "percent": _plain(step.ratio * PERCENT)}
        for step in steps
    ]


def describe(steps: Sequence[LatePenaltyStep]) -> list[dict[str, float]]:
    """監査に残す形。**割合ではなく % で書く** ── 読む人が画面と照らし合わせるため。"""
    return [
        {"after_hours": step.after_hours, "percent": round(step.ratio * PERCENT, 4)}
        for step in steps
    ]


def _plain(value: float) -> str:
    rounded = round(value, 4)
    return str(int(rounded)) if rounded == int(rounded) else str(rounded)


__all__ = ["describe", "parse_steps", "to_rows"]
