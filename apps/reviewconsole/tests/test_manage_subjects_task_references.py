"""課題だけから参照されているプロファイルも「使われている」と数える（2026-11-14・#691）。

課題は自分の科目プロファイルを持てる（#195）。参照をコースの既定（`Course.subject_profile`）
だけで判定すると、課題だけが指すプロファイルが「未参照」に見え、管理者の改名・保存が通って、
その課題への提出が「科目プロファイルがありません」で恒久的に採点できなくなる。

固定したいのは 3 つ。

守る        課題だけが指すプロファイルは、保存も改名も 400 になり、ファイルは変わらない
他テナント   別テナントのコースの課題だけが指すものも同じ（参照はテナントを越えて数える）
数え直す    課題の参照が無いプロファイルは、これまでどおり編集・改名できる
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from test_manage import PROFILES, World

from aijudge_authoring import TaskSpec
from aijudge_core import Course, Role
from aijudge_core.ids import CourseId, TenantId, UserId
from aijudge_course_admin.authoring import save_task

OTHER_PROFILE = "report_ja"


@pytest.fixture
def world(tmp_path: Path):
    instance = World(tmp_path)
    # 作業用の複製を置き場にする（本物の見本を書き換えない）。
    profiles = tmp_path / "subjects"
    profiles.mkdir()
    for path in PROFILES.glob("*.yaml"):
        shutil.copy(path, profiles / path.name)
    instance.console.profiles_dir = profiles
    instance.profiles = profiles  # type: ignore[attr-defined]
    yield instance
    instance.close()


def _task_pointing_at(world: World, course: Course, profile: str) -> None:
    save_task(
        world.database,
        course_id=course.id,
        spec=TaskSpec(
            key="rep1/essay",
            statement="## [必須] 小レポート ##\n\n本文",
            unit="rep1",
            subject_profile=profile,
            readability_weight=0.3,
        ),
        subject_profile=course.subject_profile,
        authored_by=UserId("usr_" + "a" * 32),
    )


def test_a_profile_only_a_task_points_at_cannot_be_saved(world: World) -> None:
    _task_pointing_at(world, world.course, OTHER_PROFILE)
    world.register("boss", Role.ADMIN)
    path = world.profiles / f"{OTHER_PROFILE}.yaml"  # type: ignore[attr-defined]
    before = path.read_text(encoding="utf-8")

    response = world.client("boss").post(
        f"/manage/subjects/{OTHER_PROFILE}", data={"text": f"name: {OTHER_PROFILE}\n"}
    )

    assert response.status_code == 400
    assert path.read_text(encoding="utf-8") == before, "ファイルが変わった"


def test_a_profile_only_a_task_points_at_cannot_be_renamed(world: World) -> None:
    _task_pointing_at(world, world.course, OTHER_PROFILE)
    world.register("boss", Role.ADMIN)
    path = world.profiles / f"{OTHER_PROFILE}.yaml"  # type: ignore[attr-defined]

    response = world.client("boss").post(
        f"/manage/subjects/{OTHER_PROFILE}/rename", data={"new_name": "report_ja2"}
    )

    assert response.status_code == 400
    assert path.exists(), "改名で元のファイルが消えた"
    assert not (world.profiles / "report_ja2.yaml").exists()  # type: ignore[attr-defined]


def test_a_task_in_another_tenants_course_counts_too(world: World) -> None:
    """参照はテナントを越えて数える（プロファイルのファイルはテナントの外にある）。"""
    other = Course(
        id=CourseId("crs_" + "9" * 32),
        tenant_id=TenantId("ten_" + "9" * 32),
        code="other",
        title="別テナントのコース",
        term="2026-後期",
        subject_profile=world.course.subject_profile,
    )
    with world.database.unit_of_work() as uow:
        uow.identity.save_course(other)
        uow.commit()
    _task_pointing_at(world, other, OTHER_PROFILE)
    world.register("boss", Role.ADMIN)

    response = world.client("boss").post(
        f"/manage/subjects/{OTHER_PROFILE}/rename", data={"new_name": "report_ja2"}
    )

    assert response.status_code == 400


def test_the_detail_page_says_the_profile_is_in_use(world: World) -> None:
    """画面も同じ判定を使う。「編集できます」と出しておいて、保存で断る形にしない。"""
    _task_pointing_at(world, world.course, OTHER_PROFILE)
    world.register("boss", Role.ADMIN)

    body = world.client("boss").get(f"/manage/subjects/{OTHER_PROFILE}").text

    assert world.course.code in body, "参照しているコースが出ていない"
    assert "コースが使っています" in body or "読み取り専用" in body


def test_a_profile_no_task_points_at_can_still_be_renamed(world: World) -> None:
    world.register("boss", Role.ADMIN)
    spare = world.profiles / "spare.yaml"  # type: ignore[attr-defined]
    spare.write_text("name: spare\ndeterministic: []\n", encoding="utf-8")

    response = world.client("boss").post(
        "/manage/subjects/spare/rename", data={"new_name": "spare2"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert not spare.exists()
