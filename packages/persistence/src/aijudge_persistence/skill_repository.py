"""S7 の保存先の SQLAlchemy 実装。

**S7 はこのモジュールを知らない。** `aijudge_skill.SkillRepository` が定める
規則をこちらが満たす向きで、逆向きは `import-linter` の契約で禁じている
（スキル推定は保存先の実装に依存しない）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from aijudge_core import KnowledgeComponent, SkillState
from aijudge_core.ids import KcId, TenantId, UserId

from .schema import KnowledgeComponentRow, SkillStateRow


class SqlSkillRepository:
    def __init__(self, session: DbSession) -> None:
        self._session = session

    # -- 習熟度 ------------------------------------------------------------

    def get_state(
        self, tenant_id: TenantId, learner_id: UserId, kc_id: KcId
    ) -> SkillState | None:
        row = self._session.get(
            SkillStateRow, (str(tenant_id), str(learner_id), str(kc_id))
        )
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

    def list_kcs(self, namespace: str | None = None) -> tuple[KnowledgeComponent, ...]:
        statement = select(KnowledgeComponentRow).order_by(KnowledgeComponentRow.key)
        if namespace is not None:
            statement = statement.where(KnowledgeComponentRow.namespace == namespace)
        rows = self._session.execute(statement).scalars()
        return tuple(KnowledgeComponent.model_validate(row.document) for row in rows)
