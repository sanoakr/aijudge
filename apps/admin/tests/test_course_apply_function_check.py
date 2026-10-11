"""`course apply` の `problem_dir:` は `check/`（関数の検査）も読む（2026-10-11）。

固定したいのは 3 つ。

読む       `check/driver.c` と `check/out/` が、`c_function_check` あてのケースとして入る
残る       投入した版に、入出力のケースと並んで検査のケースが残る（保存で外れない）
黙らない   `check/` があるのに観点がどれも `c_function_check` を名指ししないなら断る
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_course_apply import AUTHOR, PROFILES, TENANT, _tasks
from test_course_apply import database as database  # フィクスチャを借りる

from aijudge_course_admin.course_definition import apply_course_definition, load_course_definition
from aijudge_course_admin.operations import AdminError
from aijudge_persistence import Database

CHECK = "c_function_check"
DRIVER = (
    "#include <stdio.h>\n\nint sqsum(int n);\n\n"
    'int main(void)\n{\n    printf("%d\\n", sqsum(3));\n    return 0;\n}\n'
)
REFERENCE = (
    "#include <stdio.h>\n\nint sqsum(int n)\n{\n    int s = 0;\n"
    "    for (int i = 1; i <= n; i++) {\n        s += i * i;\n    }\n    return s;\n}\n\n"
    'int main(void)\n{\n    int n;\n    scanf("%d", &n);\n    printf("%d\\n", sqsum(n));\n'
    "    return 0;\n}\n"
)
HEAD = """
course:
  code: prog2
  title: プログラミング及び実習2
  term: 2026-後期
  subject_profile: cs_lang_c_intro
  upload_suffixes: [.c]
tasks:
  - problem_dir: ex10/p2
    criteria:
"""
FUNCTION = """
      - code: function
        title: 関数の定義
        description: 指定の関数を定義しているか。
        weight: 0.1
        evaluator: c_function_check
        levels:
          - {level: 0, label: 未達, descriptor: 定義していない, score_ratio: 0.0}
          - {level: 1, label: 達成, descriptor: 定義している, score_ratio: 1.0}
"""
CORRECTNESS = """
      - code: correctness
        title: 出力の正しさ
        description: 仕様どおりに出力するか。
        weight: {weight}
        evaluator: code_test_runner
        levels:
          - {{level: 0, label: 未達, descriptor: 動かない, score_ratio: 0.0}}
          - {{level: 1, label: 達成, descriptor: 動く, score_ratio: 1.0}}
"""


def _write(root: Path, criteria: str) -> Path:
    problem = root / "ex10" / "p2"
    (problem / "in").mkdir(parents=True)
    (problem / "out").mkdir()
    (problem / "check" / "out").mkdir(parents=True)
    (problem / "desc.md").write_text("## 2 乗の和\n\n`sqsum()` を定義する。\n", encoding="utf-8")
    (problem / "sqsum.c").write_text(REFERENCE, encoding="utf-8")
    (problem / "in" / "input1.txt").write_text("2\n", encoding="utf-8")
    (problem / "out" / "output1.txt").write_text("5\n", encoding="utf-8")
    (problem / "check" / "driver.c").write_text(DRIVER, encoding="utf-8")
    (problem / "check" / "out" / "output1.txt").write_text("14\n", encoding="utf-8")
    path = root / "course.yaml"
    path.write_text(HEAD + criteria, encoding="utf-8")
    return path


def test_the_function_checks_are_read_with_their_evaluator(tmp_path: Path) -> None:
    path = _write(tmp_path, FUNCTION + CORRECTNESS.format(weight=0.9))
    (task,) = load_course_definition(path).tasks

    io, check = task.test_cases
    assert io.name == "case1" and io.evaluator is None
    assert check.name == "function1" and check.evaluator == CHECK
    assert check.payload == {"input": "", "expected": "14\n", "driver": DRIVER}
    assert check.hidden is True


def test_applying_keeps_the_function_checks_next_to_the_io_cases(
    database: Database, tmp_path: Path
) -> None:
    path = _write(tmp_path, FUNCTION + CORRECTNESS.format(weight=0.9))
    applied = apply_course_definition(
        database, path, tenant_id=TENANT, profiles_dir=PROFILES, authored_by=AUTHOR
    )

    _tasks_by_title, versions = _tasks(database, applied.course.id)
    (version,) = versions.values()
    assert [(case.name, case.evaluator_id) for case in version.test_cases] == [
        ("case1", "code_test_runner"),
        ("function1", CHECK),
    ]
    assert version.test_cases[1].payload["driver"] == DRIVER


def test_a_check_dir_without_a_function_criterion_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AdminError, match=CHECK):
        load_course_definition(_write(tmp_path, CORRECTNESS.format(weight=1.0)))


def test_a_driver_without_expected_output_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, FUNCTION + CORRECTNESS.format(weight=0.9))
    (tmp_path / "ex10" / "p2" / "check" / "out" / "output1.txt").unlink()
    with pytest.raises(AdminError, match="関数の検査が壊れています"):
        load_course_definition(path)


def test_a_criterion_the_course_does_not_run_is_refused_before_saving(
    database: Database, tmp_path: Path
) -> None:
    """コースが評価器の一覧を上書きしていて、そこに無い評価器を観点が指すなら断る
    （2026-10-11・prog2 は一覧を凍らせていて、足した評価器が走らなかった）。"""
    first = tmp_path / "first"
    first.mkdir()
    applied = apply_course_definition(
        database,
        _write(first, FUNCTION + CORRECTNESS.format(weight=0.9)),
        tenant_id=TENANT,
        profiles_dir=PROFILES,
        authored_by=AUTHOR,
    )
    with database.unit_of_work() as uow:
        course = uow.identity.get_course(applied.course.id)
        uow.identity.save_course(
            course.model_copy(update={"grading_overrides": {"deterministic": ["code_test_runner"]}})
        )
        uow.commit()

    second = tmp_path / "second"
    second.mkdir()
    with pytest.raises(AdminError, match="c_function_check"):
        apply_course_definition(
            database,
            _write(second, FUNCTION + CORRECTNESS.format(weight=0.9)),
            tenant_id=TENANT,
            profiles_dir=PROFILES,
            authored_by=AUTHOR,
            revise=True,
        )
