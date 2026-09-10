"""コースの複製（#170）。

固定したいのは 3 つ。

1. **元のコースが変わらない。** 複製は「作る」操作で、元に触ってはいけない
2. **引き継ぐものと引き継がないものの境目。** 設定と問題セットは写し、
   日程・受講登録・未承認の課題は写さない（運用の判断・issue のコメント）
3. **画像が複製先の鍵で引けること。** 画像の URL は権限判定の材料なので、
   書き換えないと複製先の誰にも見えない
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aijudge_admin import (
    AdminError,
    duplicate_course,
    ensure_course,
    register_kc,
    save_task,
)
from aijudge_authoring import TaskSpec, images
from aijudge_core import ReviewState, Role
from aijudge_core.ids import TenantId, UserId
from aijudge_identity import AuthService
from aijudge_persistence import Database
from aijudge_submission import InMemoryArtifactStore

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
TEACHER = UserId("usr_" + "1" * 32)

_PNG = bytes.fromhex("89504e470d0a1a0a") + b"0" * 24


@pytest.fixture
def world(tmp_path: Path):
    database = Database.connect(f"sqlite+pysqlite:///{tmp_path}/a.db", create=True)
    course, _ = ensure_course(
        database,
        tenant_id=TENANT,
        code="prog2",
        title="プログラミング演習",
        term="2026-前期",
        subject_profile="cs_lang_c_intro",
        profiles_dir=PROFILES,
    )
    yield database, course
    database.dispose()


def _spec(key: str = "p1", statement: str = "本文", **extra) -> TaskSpec:
    return TaskSpec(key=key, statement=statement, title=f"課題 {key}", unit="ex01", **extra)


def _duplicate(database, course, *, code="prog2", term="2026-後期", store=None):
    return duplicate_course(
        database,
        source_id=course.id,
        code=code,
        title="プログラミング演習",
        term=term,
        authored_by=TEACHER,
        profiles_dir=PROFILES,
        artifact_store=store,
    )


def test_the_settings_are_carried_over(world) -> None:
    """**設定は丸ごと引き継ぐ。** 学期が変わっても採点の仕方は変わらない。"""
    database, course = world
    with database.unit_of_work() as uow:
        uow.identity.save_course(
            course.model_copy(
                update={
                    "description": "到達目標",
                    "auto_finalize_after_minutes": 4320,
                    "upload_suffixes": (".c",),
                    "grading_overrides": {"timeout_seconds": 30},
                }
            )
        )
        uow.commit()

    copied = _duplicate(database, course)

    assert copied.course.id != course.id
    assert copied.course.term == "2026-後期"
    assert copied.course.subject_profile == course.subject_profile
    assert copied.course.description == "到達目標"
    assert copied.course.auto_finalize_after_minutes == 4320
    assert copied.course.upload_suffixes == (".c",)
    assert copied.course.grading_overrides == {"timeout_seconds": 30}


def test_the_source_course_is_untouched(world) -> None:
    database, course = world
    save_task(
        database,
        course_id=course.id,
        spec=_spec(),
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
    )

    _duplicate(database, course)

    with database.unit_of_work() as uow:
        again = uow.identity.get_course(course.id)
        tasks = uow.tasks.list_for_course(course.id)
    assert again is not None
    assert again.code == "prog2"
    assert again.term == "2026-前期"
    assert len(tasks) == 1


def test_the_tasks_are_copied_with_their_keys(world) -> None:
    """鍵が同じなら**複製を二度流しても課題は増えない**（`save_task` の冪等性）。"""
    database, course = world
    for key in ("p1", "p2"):
        save_task(
            database,
            course_id=course.id,
            spec=_spec(key),
            subject_profile=course.subject_profile,
            authored_by=TEACHER,
        )

    copied = _duplicate(database, course)

    assert copied.tasks == 2
    with database.unit_of_work() as uow:
        tasks = uow.tasks.list_for_course(copied.course.id)
        keys = sorted(uow.tasks.latest_published_version(task.id).source_key for task in tasks)
    assert keys == ["p1", "p2"]


def test_the_schedule_is_not_carried_over(world) -> None:
    """**元の学期の日付を持ち込まない。** 持ち込むと初日から全課題が締切済み。"""
    database, course = world
    save_task(
        database,
        course_id=course.id,
        spec=_spec(
            opens_at=datetime(2026, 4, 10, tzinfo=UTC),
            due_at=datetime(2026, 4, 10, tzinfo=UTC) + timedelta(days=7),
        ),
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
    )

    copied = _duplicate(database, course)

    with database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(copied.course.id)[0]
    assert task.opens_at is None
    assert task.due_at is None


def test_nobody_is_enrolled_in_the_copy(world) -> None:
    """学期が変われば履修者は変わる。**受講登録は空にする。**"""
    database, course = world
    with database.unit_of_work() as uow:
        auth = AuthService(uow.identity, audit=uow.audit)
        learner = auth.register(
            tenant_id=TENANT, login="s2400001", display_name="学生", password="x" * 12
        )
        auth.enroll(
            tenant_id=TENANT, course_id=course.id, user_id=learner.user_id, role=Role.LEARNER
        )
        uow.commit()

    copied = _duplicate(database, course)

    with database.unit_of_work() as uow:
        assert uow.identity.list_enrollments(copied.course.id) == ()
        assert len(uow.identity.list_enrollments(course.id)) == 1


def test_an_unapproved_task_is_not_copied(world) -> None:
    """下書きは「まだ出すと決めていないもの」。**新しい学期に持ち込まない。**"""
    database, course = world
    save_task(
        database,
        course_id=course.id,
        spec=_spec("p1"),
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
    )
    save_task(
        database,
        course_id=course.id,
        spec=_spec("p2"),
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
        review_state=ReviewState.DRAFT,
    )

    copied = _duplicate(database, course)

    assert copied.tasks == 1
    # **黙って減らさない。** 写さなかったものは理由つきで返す。
    assert [reason for _title, reason in copied.skipped] == ["未承認です"]


def test_the_copy_is_approved(world) -> None:
    """元は同じ教員のコースで、中身はその人が読んで承認したもの。"""
    database, course = world
    save_task(
        database,
        course_id=course.id,
        spec=_spec(),
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
    )

    copied = _duplicate(database, course)

    with database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(copied.course.id)[0]
        version = uow.tasks.latest_published_version(task.id)
    assert version.is_published


def test_the_knowledge_components_survive_the_copy(world) -> None:
    """**Q-matrix が欠けた課題を作らない**（設計原則 P6）。

    正準キーは版に入っていない（Q-matrix が持つのは ID で、ID はキーからの
    一方向の値）。体系から引き直さないと、複製先の課題は同じ問いなのに
    習熟度が積まれないものになる。
    """
    database, course = world
    # 分野と単位は骨格が決めるので `seeding` で入れる。教員が足せるのは
    # 知識要素（第 3 階層）だけ。
    for key, label in (("cs.loops", "繰り返し"), ("cs.loops.control", "制御")):
        register_kc(database, key=key, label=label, namespaces=("cs",), seeding=True)
    register_kc(database, key="cs.loops.control.termination", label="停止条件", namespaces=("cs",))
    save_task(
        database,
        course_id=course.id,
        spec=_spec(knowledge_components=("cs.loops.control.termination",)),
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
    )

    copied = _duplicate(database, course)

    with database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(copied.course.id)[0]
        version = uow.tasks.latest_published_version(task.id)
        kc = uow.skills.get_kc(version.q_matrix[0].kc_id)
    assert kc is not None
    assert kc.key == "cs.loops.control.termination"


def test_the_statement_images_follow_the_copy(world) -> None:
    """**画像の URL は権限判定の材料である。**

    返す側は URL 中のコース ID で「採点できる人か」を判定する
    （`/images/<course>/<name>`）。書き換えないと、複製先の教員・TA・学習者に
    はその画像が 404 になり、元コースを消せば全員に消える。
    """
    database, course = world
    store = InMemoryArtifactStore()
    name = images.new_name(_PNG, "fig1.png")
    store.put(images.storage_key(str(course.id), name), _PNG)
    save_task(
        database,
        course_id=course.id,
        spec=_spec(statement=f"本文\n\n{images.markdown_for(str(course.id), name)}\n"),
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
    )

    copied = _duplicate(database, course, store=store)

    assert copied.images == 1
    with database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(copied.course.id)[0]
        version = uow.tasks.latest_published_version(task.id)
    assert images.url_for(str(copied.course.id), name) in version.statement
    assert str(course.id) not in version.statement
    # 元の画像も残っている（元コースはそのまま動く）。
    assert store.exists(images.storage_key(str(course.id), name))
    assert store.exists(images.storage_key(str(copied.course.id), name))


def test_the_same_code_and_term_is_refused(world) -> None:
    """**同じ組では複製にならない。** そのまま作ると元のコースを上書きする。"""
    database, course = world

    with pytest.raises(AdminError) as exc:
        _duplicate(database, course, term="2026-前期")

    assert "コードと学期" in str(exc.value)
    with database.unit_of_work() as uow:
        assert uow.identity.get_course(course.id).title == "プログラミング演習"


def test_an_existing_course_is_not_overwritten(world) -> None:
    """元とは違う組でも、その組の**別のコースが既にあれば**断る。"""
    database, course = world
    ensure_course(
        database,
        tenant_id=TENANT,
        code="prog3",
        title="別の授業",
        term="2026-後期",
        subject_profile="cs_lang_c_intro",
        profiles_dir=PROFILES,
    )

    with pytest.raises(AdminError, match="既にあります"):
        _duplicate(database, course, code="prog3", term="2026-後期")

    with database.unit_of_work() as uow:
        courses = {c.code: c.title for c in uow.identity.list_courses(TENANT)}
    assert courses["prog3"] == "別の授業"


def test_a_free_text_term_is_refused_here_too(world) -> None:
    """検査は `ensure_course` 1 か所（#167）。**複製にも同じ検査が効く。**"""
    database, course = world

    with pytest.raises(AdminError, match="学期"):
        _duplicate(database, course, term="2026後期")
