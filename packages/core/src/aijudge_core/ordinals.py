"""提出の「何回目」を数える（2026-09-25）。

**課題の中で、版をまたいで数える。** `Submission.attempt` は版ごとに 1 から振る
（受付がその版の提出を数える）ので、課題を訂正して版が上がると「1 回目」が
重なる。画面に出す回数は学習者にとって「この課題に何回出したか」であって、
版ごとの内部の番号ではない。

記録（`attempt`）は書き換えない ── 表示のたびにここで数える。並べ方は採用の
規則（同点なら後の提出・`aijudge_reviewconsole.submissions.adopted_ids`）と同じ
`(提出時刻, attempt)` で、さらに同じなら ID で決める（偶然で順番を変えない）。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from .ids import SubmissionId, TaskId, UserId

__all__ = ["attempt_ordinals"]


def attempt_ordinals(
    rows: Iterable[tuple[SubmissionId, UserId, TaskId, datetime, int]],
) -> dict[SubmissionId, int]:
    """`(提出 ID, 学習者, 課題, 提出時刻, attempt)` の並びから、提出 → 課題の中での回数。

    渡されなかった提出は数に入らない ── 呼ぶ側は、その学習者のその課題の提出を
    **全部**渡すこと（一部だけだと番号が詰まる）。
    """
    groups: dict[tuple[str, str], list[tuple[datetime, int, str]]] = {}
    for submission_id, learner_id, task_id, submitted_at, attempt in rows:
        groups.setdefault((str(learner_id), str(task_id)), []).append(
            (submitted_at, attempt, str(submission_id))
        )
    numbers: dict[SubmissionId, int] = {}
    for members in groups.values():
        for index, (_at, _attempt, key) in enumerate(sorted(members), start=1):
            numbers[SubmissionId(key)] = index
    return numbers
