"""推移として描く点の選び方（`effective_points`・ADR 0032 §5）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aijudge_core import MasteryModel, SkillPoint, effective_points
from aijudge_core.ids import KcId, TenantId, UserId

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _point(mastery: float, *, day: int, rebuilt: int | None = None) -> SkillPoint:
    return SkillPoint(
        tenant_id=TenantId("ten_" + "0" * 32),
        learner_id=UserId("usr_" + "1" * 32),
        kc_id=KcId("kc_" + "2" * 32),
        mastery=mastery,
        observation_count=1,
        model=MasteryModel.BKT,
        recorded_at=T0 + timedelta(days=day),
        rebuilt_at=None if rebuilt is None else T0 + timedelta(days=rebuilt),
    )


def test_without_a_rebuild_every_point_is_drawn() -> None:
    points = (_point(0.1, day=0), _point(0.2, day=1))
    assert effective_points(points) == points


def test_before_the_latest_rebuild_only_its_points_are_drawn() -> None:
    """古い数え方の点と、古い作り直しの点は描かない。作り直しの後のふだんの点は描く。"""
    old = _point(0.3, day=0)
    first_rebuild = _point(0.4, day=0, rebuilt=5)
    rebuilt = _point(0.5, day=0, rebuilt=10)
    before_rebuild = _point(0.6, day=8)
    after_rebuild = _point(0.7, day=12)
    drawn = effective_points((old, first_rebuild, rebuilt, before_rebuild, after_rebuild))
    assert [p.mastery for p in drawn] == [0.5, 0.7]
