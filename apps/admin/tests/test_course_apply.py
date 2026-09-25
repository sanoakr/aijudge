"""コースの定義（YAML）から作る（`course apply`）。

`demo seed` の一般化である。学期初めに「コースを作り、課題を取り込み、
日程を入れる」を 1 つのファイルから流せること、そして Sharif Judge 形式の
既存の課題ディレクトリをその定義から指せることを確かめる。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_admin.course_definition import apply_course_definition, load_course_definition
from aijudge_admin.operations import AdminError, ensure_course
from aijudge_core import HUMAN_SCORED, AnswerMode
from aijudge_core.ids import TenantId, UserId
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
AUTHOR = UserId("usr_" + "a" * 32)

DEFINITION = """
course:
  code: network
  title: ネットワーク及び演習
  term: 2026-後期
  subject_profile: cs_network_python
  description: Python によるネットワークプログラミングの演習。
  upload_suffixes: [.py]
units:
  ex1:
    opens_at: 2026-09-18T13:00:00+09:00
    due_at: 2026-09-25T23:59:00+09:00
tasks:
  - key: ex1/cert
    unit: ex1
    session: 1
    position: 1
    statement: |
      ## [必須] 認定証を提出する ##

      認定証の画面キャプチャを提出してください。
    accepted_suffixes: [.png, .jpg]
    criteria:
      - code: shown
        title: 認定証が写っているか
        description: 講座名と学籍番号入りのニックネームが読めるか。
        weight: 1.0
        evaluator: __human__
        levels:
          - {level: 0, label: 不足, descriptor: 読めない, score_ratio: 0.0}
          - {level: 1, label: 十分, descriptor: 読める, score_ratio: 1.0}
  - problem_dir: ex1/p2
    readability_weight: 0.3
  - problem_dir: ex1/p3
    due_at: 2026-10-02T23:59:00+09:00
