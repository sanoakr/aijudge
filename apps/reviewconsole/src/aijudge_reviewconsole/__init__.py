"""aiJudge review console — 採点結果を教員が確認して確定させる。

**採点はここでは走らない。** 採点は `worker.Grader`（`aijudge-grade`）が
提出時に走らせ、コンソールは届いた結果を読むだけ。レビューを採点の前提条件に
しないのは、測定用データの入力が採点を止めてはならないため（ADR 0007）。

blind 採点（AI を見る前に教員が段階を付ける）は**抽出された提出のみ**に求める。
順序を逆にすると教員の採点が AI に引きずられ、一致度の測定に使えなくなるが、
全件に課すとレビューのたびに 2 段階の入力を強制することになる。
"""

from __future__ import annotations

from .app import Console, build_app, create_app, numbered_lines
from .projection import project
from .store import (
    FinalDecision,
    GoldenMark,
    QueueEntry,
    ReviewStore,
    is_blind_sample,
)
from .tasks import TaskLoader
from .worker import Grader, grade_pending

__all__ = [
    "Console",
    "FinalDecision",
    "GoldenMark",
    "Grader",
    "QueueEntry",
    "ReviewStore",
    "TaskLoader",
    "build_app",
    "create_app",
    "grade_pending",
    "is_blind_sample",
    "numbered_lines",
    "project",
]
