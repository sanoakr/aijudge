"""GradingCompleted を S7 に届ける（S5 → S7、設計方針 §2.3）。

**ここが唯一の接点である。** 採点ワーカーは S7 を import せず、S7 は採点を
import しない。両者を繋ぐのは app 層のこのモジュールだけで、それが
「サブシステムどうしは直接 import しない」（ADR 0001）の実体である。

**落ちても採点は完了する**（P2）。イベントは outbox に残り、リレーが
次回に再送する。習熟度が遅れて付くことはあっても、採点が止まることはない。
"""

from __future__ import annotations

import logging

from aijudge_core import GradingCompleted
from aijudge_core.events import DomainEvent
from aijudge_persistence import Database
from aijudge_skill import SkillService

logger = logging.getLogger(__name__)


class SkillSubscriber:
    """イベント 1 件を 1 トランザクションで習熟度に反映する。

    **トランザクションを跨がない。** 複数の KC をまたぐ更新が途中で落ちると、
    一部の KC だけが進んだ状態になり、再送しても冪等判定（同じ採点を二度
    吸収しない）が働いて残りが永久に埋まらない。
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    def __call__(self, event: DomainEvent) -> None:
        if not isinstance(event, GradingCompleted):
            return
        if not event.kc_outcomes:
            # Q-matrix が空の課題。習熟度は付かないが、それは劣化であって
            # 失敗ではない（KC を宣言していない課題では正常な状態）。
            return

        with self._database.unit_of_work() as uow:
            updates = SkillService(uow.skills).apply(event)
            uow.commit()

        if updates:
            logger.info(
                "skill updated: learner=%s kcs=%d run=%s",
                event.learner_id,
                len(updates),
                event.grading_run_id,
            )


def subscribe_skills(relay, database: Database) -> SkillSubscriber:
    """リレーに S7 を繋ぐ。**繋がなくても採点は動く。**"""
    subscriber = SkillSubscriber(database)
    relay.subscribe(GradingCompleted.model_fields["type"].default, subscriber)
    return subscriber