"""

DESC = "## [必須] hello.py ##\n\nHello と出力する。\n"
DESC3 = DESC.replace("hello.py", "hello2.py")


@pytest.fixture
def database(tmp_path: Path):
    db = Database.connect(f"sqlite+pysqlite:///{tmp_path}/apply.db", create=True)
    yield db
    db.dispose()


def _write_definition(root: Path, text: str = DEFINITION) -> Path:
    for problem in ("p2", "p3"):
        problem_dir = root / "ex1" / problem
        (problem_dir / "in").mkdir(parents=True)
        (problem_dir / "out").mkdir()
        (problem_dir / "desc.md").write_text(DESC if problem == "p2" else DESC3, encoding="utf-8")
        (problem_dir / "hello.py").write_text('print("Hello")\n', encoding="utf-8")
        (problem_dir / "in" / "input1.txt").write_text("", encoding="utf-8")
        (problem_dir / "out" / "output1.txt").write_text("Hello\n", encoding="utf-8")
    path = root / "course.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _apply(database: Database, path: Path):
    return apply_course_definition(
        database, path, tenant_id=TENANT, profiles_dir=PROFILES, authored_by=AUTHOR
    )


def _tasks(database: Database, course_id):
    with database.unit_of_work() as uow:
        tasks = {task.title: task for task in uow.tasks.list_for_course(course_id)}
        versions = {title: uow.tasks.latest_version(task.id) for title, task in tasks.items()}
    return tasks, versions


def test_problem_dir_fills_what_the_yaml_does_not_say(tmp_path: Path) -> None:
    """Sharif Judge 形式のディレクトリから、問題文・テスト・参照解答・回・順序を読む。"""
    definition = load_course_definition(_write_definition(tmp_path))
    by_key = {task.key: task for task in definition.tasks}

    imported = by_key["ex1/p2"]
    assert imported.statement == DESC
    assert imported.unit == "ex1" and imported.session == 1 and imported.position == 2
    assert imported.reference_solution == 'print("Hello")\n'
    assert [(case.input, case.expected) for case in imported.test_cases] == [("", "Hello\n")]
    assert imported.readability_weight == 0.3


def test_unit_schedule_is_the_default_and_the_task_wins(tmp_path: Path) -> None:
    """`units:` の日程は回の既定で、課題が自分の値を書けばそちらが勝つ。"""
    definition = load_course_definition(_write_definition(tmp_path))
    by_key = {task.key: task for task in definition.tasks}

    opens = datetime(2026, 9, 18, 4, 0, tzinfo=UTC)
    assert by_key["ex1/cert"].opens_at == opens
    assert by_key["ex1/p2"].opens_at == opens
    assert by_key["ex1/p2"].due_at == datetime(2026, 9, 25, 14, 59, tzinfo=UTC)
    assert by_key["ex1/p3"].due_at == datetime(2026, 10, 2, 14, 59, tzinfo=UTC)


def test_apply_creates_the_course_with_its_settings_and_tasks(database: Database, tmp_path) -> None:
    result = _apply(database, _write_definition(tmp_path))
    assert result.created and result.tasks == 3
    assert result.course.description.startswith("Python による")
    assert result.course.upload_suffixes == (".py",)

    tasks, versions = _tasks(database, result.course.id)
    assert len(tasks) == 3
    cert = tasks["認定証を提出する"]
    assert all(c.evaluator_id == HUMAN_SCORED for c in versions[cert.title].criteria)
    assert set(cert.accepted_suffixes) == {".png", ".jpg"}
    assert cert.due_at is not None


def test_apply_is_idempotent(database: Database, tmp_path: Path) -> None:
    path = _write_definition(tmp_path)
    first = _apply(database, path)
    second = _apply(database, path)
    assert not second.created
    assert first.course.id == second.course.id
    with database.unit_of_work() as uow:
        assert len(uow.tasks.list_for_course(second.course.id)) == 3


def test_ensure_course_keeps_the_settings_of_an_existing_course(
    database: Database, tmp_path: Path
) -> None:
    """流し直しても、ブラウザで入れた運用値（概要など）が既定に戻らない。"""
    result = _apply(database, _write_definition(tmp_path))
    course, created = ensure_course(
        database,
        tenant_id=TENANT,
        code="network",
        title="ネットワーク及び演習（改）",
        term="2026-後期",
        subject_profile="cs_network_python",
        profiles_dir=PROFILES,
    )
    assert not created and course.id == result.course.id
    assert course.title == "ネットワーク及び演習（改）"
    assert course.description == result.course.description
    assert course.upload_suffixes == (".py",)


def test_a_path_outside_the_definition_is_refused(tmp_path: Path) -> None:
    text = DEFINITION.replace("problem_dir: ex1/p2", "problem_dir: ../elsewhere")
    with pytest.raises(AdminError, match="外"):
        load_course_definition(_write_definition(tmp_path, text))


def test_a_missing_problem_dir_is_named(tmp_path: Path) -> None:
    text = DEFINITION.replace("problem_dir: ex1/p3", "problem_dir: ex1/p9")
    with pytest.raises(AdminError, match="p9"):
        load_course_definition(_write_definition(tmp_path, text))


def test_the_template_is_a_valid_definition(tmp_path: Path) -> None:
    """配るひな形は、そのまま流せる形でなければならない（教員が埋めて管理者に渡す）。"""
    from aijudge_admin.course_definition import course_template

    text = course_template()
    for problem in ("p2", "p3"):
        problem_dir = tmp_path / "ex1" / problem
        (problem_dir / "in").mkdir(parents=True)
        (problem_dir / "out").mkdir()
        (problem_dir / "desc.md").write_text(DESC, encoding="utf-8")
        (problem_dir / "in" / "input1.txt").write_text("", encoding="utf-8")
        (problem_dir / "out" / "output1.txt").write_text("Hello\n", encoding="utf-8")
    path = tmp_path / "course.yaml"
    path.write_text(text, encoding="utf-8")

    definition = load_course_definition(path)
    assert definition.course["code"] == "network"
    assert [task.key for task in definition.tasks] == ["ex1/p1", "ex1/p2", "ex1/p3", "ex2/p1"]
    assert definition.tasks[3].test_cases[0].expected == "6\n"


def _with_unit_setting(setting: str) -> str:
    return DEFINITION.replace(
        "    due_at: 2026-09-25T23:59:00+09:00\n",
        f"    due_at: 2026-09-25T23:59:00+09:00\n    {setting}\n",
        1,
    )


def test_unit_answer_mode_goes_to_every_task_of_the_set(database: Database, tmp_path) -> None:
    """`units` の答え方は問題セットの値で、回の全課題に入る（ADR 0026・`/manage` と同じ）。

    認定証（画像）を含む回では `editor` にできないので、画像の課題を除いた回で確かめる。
    """
    text = _with_unit_setting("answer_mode: editor\n    editor_completion: true").replace(
        "  - key: ex1/cert\n    unit: ex1\n", "  - key: ex1/cert\n    unit: ex0\n"
    )
    result = _apply(database, _write_definition(tmp_path, text))

    tasks, _versions = _tasks(database, result.course.id)
    in_set = [task for task in tasks.values() if task.unit == "ex1"]
    assert len(in_set) == 2
    assert all(task.answer_mode is AnswerMode.EDITOR for task in in_set)
    assert all(task.editor_completion for task in in_set)
    # 書かれていない回は触らない。
    assert tasks["認定証を提出する"].answer_mode is AnswerMode.UPLOAD


def _only_the_certificate_in_ex1(text: str) -> str:
    """ex1 に認定証（画像）だけを残す。p2・p3 は別の回へ。"""
    return text.replace(
        "  - problem_dir: ex1/p2\n", "  - problem_dir: ex1/p2\n    unit: ex9\n"
    ).replace("  - problem_dir: ex1/p3\n", "  - problem_dir: ex1/p3\n    unit: ex9\n")


def test_a_set_that_cannot_open_in_the_editor_is_refused(database: Database, tmp_path) -> None:
    """**画面と同じ検査を通す**（`editor_blockers`）。書ける課題が 1 つも無い回は断る。"""
    text = _only_the_certificate_in_ex1(_with_unit_setting("answer_mode: editor"))
    with pytest.raises(AdminError, match="エディタで書ける課題がありません"):
        _apply(database, _write_definition(tmp_path, text))


def test_a_mixed_set_opens_in_the_editor(database: Database, tmp_path) -> None:
    """認定証（画像）と .py の課題が混ざった回もエディタにできる（2026-09-25）。"""
    result = _apply(
        database, _write_definition(tmp_path, _with_unit_setting("answer_mode: editor"))
    )
    tasks, _versions = _tasks(database, result.course.id)
    assert all(
        task.answer_mode is AnswerMode.EDITOR for task in tasks.values() if task.unit == "ex1"
    )


def test_a_video_task_refuses_editor_only(database: Database, tmp_path) -> None:
    """動画を受ける課題がある回は、ファイル選択を止められない。"""
    text = _with_unit_setting("answer_mode: editor\n    file_upload: false").replace(
        "accepted_suffixes: [.png, .jpg]", "accepted_suffixes: [.png, .jpg, .mp4]"
    )
    with pytest.raises(AdminError, match="ファイル選択での提出を止められません"):
        _apply(database, _write_definition(tmp_path, text))


def test_a_misspelt_unit_setting_is_refused_before_anything_is_saved(tmp_path: Path) -> None:
    """値の誤りは**読む段で**落とす。課題を入れてから分かると、半分だけ入った定義が残る。"""
    with pytest.raises(AdminError, match="answer_mode"):
        load_course_definition(_write_definition(tmp_path, _with_unit_setting("answer_mode: ide")))
    with pytest.raises(AdminError, match="editor_completion"):
        load_course_definition(
            _write_definition(tmp_path / "b", _with_unit_setting("editor_completion: yes-please"))
        )


def test_a_unit_can_be_editor_only(database: Database, tmp_path) -> None:
    """`file_upload: false` でエディタだけの回にできる（試験）。"""
    text = _with_unit_setting("answer_mode: editor\n    file_upload: false").replace(
        "  - key: ex1/cert\n    unit: ex1\n", "  - key: ex1/cert\n    unit: ex0\n"
    )
    result = _apply(database, _write_definition(tmp_path, text))
    tasks, _versions = _tasks(database, result.course.id)
    in_set = [task for task in tasks.values() if task.unit == "ex1"]
    assert in_set and all(task.file_upload is False for task in in_set)


def test_turning_off_files_without_the_editor_is_refused(tmp_path: Path) -> None:
    """エディタも無くファイルも断ると誰も提出できない。**読む段で**落とす。"""
    with pytest.raises(AdminError, match="file_upload"):
        load_course_definition(
            _write_definition(tmp_path, _with_unit_setting("file_upload: false"))
        )


def test_a_unit_can_be_confidential_until_open(database: Database, tmp_path) -> None:
    """`confidential_until_open` は問題セットの値で、回の全課題に入る（試験）。

    画面でしか入れられないと、`course apply` から切り替えるまでの間、公開前の
    課題が TA に見える。書かれていない回は触らない。
    """
    text = _with_unit_setting("confidential_until_open: true").replace(
        "  - key: ex1/cert\n    unit: ex1\n", "  - key: ex1/cert\n    unit: ex0\n"
    )
    result = _apply(database, _write_definition(tmp_path, text))
    tasks, _versions = _tasks(database, result.course.id)
    in_set = [task for task in tasks.values() if task.unit == "ex1"]
    assert len(in_set) == 2
    assert all(task.confidential_until_open for task in in_set)
    assert tasks["認定証を提出する"].confidential_until_open is False


def test_confidential_survives_a_refused_editor_setting(database: Database, tmp_path) -> None:
    """**秘匿は、同じ回の答え方が断られても入る。**

    課題は答え方の検査より前に保存されるので、秘匿を答え方と同じ作業単位で
    入れると、断られたときに秘匿だけが巻き戻り、TA に見える課題が残る。
    """
    text = _only_the_certificate_in_ex1(
        _with_unit_setting("answer_mode: editor\n    confidential_until_open: true")
    )
    with pytest.raises(AdminError, match="エディタで書ける課題がありません"):
        _apply(database, _write_definition(tmp_path, text))
    with database.unit_of_work() as uow:
        course = next(iter(uow.identity.list_courses(TENANT)))
        in_set = [task for task in uow.tasks.list_for_course(course.id) if task.unit == "ex1"]
    assert len(in_set) == 1
    assert all(task.confidential_until_open for task in in_set)
    assert all(task.answer_mode is AnswerMode.UPLOAD for task in in_set)


def test_a_misspelt_confidential_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AdminError, match="confidential_until_open"):
        load_course_definition(
            _write_definition(tmp_path, _with_unit_setting("confidential_until_open: yes-please"))
        )


def test_leaving_confidential_out_keeps_what_the_console_set(database: Database, tmp_path) -> None:
    """書かない回は、画面で切り替えた値のまま（`answer_mode` と同じ）。"""
    path = _write_definition(tmp_path)
    result = _apply(database, path)
    with database.unit_of_work() as uow:
        for task in uow.tasks.list_for_course(result.course.id):
            uow.tasks.save_task(task.model_copy(update={"confidential_until_open": True}))
        uow.commit()
    _apply(database, path)
    tasks, _versions = _tasks(database, result.course.id)
    assert all(task.confidential_until_open for task in tasks.values())


def test_a_unit_carries_its_clear_points(database: Database, tmp_path) -> None:
    """`clear_points` は問題セットの値で、回の全課題に入る。"""
    result = _apply(database, _write_definition(tmp_path, _with_unit_setting("clear_points: 60")))
    tasks, _versions = _tasks(database, result.course.id)
    in_set = [task for task in tasks.values() if task.unit == "ex1"]
    assert in_set and all(task.clear_points == 60.0 for task in in_set)


def test_a_bad_clear_points_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AdminError, match="clear_points"):
        load_course_definition(_write_definition(tmp_path, _with_unit_setting("clear_points: -1")))
