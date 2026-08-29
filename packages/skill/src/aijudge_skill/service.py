"""GradingCompleted を受けて習熟度を更新する（S5 → S7、設計方針 §2.3）。

**採点の内部構造を知らない。** 読むのはイベントが運ぶ `KcOutcome` だけで、
どの評価器がどう判定したかには触れない。これが P6 の「Q-matrix を結節点に
する」の実体であり、採点の実装を変えても S7 が変わらない理由である。

**S7 が落ちても採点は完了する**（P2）。イベントは後から追いつく。逆に
S7 はイベントを二度受け取っても壊れてはならない（購読側は冪等）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from aijudge_core import (
    GradingCompleted,
    MasteryModel,
    Routing,
    SkillEvidence,
    SkillState,
    SkillStateUpdated,
    new_id,
)
from aijudge_core.ids import EventId, KcId

from .bkt import BktParameters, is_correct, posterior
from .protocol import SkillRepository

# 1 つの KC について保持する根拠の上限。
#
# **習熟度そのものは全観測を畳んだ値なので減らない。** 減るのは
# ポートフォリオに並べられる根拠の件数だけで、新しいものを残す。
MAX_EVIDENCE = 20

# この確信度に満たない判定は習熟度を動かさない。
#
# **人間が見ると決めた判定で、学習者の記録を動かさない。** 動かすと、
# 教員が後で覆しても習熟度には反映されない（覆しのイベントが無い）。
# 確信度の低い AI 判定がポートフォリオに残り続けることになる。
MIN_CONFIDENCE = 0.5


class SkillService:
    def __init__(
        self,
        repository: SkillRepository,
        *,
        parameters: Callable[[KcId], BktParameters] | None = None,
        max_evidence: int = MAX_EVIDENCE,
        min_confidence: float = MIN_CONFIDENCE,
    ) -> None:
        self._repository = repository
        # KC ごとのパラメータ。当てるには観測が要るので、既定では共通の値。
        self._parameters = parameters or (lambda _: BktParameters())
        self._max_evidence = max_evidence
        self._min_confidence = min_confidence

    def apply(
        self, event: GradingCompleted, *, observed_at: datetime | None = None
    ) -> tuple[SkillStateUpdated, ...]:
        """1 件の採点完了を習熟度に反映し、起きた更新を返す。

        **テナントも時刻もイベントから読む。** 引数で別に受け取ると、
        呼び出し側が取り違えた瞬間に他テナントの記録を書ける。

        返り値は S8（ポートフォリオ）と S2（弱い KC を狙った作問）へ流す
        イベント。**購読者が居なくても S7 は成立する**（P2）。
        """
        at = observed_at or event.occurred_at
        updates: list[SkillStateUpdated] = []
        for outcome in event.kc_outcomes:
            update = self._apply_one(event, outcome, observed_at=at)
            if update is not None:
                updates.append(update)
        return tuple(updates)

    def _apply_one(
        self, event: GradingCompleted, outcome, *, observed_at: datetime
    ) -> SkillStateUpdated | None:
        tenant_id = event.tenant_id
        if outcome.confidence < self._min_confidence:
            # 確信度が足りない。習熟度は動かさない（モジュール冒頭）。
            return None

        state = self._repository.get_state(tenant_id, event.learner_id, outcome.kc_id)
        if state is not None and any(
            item.grading_run_id == event.grading_run_id for item in state.evidence
        ):
            # 同じ採点を二度受け取った。**冪等**（§2.3）。再送で習熟度が
            # 二度上がると、配信の都合が学習者の記録に出てしまう。
            return None

        params = self._parameters(outcome.kc_id)
        previous = None if state is None else state.mastery
        prior = params.prior if state is None else state.mastery
        mastery = posterior(prior, is_correct(outcome.score_ratio), params)

        evidence = SkillEvidence(
            grading_run_id=event.grading_run_id,
            criterion_score_id=outcome.criterion_score_ids[0],
            score_ratio=outcome.score_ratio,
            # **誰も読んでいない採点を「確認済み」と書かない**（ADR 0005 /
            # ADR 0010）。自動確定は人が読んだ記録を作らない。
            human_verified=False,
            observed_at=observed_at,
        )
        history = ((() if state is None else state.evidence) + (evidence,))[-self._max_evidence :]

        self._repository.save_state(
            SkillState(
                tenant_id=tenant_id,
                learner_id=event.learner_id,
                kc_id=outcome.kc_id,
                mastery=mastery,
                model=MasteryModel.BKT,
                observation_count=(0 if state is None else state.observation_count) + 1,
                evidence=history,
                updated_at=observed_at,
            )
        )
        return SkillStateUpdated(
            event_id=EventId(new_id("evt")),
            tenant_id=tenant_id,
            occurred_at=observed_at,
            learner_id=event.learner_id,
            kc_id=outcome.kc_id,
            mastery=mastery,
            previous_mastery=previous,
            model=MasteryModel.BKT,
            observation_count=(0 if state is None else state.observation_count) + 1,
        )


def unreviewed(event: GradingCompleted) -> bool:
    """誰も読んでいない採点か。根拠の重みづけに使う。"""
    return event.routing is Routing.AUTO
