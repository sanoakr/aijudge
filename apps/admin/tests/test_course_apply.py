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
from aijudge_core import HUMAN_SCORED
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
