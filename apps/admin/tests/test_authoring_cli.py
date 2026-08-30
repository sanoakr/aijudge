"""作問とレビューの口を固定する（S2、設計方針 §5）。

固定したいのは 4 つ。

保存しても出題されない 生成物は IN_REVIEW で止まる（設計原則 P5）。
門が落ちても捨てない  捨てると、門が厳しすぎることに誰も気づけない。
ID を出さない        レビュー一覧は KC を可読なキーで出す。
理由なく却下できない  CLI からも塞がっている。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aijudge_admin.authoring_cli import cmd_task_review_decide, cmd_task_review_list
from aijudge_core import (
    Course,
    KnowledgeComponent,
    Provenance,
    QMatrixEntry,
    ReviewState,
    RubricCriterion,
    RubricLevel,
    Task,
    TaskVersion,
)
from aijudge_core.ids import (
    CourseId,
    CriterionId,
    KcId,
    TaskId,
    TaskVersionId,
    TenantId,
    UserId,
)
from aijudge_persistence import Database

TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
INSTRUCTOR = UserId("usr_" + "2" * 32)
VERSION = TaskVersionId("tsv_" + "3" * 32)
KC = KcId("kc_" + "4" * 32)


class Args:
    def __init__(self, database: Database, **kwargs) -> None:
        self.database_url = "sqlite+pysqlite:///:memory:"
        self.create_schema = True
        self._database = database
        for key, value in kwargs.items():
            setattr(self, key, value)


@pytest.fixture
def database(monkeypatch):
    made = Database.connect("sqlite+pysqlite:///:memory:", create=True)

    # CLI は自分で接続を開く。テストではインメモリ DB を共有させる。
    import aijudge_admin.authoring_cli as module

    monkeypatch.setattr(module, "_open", lambda args: made)
    monkeypatch.setattr(made, "dispose", lambda: None)

    with made.unit_of_work() as uow:
        uow.identity.save_course(
            Course(
                id=COURSE,
                tenant_id=TENANT,
                code="prog2",
                title="演習",
                term="2026",
                subject_profile="cs_intro_c",
            )
        )
        uow.skills.save_kc(
            KnowledgeComponent(
                id=KC, namespace="cs", path=("loops", "termination"), label="ループの停止"
            )
        )
        uow.tasks.save_task(Task(id=TaskId("tsk_" + "3" * 32), course_id=COURSE, title="生成課題"))
        uow.tasks.save_version(_version())
        uow.commit()
    yield made
    made.engine.dispose()


def _version() -> TaskVersion:
    return TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "3" * 32),
        version=1,
        subject_profile="cs_intro_c",
        statement="## 課題 ##\n\n書きなさい。",
        criteria=(
            RubricCriterion(
                id=CriterionId("crt_" + "5" * 32),
                code="correctness",
                title="正しさ",
                description="テスト実行で判定する。",
                weight=1.0,
                levels=(
                    RubricLevel(level=0, label="未達", descriptor="通らない", score_ratio=0.0),
                    RubricLevel(level=1, label="達成", descriptor="通る", score_ratio=1.0),
                ),
            ),
        ),
        q_matrix=(QMatrixEntry(task_version_id=VERSION, kc_id=KC),),
        max_score=100.0,
        provenance=Provenance(
            authored_by=INSTRUCTOR,
            generated_by="stub",
            generation_prompt_version="task_draft_ja@1",
            review_state=ReviewState.IN_REVIEW,
        ),
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
    )


def test_the_queue_shows_components_by_their_readable_key(database, capsys) -> None:
    """**ID のまま出さない。** `kc_4444…` は教員に何も伝えない。"""
    assert cmd_task_review_list(Args(database)) == 0
    out = capsys.readouterr().out
    assert "cs.loops.termination" in out
    assert str(KC) not in out


def test_approving_from_the_cli_publishes(database, capsys) -> None:
    args = Args(database, version=str(VERSION), reviewer=str(INSTRUCTOR), reject=False, reason=None)
    assert cmd_task_review_decide(args) == 0
    assert "approved" in capsys.readouterr().out

    with database.unit_of_work() as uow:
        assert uow.tasks.get_version(VERSION).is_published


def test_rejecting_without_a_reason_is_refused_from_the_cli(database, capsys) -> None:
    args = Args(database, version=str(VERSION), reviewer=str(INSTRUCTOR), reject=True, reason=None)
    assert cmd_task_review_decide(args) == 1
    assert "理由" in capsys.readouterr().err

    with database.unit_of_work() as uow:
        # 何も起きていない。
        assert uow.tasks.get_version(VERSION).provenance.review_state is ReviewState.IN_REVIEW


def test_rejecting_with_a_reason_records_it(database, capsys) -> None:
    args = Args(
        database,
        version=str(VERSION),
        reviewer=str(INSTRUCTOR),
        reject=True,
        reason="入出力の形式が課題文にない",
    )
    assert cmd_task_review_decide(args) == 0

    with database.unit_of_work() as uow:
        provenance = uow.tasks.get_version(VERSION).provenance
    assert provenance.review_state is ReviewState.REJECTED
    assert provenance.reject_reason == "入出力の形式が課題文にない"


def test_an_unregistered_component_is_named_as_such(database, capsys) -> None:
    """**黙って空にしない。** 「KC が無い課題」と区別が付かなくなる。"""
    with database.unit_of_work() as uow:
        stray = TaskVersionId("tsv_" + "9" * 32)
        version = _version().model_copy(
            update={
                "id": stray,
                "task_id": TaskId("tsk_" + "9" * 32),
                "q_matrix": (QMatrixEntry(task_version_id=stray, kc_id=KcId("kc_" + "e" * 32)),),
            }
        )
        uow.tasks.save_task(Task(id=version.task_id, course_id=COURSE, title="別"))
        uow.tasks.save_version(version)
        uow.commit()

    assert cmd_task_review_list(Args(database)) == 0
    assert "未登録" in capsys.readouterr().out
