"""S7 が保存先に求めること。**実装は知らない。**

インメモリでも PostgreSQL でも同じ規則で動く。サブシステムが保存先の実装に
依存しないのは、`.importlinter` の契約でもある。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from aijudge_core import KnowledgeComponent, SkillPoint, SkillState
from aijudge_core.ids import KcId, TenantId, UserId


@runtime_checkable
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

    def find_kc_by_key(self, key: str) -> KnowledgeComponent | None: ...

    def save_kc(self, kc: KnowledgeComponent) -> None:
        """KC を保存する。同じ ID なら上書きする（名前・親・退役の変更）。"""
        ...

    def delete_kc(self, kc_id: KcId) -> None:
        """KC を 1 件消す。無い ID でも落とさない。**呼んでよいかの判断は呼び出し側が持つ。**

        使われている KC を消すと、過去の課題が何を問うていたのか辿れなく
        なる（P8）。その判定は利用状況を数えられる層でしかできないので、
        ここは求められたとおりに消す（`aijudge_admin.kc.delete` が守る）。
        """
        ...

    def list_kcs(self, namespace: str | None = None) -> tuple[KnowledgeComponent, ...]:
        """KC の一覧。キーの順。`namespace` が None なら全部。"""
        ...


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

    def find_kc_by_key(self, key: str) -> KnowledgeComponent | None:
        return next((kc for kc in self._kcs.values() if kc.key == key), None)

    def save_kc(self, kc: KnowledgeComponent) -> None:
        self._kcs[kc.id] = kc

    def delete_kc(self, kc_id: KcId) -> None:
        self._kcs.pop(kc_id, None)

    def list_kcs(self, namespace: str | None = None) -> tuple[KnowledgeComponent, ...]:
        found = (kc for kc in self._kcs.values() if namespace is None or kc.namespace == namespace)
        return tuple(sorted(found, key=lambda kc: kc.key))
