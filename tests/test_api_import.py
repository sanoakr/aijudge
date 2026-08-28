"""API で入れた課題が**実際に採点できる**ことを確かめる。

型が通り、201 が返り、観点が 2 つ付いていても、それだけでは採点できるとは
言えない。実際にそうなった ── テストケースの payload のキー名を評価器が
読む名前と違えて書いていて、`payload.get("expected", "")` が既定の空文字と
比較され、**全ケースが黙って不合格になる**ところだった。例外は出ないので、
提出を 1 件通してみるまで分からない。

だからここでは、API で課題を入れて、参照解答を提出して、ワーカーに採点させ、
満点になることまで見る。リポジトリ直下の `tests/` にあるのは、どのパッケージ
にも属さない検証だから（複数のアプリを束ねて初めて意味がある）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aijudge_admin import ensure_course
from aijudge_core import ArtifactKind, Role
from aijudge_core.ids import TenantId
from aijudge_grader import GradingWorker
from aijudge_grading import EvaluatorRegistry
from aijudge_identity import AuthService
from aijudge_persistence import Database
from aijudge_reviewconsole import Console
from aijudge_reviewconsole import create_app as review_app
from aijudge_submission import FilesystemArtifactStore, IncomingFile, SubmissionService

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
PASSWORD = "correct horse battery"

needs_c_compiler = pytest.mark.skipif(
    shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="no C compiler available",
)

STATEMENT = "## [必須] 二数の和 ##\n\n2 つの整数を読み、その和を出力しなさい。"

SOLUTION = """\
#include <stdio.h>
int main(void) {
    int a, b;
    if (scanf("%d %d", &a, &b) != 2) return 1;
    printf("%d\\n", a + b);
    return 0;
}
"""

WRONG = """\
#include <stdio.h>
int main(void) {
    int a, b;
    if (scanf("%d %d", &a, &b) != 2) return 1;
    printf("%d\\n", a - b);
    return 0;
}
"""

SPEC = {
    "key": "api/sum",
    "statement": STATEMENT,
    "unit": "api",
    "session": 1,
    "position": 1,
    # 決定的評価だけを見たいので AI 観点は付けない（S6 に依存させない）。
    "readability_weight": 0.0,
    "test_cases": [
        {"name": "1", "input": "1 2\n", "expected": "3\n"},
        {"name": "2", "input": "-4 4\n", "expected": "0\n"},
        {"name": "3", "input": "100 5\n", "expected": "105\n"},
    ],
}


@pytest.fixture
def world(tmp_path: Path):
    database = Database.connect(f"sqlite+pysqlite:///{tmp_path}/a.db", create=True)
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    console = Console(database, store, profiles_dir=PROFILES)
    course, _ = ensure_course(
        database,
        tenant_id=TENANT,
        code="prog2",
        title="プログラミング及び実習 2",
        term="2025-後期",
        subject_profile="cs_intro_c",
        profiles_dir=PROFILES,
    )
    with database.unit_of_work() as uow:
        auth = AuthService(uow.identity)
        teacher = auth.register(
            tenant_id=TENANT, login="sano", display_name="佐野", password=PASSWORD
        )
        auth.enroll(
            tenant_id=TENANT, course_id=course.id, user_id=teacher.user_id, role=Role.INSTRUCTOR
        )
        learner = auth.register(
            tenant_id=TENANT, login="y250001", display_name="学生", password=PASSWORD
        )
        auth.enroll(
            tenant_id=TENANT, course_id=course.id, user_id=learner.user_id, role=Role.LEARNER
        )
        _record, token = auth.issue_token(
            tenant_id=TENANT, user_id=teacher.user_id, note="移行の流し込み"
        )
        uow.commit()

    yield {
        "database": database,
        "store": store,
        "course": course,
        "client": TestClient(review_app(console)),
        "token": token,
        "learner": learner,
        "worker": GradingWorker(
            database, store, profiles_dir=PROFILES, registry=EvaluatorRegistry().load_installed()
        ),
    }
    database.dispose()


def _post(world, spec: dict):
    return world["client"].post(
        f"/api/courses/{world['course'].id}/tasks",
        json=spec,
        headers={"Authorization": f"Bearer {world['token']}"},
    )


def _submit_and_grade(world, source: str):
    service = SubmissionService(world["database"].unit_of_work, world["store"])
    with world["database"].unit_of_work() as uow:
        version_id = uow.tasks.latest_version(
            uow.tasks.list_for_course(world["course"].id)[0].id
        ).id
    result = service.accept(
        tenant_id=TENANT,
        task_version_id=version_id,
        learner_id=world["learner"].user_id,
        subject_profile="cs_intro_c",
        files=[IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=source.encode())],
    )
    world["worker"].run_until_empty()
    with world["database"].unit_of_work() as uow:
        return uow.runs.latest_for(result.submission.id)


@needs_c_compiler
def test_a_task_posted_over_the_api_grades_a_correct_solution_full_marks(world) -> None:
    """**これが要点。** 201 が返ることと採点できることは別である。"""
    assert _post(world, SPEC).status_code == 201

    run = _submit_and_grade(world, SOLUTION)

    assert run is not None
    assert run.score_ratio == 1.0, "参照解答が満点にならない（テストケースが効いていない）"


@needs_c_compiler
def test_the_same_task_fails_a_wrong_solution(world) -> None:
    """満点だけを見ると、テストが素通りしていても気づけない。"""
    _post(world, SPEC)

    run = _submit_and_grade(world, WRONG)

    assert run is not None
    assert run.score_ratio < 1.0, "誤答が満点になっている（期待出力と比べていない）"


def test_the_test_case_payload_uses_the_keys_the_evaluator_reads(world) -> None:
    """キー名が評価器と食い違うと、**例外を出さずに全ケースが不合格**になる。

    `code_test_runner` は `payload.get("expected", "")` と
    `payload["input"]` を読む。パッケージ境界（import-linter）の都合で
    定数を共有できないので、ここで固定する。
    """
    _post(world, SPEC)

    with world["database"].unit_of_work() as uow:
        task = uow.tasks.list_for_course(world["course"].id)[0]
        version = uow.tasks.latest_version(task.id)

    assert version.test_cases
    for case in version.test_cases:
        assert set(case.payload) == {"input", "expected"}, case.payload
        assert case.evaluator_id == "code_test_runner"
