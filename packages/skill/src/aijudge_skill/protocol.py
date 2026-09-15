"""S7 が保存先に求めること。**実装は知らない。**

インメモリでも PostgreSQL でも同じ規則で動く。サブシステムが保存先の実装に
依存しないのは、`.importlinter` の契約でもある。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from aijudge_core import KnowledgeComponent, SkillPoint, SkillState
from aijudge_core.ids import KcId, TenantId, UserId


class SkillRepository(Protocol):
    def get_state(
        self, tenant_id: TenantId, learner_id: UserId, kc_id: KcId
    ) -> SkillState | None: ...

    def save_state(self, state: SkillState) -> None: ...

    def list_states(self, tenant_id: TenantId, learner_id: UserId) -> tuple[SkillState, ...]: ...

    def list_states_for(
        self, tenant_id: TenantId, learner_ids: tuple[UserId, ...]
    ) -> tuple[SkillState, ...]:
        """複数の学習者の習熟度をまとめて引く。

        **1 人ずつ引かせない。** コースの受講者 88 名を並べる画面が
        `list_states` を人数ぶん呼ぶと、学期が進むほどその画面だけが遅くなり、
        件数からは気づけない（`roles_by_user` と同じ理由）。

        `learner_ids` が空なら空を返す ── 「全員」の意味にしない。取り違えると
        テナント全員の習熟度を 1 コースの画面に並べることになる。
        """
        ...

    def append_point(self, point: SkillPoint) -> None:
        """習熟度が動いた瞬間を記録する。**追記のみ**（`SkillPoint`）。

        冪等ではない ── 呼ぶ側（`SkillService`）が、実際に動いたときだけ
        呼ぶ責任を持つ。
        """
        ...

    def history(
        self,
        tenant_id: TenantId,
        learner_ids: tuple[UserId, ...],
        *,
        kc_ids: tuple[KcId, ...] = (),
        since: datetime | None = None,
    ) -> tuple[SkillPoint, ...]:
        """記録された推移。古い順。

        `kc_ids` が空なら KC で絞らない。`learner_ids` は空なら空を返す
        （`list_states_for` と同じ作法）。
        """
        ...

    def get_kc(self, kc_id: KcId) -> KnowledgeComponent | None: ...


class InMemorySkillRepository:
    """テストと単独起動用。"""

    def __init__(self, kcs: tuple[KnowledgeComponent, ...] = ()) -> None:
        self._states: dict[tuple[str, str, str], SkillState] = {}
        self._points: list[SkillPoint] = []
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

    def list_states_for(
        self, tenant_id: TenantId, learner_ids: tuple[UserId, ...]
    ) -> tuple[SkillState, ...]:
        wanted = {str(learner_id) for learner_id in learner_ids}
        if not wanted:
            return ()
        return tuple(
            state
            for state in self._states.values()
            if state.tenant_id == tenant_id and str(state.learner_id) in wanted
        )

    def append_point(self, point: SkillPoint) -> None:
        self._points.append(point)

    def history(
        self,
        tenant_id: TenantId,
        learner_ids: tuple[UserId, ...],
        *,
        kc_ids: tuple[KcId, ...] = (),
        since: datetime | None = None,
    ) -> tuple[SkillPoint, ...]:
        wanted = {str(learner_id) for learner_id in learner_ids}
        if not wanted:
            return ()
        kcs = {str(kc_id) for kc_id in kc_ids}
        found = [
            point
            for point in self._points
            if point.tenant_id == tenant_id
            and str(point.learner_id) in wanted
            and (not kcs or str(point.kc_id) in kcs)
            and (since is None or point.recorded_at >= since)
        ]
        # 古い順。同着は観測数で解く（同じ日に何度も動くため）。
        found.sort(key=lambda point: (point.recorded_at, point.observation_count))
        return tuple(found)

    def get_kc(self, kc_id: KcId) -> KnowledgeComponent | None:
        return self._kcs.get(kc_id)
