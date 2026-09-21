"""画像を自動で読むことを、ルーブリックの編集画面が言う（#351）。

固定したいのは 4 つ。

言う           書き起こす科目では、何が読まれ、観点がその本文を見ると書く。
言わない       書き起こさない科目では出さない（関係のない注記は読まれなくなる）。
抽出器に訊く   拡張子の一覧は `applies_to` から作る。画面に対応表を持たない。
採点はしない   読む模型が段階を決めるのではないことを、同じ場所で言う。
"""

from __future__ import annotations

from test_manage import PROFILES, TENANT, World
from test_manage import world as world  # フィクスチャを借りる

from aijudge_admin import ensure_course
from aijudge_core import Role


def _image_course(world: World):
    """画像を書き起こす科目（`cs_network_python` の `input.transcription`）。"""
    course, _ = ensure_course(
        world.database,
        tenant_id=TENANT,
        code="network",
        title="ネットワーク及び演習",
        term="2025-後期",
        subject_profile="cs_network_python",
        profiles_dir=PROFILES,
    )
    return course


def test_the_course_rubric_says_images_are_read_at_intake(world: World) -> None:
    """**画面が黙っていると、教員は画像の観点を「人が採点する」に倒す。**"""
    course = _image_course(world)
    world.register("teacher", Role.INSTRUCTOR, course.id)

    page = world.client("teacher").get(f"/manage/courses/{course.id}").text

    assert "受付のときに本文へ自動で書き起こされます" in page
    assert "image_text" in page
    # 観点が読むのは書き起こした本文である、と言っていること。
    assert "観点が読むのはその本文です" in page


def _note_of(page: str) -> str:
    """注記の中だけを取り出す。

    拡張子は受付形式の選択欄にも出るので、ページ全体で探すと**注記が空でも
    通ってしまう**。
    """
    hit = page.index("受付のときに本文へ自動で書き起こされます")
    # 拡張子はこの文言より**前**に出る（「.png … の提出は、受付のときに…」）。
    head = page.rindex('<div class="note">', 0, hit)
    return page[head : page.index("</div>", hit)]


def test_the_note_names_the_suffixes_the_extractor_handles(world: World) -> None:
    """**対応表は画面に書かない。** 抽出器に訊く（`applies_to`）。"""
    course = _image_course(world)
    world.register("teacher", Role.INSTRUCTOR, course.id)

    note = _note_of(world.client("teacher").get(f"/manage/courses/{course.id}").text)

    assert ".png" in note
    assert ".jpg" in note


def test_the_task_editor_says_it_too(world: World) -> None:
    """**観点に評価器を割り当てるのは課題の画面でもある。**

    混在コースがあるので、見るのは**課題のプロファイル**（#195・#264）。
    """
    course = _image_course(world)
    world.register("teacher", Role.INSTRUCTOR, course.id)
    unit = "ex1"

    page = (
        world.client("teacher")
        .get(f"/manage/courses/{course.id}/units/{unit}/tasks/new")
        .text
    )

    assert "受付のときに本文へ自動で書き起こされます" in page
    assert "image_text" in _note_of(page)


def test_the_note_says_the_model_does_not_decide_the_level(world: World) -> None:
    """書き起こしと採点は別である（ADR 0021）。**同じ場所で言う。**"""
    course = _image_course(world)
    world.register("teacher", Role.INSTRUCTOR, course.id)

    page = world.client("teacher").get(f"/manage/courses/{course.id}").text

    assert "段階を決めません" in page
    assert "text_pattern_check" in page


def test_a_course_that_transcribes_nothing_says_nothing(world: World) -> None:
    """関係のない注記を出すと、注記そのものが読まれなくなる。

    既定のコースは `cs_lang_c_intro`（`input.transcription` を持たない）。
    """
    world.register("teacher", Role.INSTRUCTOR)

    page = world.client("teacher").get(f"/manage/courses/{world.course.id}").text

    assert "受付のときに本文へ自動で書き起こされます" not in page
