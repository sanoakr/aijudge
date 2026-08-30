"""S7 が保存先に求めること。**実装は知らない。**

インメモリでも PostgreSQL でも同じ規則で動く。サブシステムが保存先の実装に
依存しないのは、`.importlinter` の契約でもある。
"""

from __future__ import annotations

from typing import Protocol

from aijudge_core import KnowledgeComponent, SkillState
from aijudge_core.ids import KcId, TenantId, UserId


class SkillRepository(Protocol):
    def get_state(
        self, tenant_id: TenantId, learner_id: UserId, kc_id: KcId
    ) -> SkillState | None: ...

    def save_state(self, state: SkillState) -> None: ...

    def list_states(self, tenant_id: TenantId, learner_id: UserId) -> tuple[SkillState, ...]: ...

    def get_kc(self, kc_id: KcId) -> KnowledgeComponent | None: ...


class InMemorySkillRepository:
    """テストと単独起動用。"""

    def __init__(self, kcs: tuple[KnowledgeComponent, ...] = ()) -> None:
        self._states: dict[tuple[str, str, str], SkillState] = {}
        self._kcs = {kc.id: kc for kc in kcs}

    def get_state(self, tenant_id: TenantId, learner_id: UserId, kc_id: KcId) -> SkillState | None:
        return self._states.get((str(tenant_id), str(learner_id), str(kc_id)))

    def save_state(self, state: SkillState) -> None:
        key = (str(state.tenant_id), str(state.learner_id), str(state.kc_id))
        self._states[key] = state

    def list_states(self, tenant_id: TenantId, learner_id: UserId) -> tuple[SkillState, ...]:
        return tuple(
            state
            for state in self._states.values()
            if state.tenant_id == tenant_id and state.learner_id == learner_id
        )

    def get_kc(self, kc_id: KcId) -> KnowledgeComponent | None:
        return self._kcs.get(kc_id)
