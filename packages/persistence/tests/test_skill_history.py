"""習熟度の推移が SQL でも同じ規則で動く（#328）。

インメモリ側は `packages/skill/tests/test_skill.py` が固定している。こちらで
確かめたいのは 3 つ。**追記であること**（持ち替えない）、**古い順で返る**こと、
そして **学習者・KC・時刻で絞れる**ことである。

`database` フィクスチャは SQLite で常に走り、`AIJUDGE_TEST_DATABASE_URL` が
あれば PostgreSQL でも走る。時刻の扱いは方言差が出るところなので、ここは
両方で通っている必要がある。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from aijudge_core import MasteryModel, SkillPoint
from aijudge_core.ids import KcId, TenantId, UserId
from aijudge_persistence import Database

TENANT = TenantId("ten_" + "0" * 32)
OTHER_TENANT = TenantId("ten_" + "9" * 32)
LEARNER = UserId("usr_" + "4" * 32)
OTHER_LEARNER = UserId("usr_" + "5" * 32)
KC = KcId("kc_" + "2" * 32)
OTHER_KC = KcId("kc_" + "3" * 32)
NOW = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)

POSTGRES_URL = os.environ.get("AIJUDGE_TEST_DATABASE_URL")


@pytest.fixture(params=["sqlite"] + (["postgres"] if POSTGRES_URL else []))
def database(request) -> Database:
    from aijudge_persistence import Base

    url = "sqlite+pysqlite:///:memory:" if request.param == "sqlite" else POSTGRES_URL
    db = Database.connect(url, create=False)
    Base.metadata.drop_all(db.engine)
    Base.metadata.create_all(db.engine)
    yield db
    db.dispose()


def _point(
    *,
    learner: UserId = LEARNER,
    kc: KcId = KC,
    mastery: float = 0.5,
    n: int = 1,
    at: datetime = NOW,
    tenant: TenantId = TENANT,
) -> SkillPoint:
    return SkillPoint(
        tenant_id=tenant,
        learner_id=learner,
        kc_id=kc,
        mastery=mastery,
        observation_count=n,
        model=MasteryModel.BKT,
        recorded_at=at,
    )


def test_points_are_appended_not_replaced(database: Database) -> None:
    """**持ち替えない。** 同じ学習者・同じ KC の 2 点目が 1 点目を消さない
    ── 消すと、残っているのに推移が読めない `skill_states` と同じものになる。
    """
    with database.unit_of_work() as uow:
        uow.skills.append_point(_point(mastery=0.4, n=1))
        uow.skills.append_point(_point(mastery=0.6, n=2, at=NOW + timedelta(hours=1)))
        uow.commit()

    with database.unit_of_work() as uow:
        points = uow.skills.history(TENANT, (LEARNER,))

    assert [p.mastery for p in points] == [0.4, 0.6]


def test_points_come_back_oldest_first(database: Database) -> None:
    """図は左から右へ引く。**並べ替えを画面に任せない。**"""
    with database.unit_of_work() as uow:
        uow.skills.append_point(_point(mastery=0.6, n=2, at=NOW + timedelta(hours=1)))
        uow.skills.append_point(_point(mastery=0.4, n=1))
        uow.commit()

    with database.unit_of_work() as uow:
        points = uow.skills.history(TENANT, (LEARNER,))

    assert [p.observation_count for p in points] == [1, 2]


def test_the_same_moment_is_ordered_by_observation_count(database: Database) -> None:
    """**同じ時刻に何度も動く。** 時刻だけで並べると同着が任意の順で出る。"""
    with database.unit_of_work() as uow:
        uow.skills.append_point(_point(mastery=0.7, n=3))
        uow.skills.append_point(_point(mastery=0.5, n=2))
        uow.commit()

    with database.unit_of_work() as uow:
        points = uow.skills.history(TENANT, (LEARNER,))

    assert [p.observation_count for p in points] == [2, 3]


def test_history_is_narrowed_by_learner_kc_and_time(database: Database) -> None:
    later = NOW + timedelta(days=2)
    with database.unit_of_work() as uow:
        uow.skills.append_point(_point())
        uow.skills.append_point(_point(kc=OTHER_KC))
        uow.skills.append_point(_point(learner=OTHER_LEARNER))
        uow.skills.append_point(_point(at=later, n=2))
        uow.commit()

    with database.unit_of_work() as uow:
        assert len(uow.skills.history(TENANT, (LEARNER,))) == 3
        assert len(uow.skills.history(TENANT, (LEARNER, OTHER_LEARNER))) == 4
        assert len(uow.skills.history(TENANT, (LEARNER,), kc_ids=(KC,))) == 2
        assert len(uow.skills.history(TENANT, (LEARNER,), since=later)) == 1
        # **空は「全員」ではない。**
        assert uow.skills.history(TENANT, ()) == ()


def test_history_does_not_cross_tenants(database: Database) -> None:
    """習熟度はテナント単位で積み上がる。**越えれば他学の記録が見える。**"""
    with database.unit_of_work() as uow:
        uow.skills.append_point(_point())
        uow.skills.append_point(_point(tenant=OTHER_TENANT))
        uow.commit()

    with database.unit_of_work() as uow:
        assert len(uow.skills.history(TENANT, (LEARNER,))) == 1
        assert len(uow.skills.history(OTHER_TENANT, (LEARNER,))) == 1


def test_the_recorded_time_survives_the_round_trip(database: Database) -> None:
    """時刻は UTC のまま戻る。**方言差がいちばん出るところ。**"""
    with database.unit_of_work() as uow:
        uow.skills.append_point(_point(at=NOW))
        uow.commit()

    with database.unit_of_work() as uow:
        point = uow.skills.history(TENANT, (LEARNER,))[0]

    assert point.recorded_at == NOW
    assert point.recorded_at.tzinfo is not None, "timezone が落ちている"
