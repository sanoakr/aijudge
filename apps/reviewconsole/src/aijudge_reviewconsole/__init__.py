"""aiJudge review console — 教員が採点し、その作業がゴールデンセットになる。

blind 採点 → AI の判定を開示 → 最終確定、という順序を守ることが設計の中心。
逆にすると教員の採点が AI に引きずられ、一致度の測定に使えなくなる。
"""

from __future__ import annotations

from .app import Console, build_app, create_app, numbered_lines
from .store import FinalDecision, QueueEntry, ReviewStore

__all__ = [
    "Console",
    "FinalDecision",
    "QueueEntry",
    "ReviewStore",
    "build_app",
    "create_app",
    "numbered_lines",
]
