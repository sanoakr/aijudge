"""GradingCompleted を受けて習熟度を更新する（S5 → S7、設計方針 §2.3）。

**採点の内部構造を知らない。** 読むのはイベントが運ぶ `KcOutcome` だけで、
どの評価器がどう判定したかには触れない。これが P6 の「Q-matrix を結節点に
する」の実体であり、採点の実装を変えても S7 が変わらない理由である。

**S7 が落ちても採点は完了する**（P2）。イベントは後から追いつく。逆に
S7 はイベントを二度受け取っても壊れてはならない（購読側は冪等）。
"""

from __future__ import annotations

from datetime import datetime

from aijudge_core import (
    GradingCompleted,
    KcOutcome,
    MasteryModel,
    MasteryScope,
    Routing,
    SkillEvidence,
    SkillObservation,
    SkillPoint,
    SkillState,
    SkillStateUpdated,
    new_id,
)
from aijudge_core.ids import EventId, KcId, TaskId, TenantId, UserId

from .bkt import PRACTICE_PARAMETERS, fold, is_correct
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
        max_evidence: int = MAX_EVIDENCE,
        min_confidence: float = MIN_CONFIDENCE,
        record_points: bool = True,
    ) -> None:
        self._repository = repository
        # 推移の点を書くか。**作り直しでは切る** ── 過去の採点を再生するたびに
        # 過去の日付の点を足すと、そのとき実際に出していた値の記録（P8）に
        # 作り直した値が混ざる。作り直しは終わったときに 1 点ずつ足す。
        self._record_points = record_points
        # パラメータは観測ごとに監督の有無で選ぶ（`bkt.parameters_for`・ADR 0032）。
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
        if event.provisional:
            # **暫定の採点で学習者の記録を動かさない。**
            #
            # 二段階キュー（ADR 0011）は 1 提出につきこのイベントを 2 回出す。
            # 1 回目は AI 観点が未採点で、その時点の総合点は決定的評価だけを
            # 比例配分したものである。それを習熟度に入れると、同じ提出を
            # 二度数えたうえ、一度目は不完全な評価で数えることになる。
            return ()

        at = observed_at or event.occurred_at
        updates: list[SkillStateUpdated] = []
        for outcome in event.kc_outcomes:
            update = self._apply_one(event, outcome, observed_at=at)
            if update is not None:
                updates.append(update)
        return tuple(updates)

    def _apply_one(
        self, event: GradingCompleted, outcome: KcOutcome, *, observed_at: datetime
    ) -> SkillStateUpdated | None:
        """観測を課題ごとの最終提出で置き換え、習熟度を畳み直す（ADR 0032）。

        以前は採点 1 回ごとに観測を足していたので、演習で通るまで出し直した
        回数や再採点が、そのまま習熟度を動かした。
        """
        tenant_id = event.tenant_id
        if outcome.confidence < self._min_confidence:
            # 確信度が足りない。習熟度は動かさない（モジュール冒頭）。
            return None

        observations = self._repository.list_observations(
            tenant_id, event.learner_id, outcome.kc_id
        )
        if any(item.grading_run_id == event.grading_run_id for item in observations):
            # 同じ採点を二度受け取った。**冪等**（§2.3）。再送で習熟度が
            # 動くと、配信の都合が学習者の記録に出てしまう。
            return None

        # 課題の ID を持たないイベント（この欄より前に積まれたもの）は、版で代える。
        task_id = event.task_id or TaskId(str(event.task_version_id))
        submitted_at = event.submitted_at or event.occurred_at
        current = next((item for item in observations if item.task_id == task_id), None)
        if current is not None and current.submitted_at > submitted_at:
            # **古い提出の結果で、新しい提出の観測を上書きしない。** 再送や
            # 遅れて終わった採点は、提出の順では後ろにある。
            return None

        observation = SkillObservation(
            tenant_id=tenant_id,
            learner_id=event.learner_id,
            kc_id=outcome.kc_id,
            task_id=task_id,
            submission_id=event.submission_id,
            grading_run_id=event.grading_run_id,
            criterion_score_id=outcome.criterion_score_ids[0],
            score_ratio=outcome.score_ratio,
            # 採点が持っていた確信度をそのまま運ぶ（#335）。
            confidence=outcome.confidence,
            exam=event.exam,
            submitted_at=submitted_at,
            observed_at=observed_at,
        )
        self._repository.save_observation(observation)
        kept = tuple(item for item in observations if item.task_id != task_id)
        return self._refold(
            event.tenant_id, event.learner_id, outcome.kc_id, (*kept, observation), observed_at
        )

    def _refold(
        self,
        tenant_id: TenantId,
        learner_id: UserId,
        kc_id: KcId,
        observations: tuple[SkillObservation, ...],
        observed_at: datetime,
    ) -> SkillStateUpdated | None:
        """観測を提出の順に並べて BKT で畳み、状態と推移を書く。

        **動かなければ推移は書かない。** 正答の課題を正答で出し直しただけなら
        習熟度も観測の数も変わらない ── そこで点を残すと、出し直しの回数が
        推移の図に出る（ADR 0032 が無くしたかったもの）。
        """
        ordered = sorted(observations, key=lambda item: (item.submitted_at, str(item.task_id)))
        # 3 通りに畳む（ADR 0032）。演習だけ・試験だけは、その種類の観測だけを
        # その種類のパラメータで畳む ── 合わせた値と同じ出発点（P(L0)）から始める。
        values: dict[MasteryScope, tuple[float | None, int]] = {}
        for scope in MasteryScope:
            chosen = [
                item
                for item in ordered
                if scope is MasteryScope.ALL or item.exam == (scope is MasteryScope.EXAM)
            ]
            folded = fold(
                ((is_correct(item.score_ratio), item.exam) for item in chosen),
                prior=PRACTICE_PARAMETERS.prior,
            )
            values[scope] = (folded if chosen else None, len(chosen))
        mastery, count = values[MasteryScope.ALL]
        assert mastery is not None  # 観測を 1 件は足した直後に呼ばれる
        state = self._repository.get_state(tenant_id, learner_id, kc_id)
        previous = None if state is None else state.mastery
        evidence = tuple(
            SkillEvidence(
                grading_run_id=item.grading_run_id,
                criterion_score_id=item.criterion_score_id,
                score_ratio=item.score_ratio,
                confidence=item.confidence,
                # **誰も読んでいない採点を「確認済み」と書かない**（ADR 0005 /
                # ADR 0010）。自動確定は人が読んだ記録を作らない。
                human_verified=False,
                exam=item.exam,
                observed_at=item.observed_at,
            )
            for item in ordered
        )[-self._max_evidence :]
        self._repository.save_state(
            SkillState(
                tenant_id=tenant_id,
                learner_id=learner_id,
                kc_id=kc_id,
                mastery=mastery,
                model=MasteryModel.BKT,
                observation_count=count,
                evidence=evidence,
                updated_at=observed_at,
                practice_mastery=values[MasteryScope.PRACTICE][0],
                practice_count=values[MasteryScope.PRACTICE][1],
                exam_mastery=values[MasteryScope.EXAM][0],
                exam_count=values[MasteryScope.EXAM][1],
            )
        )
        moved = [
            scope
            for scope, (value, scoped_count) in values.items()
            if value is not None
            and (
                state is None
                or state.mastery_in(scope) != value
                or state.count_in(scope) != scoped_count
            )
        ]
        if not moved or not self._record_points:
            return None
        if not self._record_points:
            return None
        # **動いた瞬間を残す**（#328）。`SkillState` は最新の 1 行を持ち替える
        # ので、ここで残さないと推移は二度と読めない。範囲ごとに、動いたものだけ。
        for scope in moved:
            value, scoped_count = values[scope]
            assert value is not None
            self._repository.append_point(
                SkillPoint(
                    tenant_id=tenant_id,
                    learner_id=learner_id,
                    kc_id=kc_id,
                    mastery=value,
                    observation_count=scoped_count,
                    model=MasteryModel.BKT,
                    scope=scope,
                    recorded_at=observed_at,
                )
            )
        if MasteryScope.ALL not in moved:
            return None
        return SkillStateUpdated(
            event_id=EventId(new_id("evt")),
            tenant_id=tenant_id,
            occurred_at=observed_at,
            learner_id=learner_id,
            kc_id=kc_id,
            mastery=mastery,
            previous_mastery=previous,
            model=MasteryModel.BKT,
            observation_count=count,
        )


def unreviewed(event: GradingCompleted) -> bool:
    """誰も読んでいない採点か。根拠の重みづけに使う。"""
    return event.routing is Routing.AUTO
