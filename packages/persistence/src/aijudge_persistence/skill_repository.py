"""S7 の保存先の SQLAlchemy 実装。

**S7 はこのモジュールを知らない。** `aijudge_skill.SkillRepository` が定める
規則をこちらが満たす向きで、逆向きは `import-linter` の契約で禁じている
（スキル推定は保存先の実装に依存しない）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from aijudge_core import (
    KnowledgeComponent,
    MasteryModel,
    MasteryScope,
    SkillObservation,
    SkillPoint,
    SkillState,
    effective_points,
    new_id,
)
from aijudge_core.ids import KcId, TenantId, UserId

from .schema import KnowledgeComponentRow, SkillObservationRow, SkillPointRow, SkillStateRow


class SqlSkillRepository:
    def __init__(self, session: DbSession) -> None:
        self._session = session

    # -- 習熟度 ------------------------------------------------------------

    def get_state(self, tenant_id: TenantId, learner_id: UserId, kc_id: KcId) -> SkillState | None:
        row = self._session.get(SkillStateRow, (str(tenant_id), str(learner_id), str(kc_id)))
        return None if row is None else SkillState.model_validate(row.document)

    def save_state(self, state: SkillState) -> None:
        """**持ち替える。** 習熟度は推定値であって、追記の対象ではない。

        経緯は `SkillEvidence` が採点結果を指すことで辿れる。推定値そのものを
        全部残しても、辿れるものは増えない（同じ観測列から再計算できる）。
        """
        key = (str(state.tenant_id), str(state.learner_id), str(state.kc_id))
        row = self._session.get(SkillStateRow, key)
        document = state.model_dump(mode="json")
        if row is None:
            self._session.add(
                SkillStateRow(
                    tenant_id=key[0],
                    learner_id=key[1],
                    kc_id=key[2],
                    mastery=state.mastery,
                    model=state.model.value,
                    observation_count=state.observation_count,
                    updated_at=state.updated_at,
                    document=document,
                )
            )
        else:
            row.mastery = state.mastery
            row.model = state.model.value
            row.observation_count = state.observation_count
            row.updated_at = state.updated_at
            row.document = document
        self._session.flush()

    def list_states(self, tenant_id: TenantId, learner_id: UserId) -> tuple[SkillState, ...]:
        rows = self._session.execute(
            select(SkillStateRow)
            .where(
                SkillStateRow.tenant_id == str(tenant_id),
                SkillStateRow.learner_id == str(learner_id),
            )
            .order_by(SkillStateRow.kc_id)
        ).scalars()
        return tuple(SkillState.model_validate(row.document) for row in rows)

    def list_states_for(
        self, tenant_id: TenantId, learner_ids: tuple[UserId, ...]
    ) -> tuple[SkillState, ...]:
        """**1 回の問い合わせで引く。** 人数ぶん `list_states` を呼ばせない。"""
        if not learner_ids:
            return ()
        rows = self._session.execute(
            select(SkillStateRow)
            .where(
                SkillStateRow.tenant_id == str(tenant_id),
                SkillStateRow.learner_id.in_([str(x) for x in learner_ids]),
            )
            .order_by(SkillStateRow.learner_id, SkillStateRow.kc_id)
        ).scalars()
        return tuple(SkillState.model_validate(row.document) for row in rows)

    # -- 観測（ADR 0032）---------------------------------------------------

    def list_observations(
        self, tenant_id: TenantId, learner_id: UserId, kc_id: KcId
    ) -> tuple[SkillObservation, ...]:
        rows = self._session.execute(
            select(SkillObservationRow).where(
                SkillObservationRow.tenant_id == str(tenant_id),
                SkillObservationRow.learner_id == str(learner_id),
                SkillObservationRow.kc_id == str(kc_id),
            )
        ).scalars()
        return tuple(SkillObservation.model_validate(row.document) for row in rows)

    def save_observation(self, observation: SkillObservation) -> None:
        """**置き換える。** 課題ごとに最終提出の結果だけを持つ。"""
        key = (
            str(observation.tenant_id),
            str(observation.learner_id),
            str(observation.kc_id),
            str(observation.task_id),
        )
        row = self._session.get(SkillObservationRow, key)
        document = observation.model_dump(mode="json")
        if row is None:
            self._session.add(
                SkillObservationRow(
                    tenant_id=key[0],
                    learner_id=key[1],
                    kc_id=key[2],
                    task_id=key[3],
                    submitted_at=observation.submitted_at,
                    document=document,
                )
            )
        else:
            row.submitted_at = observation.submitted_at
            row.document = document
        self._session.flush()

    def clear(self, tenant_id: TenantId) -> None:
        """習熟度と観測を消す。**推移（`skill_points`）は残す**（P8）。"""
        for row_type in (SkillObservationRow, SkillStateRow):
            self._session.execute(delete(row_type).where(row_type.tenant_id == str(tenant_id)))
        self._session.flush()

    # -- 推移 --------------------------------------------------------------

    def append_point(self, point: SkillPoint) -> None:
        """**追記のみ**（`SkillPoint`）。持ち替えない。"""
        self._session.add(
            SkillPointRow(
                id=new_id("skp"),
                tenant_id=str(point.tenant_id),
                learner_id=str(point.learner_id),
                kc_id=str(point.kc_id),
                mastery=point.mastery,
                observation_count=point.observation_count,
                model=point.model.value,
                scope=point.scope.value,
                recorded_at=point.recorded_at,
                rebuilt_at=point.rebuilt_at,
            )
        )
        self._session.flush()

    def history(
        self,
        tenant_id: TenantId,
        learner_ids: tuple[UserId, ...],
        *,
        kc_ids: tuple[KcId, ...] = (),
        since: datetime | None = None,
        scope: MasteryScope = MasteryScope.ALL,
    ) -> tuple[SkillPoint, ...]:
        if not learner_ids:
            return ()
        query = select(SkillPointRow).where(
            SkillPointRow.tenant_id == str(tenant_id),
            SkillPointRow.learner_id.in_([str(x) for x in learner_ids]),
        )
        if kc_ids:
            query = query.where(SkillPointRow.kc_id.in_([str(x) for x in kc_ids]))
        if since is not None:
            query = query.where(SkillPointRow.recorded_at >= since)
        query = query.where(SkillPointRow.scope == scope.value)
        # 古い順。同着は観測数で解く（同じ日に何度も動く）。
        rows = self._session.execute(
            query.order_by(SkillPointRow.recorded_at, SkillPointRow.observation_count)
        ).scalars()
        # 描く点を選ぶ（作り直しより前は作り直した点で・`effective_points`）。
        return effective_points(
            SkillPoint(
                tenant_id=TenantId(row.tenant_id),
                learner_id=UserId(row.learner_id),
                kc_id=KcId(row.kc_id),
                mastery=row.mastery,
                observation_count=row.observation_count,
                model=MasteryModel(row.model),
                scope=MasteryScope(row.scope),
                recorded_at=row.recorded_at,
                rebuilt_at=row.rebuilt_at,
            )
            for row in rows
        )

    # -- 知識要素 ----------------------------------------------------------

    def get_kc(self, kc_id: KcId) -> KnowledgeComponent | None:
        row = self._session.get(KnowledgeComponentRow, str(kc_id))
        return None if row is None else KnowledgeComponent.model_validate(row.document)

    def find_kc_by_key(self, key: str) -> KnowledgeComponent | None:
        row = (
            self._session.execute(
                select(KnowledgeComponentRow).where(KnowledgeComponentRow.key == key)
            )
            .scalars()
            .first()
        )
        return None if row is None else KnowledgeComponent.model_validate(row.document)

    def save_kc(self, kc: KnowledgeComponent) -> None:
        row = self._session.get(KnowledgeComponentRow, str(kc.id))
        document = kc.model_dump(mode="json")
        if row is None:
            self._session.add(
                KnowledgeComponentRow(
                    id=str(kc.id),
                    namespace=kc.namespace,
                    key=kc.key,
                    label=kc.label,
                    parent_id=None if kc.parent_id is None else str(kc.parent_id),
                    document=document,
                )
            )
        else:
            row.label = kc.label
            row.parent_id = None if kc.parent_id is None else str(kc.parent_id)
            row.document = document
        self._session.flush()

    def delete_kc(self, kc_id: KcId) -> None:
        """KC を 1 件消す。**呼んでよいかの判断は呼び出し側が持つ。**

        使われている KC を消すと、過去の課題が何を問うていたのか辿れなく
        なる（P8）。その判定は利用状況を数えられる層でしかできないので、
        ここは求められたとおりに消す（`aijudge_course_admin.kc.delete` が守る）。
        """
        row = self._session.get(KnowledgeComponentRow, str(kc_id))
        if row is not None:
            self._session.delete(row)
            self._session.flush()

    def list_kcs(self, namespace: str | None = None) -> tuple[KnowledgeComponent, ...]:
        statement = select(KnowledgeComponentRow).order_by(KnowledgeComponentRow.key)
        if namespace is not None:
            statement = statement.where(KnowledgeComponentRow.namespace == namespace)
        rows = self._session.execute(statement).scalars()
        return tuple(KnowledgeComponent.model_validate(row.document) for row in rows)
