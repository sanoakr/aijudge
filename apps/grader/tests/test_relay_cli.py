"""`aijudge-relay` ── outbox を流す常駐（#328）。

固定したいのは 3 つ。

購読者が繋がる   組んだリレーは習熟度の購読者を持っている。**ここが要点**
                 ── 購読者は書かれていたのに誰も登録していなかった。
溜まりを片付ける `drain` は 1 回 100 件までなので、繰り返さないと終わらない。
空でも落ちない   何も無いときに 0 件で正常に終わる。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aijudge_core import GradingCompleted, KcOutcome, MasteryModel, Routing, SkillState
from aijudge_core.ids import (
    CriterionScoreId,
    EventId,
    GradingRunId,
    KcId,
    SubmissionId,
    TaskVersionId,
    TenantId,
    UserId,
)
from aijudge_grader.relay_cli import BATCH, _drain_all, build_relay, main
from aijudge_persistence import Database

TENANT = TenantId("ten_" + "0" * 32)
LEARNER = UserId("usr_" + "4" * 32)
KC = KcId("kc_" + "2" * 32)
NOW = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path):
    db = Database.connect(f"sqlite+pysqlite:///{tmp_path}/a.db", create=True)
    yield db
    db.dispose()


def _event(run: str) -> GradingCompleted:
    return GradingCompleted(
        event_id=EventId("evt_" + run.rjust(32, "0")),
        tenant_id=TENANT,
        occurred_at=NOW,
        grading_run_id=GradingRunId("grn_" + run.rjust(32, "0")),
        submission_id=SubmissionId("sub_" + run.rjust(32, "0")),
        task_version_id=TaskVersionId("tsv_" + "c" * 32),
        learner_id=LEARNER,
        score_ratio=1.0,
        confidence=1.0,
        routing=Routing.AUTO,
        kc_outcomes=(
            KcOutcome(
                kc_id=KC,
                score_ratio=1.0,
                confidence=1.0,
                criterion_score_ids=(CriterionScoreId("cs_" + "d" * 32),),
            ),
        ),
    )


def _queue(database: Database, events) -> None:
    with database.unit_of_work() as uow:
        for event in events:
            uow.outbox.append(event)
        uow.commit()


def test_the_relay_it_builds_carries_the_skill_subscriber(database: Database) -> None:
    """**ここが抜けていた。** 購読者は 2026-08-29 から在ったが、登録する人が
    テストしか居らず、運用では `grading.completed` が未送信で積み上がって
    いた（習熟度は 0 件のまま）。
    """
    relay = build_relay(database)
    _queue(database, [_event("1")])

    assert relay.drain(BATCH) == 1

    with database.unit_of_work() as uow:
        states = uow.skills.list_states(TENANT, LEARNER)
    assert [state.kc_id for state in states] == [KC], "習熟度が更新されていない"
    assert isinstance(states[0], SkillState)
    assert states[0].model is MasteryModel.BKT
    # 推移も残る（#329）。
    assert len(uow.skills.history(TENANT, (LEARNER,))) == 1


def test_a_backlog_larger_than_one_batch_is_cleared(database: Database) -> None:
    """**1 回の `drain` は上限つき。** 繰り返さないと溜まりが残る ── 運用に
    繋いだ初回がまさにそれで、数百件が待っている。
    """
    relay = build_relay(database)
    _queue(database, [_event(str(n)) for n in range(BATCH + 5)])

    moved = _drain_all(relay)

    assert moved == BATCH + 5
    with database.unit_of_work() as uow:
        assert uow.outbox.unpublished(BATCH * 2) == ()


def test_draining_nothing_is_not_an_error(database: Database) -> None:
    assert _drain_all(build_relay(database)) == 0


def test_once_drains_and_returns_zero(database: Database, tmp_path, capsys) -> None:
    """`--once` は cron でも回せる。**空でも 0 で終わる。**"""
    _queue(database, [_event("7")])

    code = main(["--database-url", f"sqlite+pysqlite:///{tmp_path}/a.db", "--once"])

    assert code == 0
    assert "1 件" in capsys.readouterr().out


def test_without_a_database_url_it_says_so(capsys, monkeypatch) -> None:
    """**黙って何もしないより、言って落ちる。** 止まっていることに気づけない
    のがこの配線の元々の問題だった。
    """
    monkeypatch.delenv("AIJUDGE_DATABASE_URL", raising=False)

    assert main([]) == 2
    assert "接続先" in capsys.readouterr().err
