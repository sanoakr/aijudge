"""採点のプロファイルは課題が決める（#195）。

**器は最初から課題側にあった。** `TaskVersion.subject_profile` は実在するのに、
常にコースの値の写しが入り、誰にも読まれていなかった。その結果、1 つのコースに
種類の違う課題を置けなかった ── レポートとプログラムが混在する科目は実在する
のに、2 コースに割るしかなかった。

**コースのプロファイルは残る。役割が違う。** コースは語彙（`kc_namespaces`）と
新しい課題の既定を決め、課題は採点を決める。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aijudge_admin import ensure_course
from aijudge_admin.authoring import save_task
from aijudge_authoring import TaskSpec
from aijudge_core.ids import TenantId, UserId
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
TEACHER = UserId("usr_" + "a" * 32)


@pytest.fixture
def database(tmp_path: Path):
    db = Database.connect(f"sqlite+pysqlite:///{tmp_path}/mixed.db", create=True)
    yield db
    db.dispose()


@pytest.fixture
def course(database: Database):
    obj, _ = ensure_course(
        database,
        tenant_id=TENANT,
        code="mixed",
        title="レポートとプログラムが混ざる科目",
        term="2026-前期",
        subject_profile="cs_lang_c_intro",
        profiles_dir=PROFILES,
    )
    return obj


def _save(database: Database, course, key: str, profile: str | None):
    return save_task(
        database,
        course_id=course.id,
        spec=TaskSpec(
            key=key,
            statement=f"## [必須] {key} ##\n\n本文",
            subject_profile=profile,
            readability_weight=0.3,
        ),
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
    )


def test_two_kinds_of_task_can_live_in_one_course(database: Database, course) -> None:
    """**同じコースで、課題ごとに違うプロファイルで採点される。**

    これが要件の全部である。以前はコースを 2 つに割るしかなかった。
    """
    program = _save(database, course, "p1", None)
    report = _save(database, course, "r1", "report_ja")

    assert program.version.subject_profile == "cs_lang_c_intro"
    assert report.version.subject_profile == "report_ja"


def test_a_task_without_a_profile_still_gets_the_courses(database: Database, course) -> None:
    """指定が無ければコースの既定。**既存の課題は何も変わらない。**

    移行を要らなくするための性質である ── いま入っている課題は誰も指定して
    いないので、これまでと同じ値で採点され続ける。
    """
    saved = _save(database, course, "p2", None)
    assert saved.version.subject_profile == course.subject_profile


def test_revising_a_task_keeps_its_own_profile(database: Database, course) -> None:
    """訂正で版を上げても、課題のプロファイルは課題のもののまま。

    **ここが落ちると、直した瞬間にコースの既定へ戻る。** 混在するコースでは
    レポートが C の評価器で採点されることになり、しかも訂正するまでは
    正しく動いていたので原因が分かりにくい。
    """
    first = _save(database, course, "r2", "report_ja")
    revised = save_task(
        database,
        course_id=course.id,
        spec=TaskSpec(
            key="r2",
            statement="## [必須] r2 ##\n\n本文を直した",
            subject_profile="report_ja",
            readability_weight=0.3,
        ),
        subject_profile=course.subject_profile,
        authored_by=TEACHER,
        revise=True,
    )

    assert revised.version.version == first.version.version + 1
    assert revised.version.subject_profile == "report_ja"
