"""習熟度を過去の採点記録から作り直す（`aijudge-admin skill rebuild`・ADR 0032）。

固定したいのは 4 つ。

最終提出だけ  出し直した課題は最後の提出だけを数える（古い数え方の値を捨てる）
試験を重く    試験の課題は試験のパラメータで畳み直す
成績にしない  動作確認の提出は再生しない（#108）
記録を残す    過去の推移の点は消さず、作り直した点を足し、監査ログに残す
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_finalization import (
    PROFILES,
    TASK_ID,
    TASK_VERSION,
    TENANT,
    _run,
    _task_version,
)

from aijudge_admin.cli import main
from aijudge_admin.skill_rebuild import rebuild_skills
from aijudge_audit import AuditAction
from aijudge_core import (
    ArtifactKind,
    KcOutcome,
    MasteryModel,
    Role,
    Routing,
    SkillPoint,
    SkillState,
    Task,
    new_id,
)
from aijudge_core.ids import CriterionScoreId, KcId, SubmissionId, UserId
from aijudge_course_admin.operations import ensure_course
from aijudge_persistence import Database
from aijudge_submission import IncomingFile, InMemoryArtifactStore, SubmissionService

KC = KcId("kc_" + "5" * 32)
LEARNER = UserId("usr_" + "4" * 32)
TEACHER = UserId("usr_" + "8" * 32)
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path}/rebuild.db"


@pytest.fixture
def database(db_url: str):
    db = Database.connect(db_url, create=True)
    yield db
    db.dispose()


def _submit(
    database: Database, ratio: float, *, learner: UserId = LEARNER, role: Role = Role.LEARNER
) -> SubmissionId:
    """提出 1 件と、その採点（KC 付き）を作る。"""
    service = SubmissionService(database.unit_of_work, InMemoryArtifactStore())
    result = service.accept(
        tenant_id=TENANT,
        task_version_id=TASK_VERSION,
        learner_id=learner,
        submitted_as=role,
        subject_profile="cs_lang_c_intro",
        files=[
            IncomingFile(
                filename="main.c",
                kind=ArtifactKind.CODE,
                payload=f"int main(void){{return 0; /* {new_id('evt')} */}}".encode(),
            )
        ],
    )
    run = _run(result.submission.id, routing=Routing.AUTO)
    run = run.model_copy(
        update={
            "score_ratio": ratio,
            "kc_outcomes": (
                KcOutcome(
                    kc_id=KC,
                    score_ratio=ratio,
                    confidence=1.0,
                    criterion_score_ids=(CriterionScoreId(new_id("cs")),),
                ),
            ),
        }
    )
    with database.unit_of_work() as uow:
        uow.runs.save(run)
        uow.commit()
    return result.submission.id


@pytest.fixture
def world(database: Database):
    """誤答を 2 回出してから通った学習者 1 人と、教員の動作確認 1 件。

    習熟度には、**古い数え方で 3 件を積んだ値**と、そのときの推移の点を入れておく。
    """
    course, _ = ensure_course(
        database,
        tenant_id=TENANT,
        code="network",
        title="ネットワーク及び演習",
        term="2026-後期",
        subject_profile="cs_lang_c_intro",
        profiles_dir=PROFILES,
    )
    with database.unit_of_work() as uow:
        uow.tasks.save_task(Task(id=TASK_ID, course_id=course.id, title="ex03 p1", session=3))
        uow.tasks.save_version(_task_version())
        uow.commit()
    for ratio in (0.0, 0.0, 1.0):
        _submit(database, ratio)
    _submit(database, 1.0, learner=TEACHER, role=Role.INSTRUCTOR)

    old = datetime(2026, 10, 1, tzinfo=UTC)
    with database.unit_of_work() as uow:
        uow.skills.save_state(
            SkillState(
                tenant_id=TENANT,
                learner_id=LEARNER,
                kc_id=KC,
                mastery=0.31,
                observation_count=3,
                updated_at=old,
            )
        )
        uow.skills.append_point(
            SkillPoint(
                tenant_id=TENANT,
                learner_id=LEARNER,
                kc_id=KC,
                mastery=0.31,
                observation_count=3,
                model=MasteryModel.BKT,
                recorded_at=old,
            )
        )
        uow.commit()
    return course


def _state(database: Database, learner: UserId = LEARNER) -> SkillState | None:
    with database.unit_of_work() as uow:
        return uow.skills.get_state(TENANT, learner, KC)


def test_only_the_final_submission_of_a_task_counts(database: Database, world) -> None:
    report = rebuild_skills(database, TENANT, now=NOW)

    state = _state(database)
    assert state.observation_count == 1
    # 演習の正答 1 件ぶん（初期 0.20 → 0.456）。誤答 2 件は数えない。
    assert state.mastery == pytest.approx(0.456)
    assert report.learners == 1


def test_an_exam_task_is_refolded_with_the_exam_parameters(database: Database, world) -> None:
    with database.unit_of_work() as uow:
        task = uow.tasks.get_task(TASK_ID)
        uow.tasks.save_task(task.model_copy(update={"exam": True}))
        uow.commit()

    rebuild_skills(database, TENANT, now=NOW)

    state = _state(database)
    assert state.mastery == pytest.approx(0.696)
    assert state.evidence[-1].exam is True


def test_a_trial_submission_is_not_replayed(database: Database, world) -> None:
    rebuild_skills(database, TENANT, now=NOW)
    assert _state(database, TEACHER) is None


def test_past_points_are_kept_and_the_rebuild_is_recorded(database: Database, world) -> None:
    rebuild_skills(database, TENANT, now=NOW)

    with database.unit_of_work() as uow:
        points = uow.skills.history(TENANT, (LEARNER,))
        events = uow.audit.list_recent(TENANT, action=AuditAction.SKILLS_REBUILT)
    assert [point.observation_count for point in points] == [3, 1]
    assert points[-1].recorded_at == NOW
    assert len(events) == 1


def test_running_it_twice_gives_the_same_values(database: Database, world) -> None:
    rebuild_skills(database, TENANT, now=NOW)
    first = _state(database)
    rebuild_skills(database, TENANT, now=NOW)
    assert _state(database).mastery == pytest.approx(first.mastery)
    assert _state(database).observation_count == first.observation_count


def _cli(db_url: str, *args: str) -> int:
    base = ["--database-url", db_url, "--tenant", str(TENANT), "--profiles", str(PROFILES)]
    return main([*base, *args])


def test_the_command_asks_first(db_url: str, world, monkeypatch, capsys) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    code = _cli(db_url, "skill", "rebuild")
    assert code == 1
    assert "中止しました" in capsys.readouterr().out

    code = _cli(db_url, "skill", "rebuild", "--yes")
    assert code == 0
    assert "作り直しました" in capsys.readouterr().out
