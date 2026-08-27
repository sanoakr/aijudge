"""2 科目の同時運用を検証する（Phase 2 の合格基準）。

設計方針 §9.2 Phase 2 が課している条件のうち、自動テストで確かめられるもの:

- 2 科目以上を同一インスタンスで同時運用する（言語が違う）
- 科目の追加がコード変更なしで完了する
- **コース間でデータが混ざらない**
- 複数教員で採点を分担できる

「1 学期を通し運用する」は実運用でしか確かめられないので、ここには無い。

このファイルがリポジトリ直下の `tests/` にあるのは、どのパッケージにも
属さない検証だから。複数のサブシステムとアプリを束ねて初めて意味がある。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aijudge_admin import enrol_roster, ensure_course, import_tasks, parse_roster
from aijudge_core import ArtifactKind
from aijudge_core.ids import TenantId
from aijudge_grader import GradingWorker
from aijudge_grading import EvaluatorRegistry
from aijudge_persistence import Database
from aijudge_reviewconsole import SESSION_COOKIE as REVIEW_COOKIE
from aijudge_reviewconsole import Console
from aijudge_reviewconsole import create_app as review_app
from aijudge_studentweb import SESSION_COOKIE as STUDENT_COOKIE
from aijudge_studentweb import StudentApp
from aijudge_studentweb import create_app as student_app
from aijudge_submission import FilesystemArtifactStore, IncomingFile, SubmissionService

REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILES = REPO_ROOT / "subjects"
C_TASK = REPO_ROOT / "evals" / "golden" / "cs_intro_c" / "example-task" / "task"
TENANT = TenantId("ten_" + "0" * 32)
PASSWORD = "correct horse battery"

needs_c_compiler = pytest.mark.skipif(
    shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="no C compiler available",
)

# Python 課題を C の例題から作らず、その場で組み立てる。移行元の教材は
# リポジトリ外（個人情報を含むディレクトリ）にあるので依存させない。
PY_STATEMENT = "## sumtwo.py\n\n2 つの整数を読み、和を出力しなさい。\n"
PY_REFERENCE = "a = int(input())\nb = int(input())\nprint(a + b)\n"


def _python_task(root: Path) -> Path:
    problem = root / "ex1" / "p1"
    (problem / "in").mkdir(parents=True)
    (problem / "out").mkdir(parents=True)
    (problem / "desc.md").write_text(PY_STATEMENT, encoding="utf-8")
    (problem / "sumtwo.py").write_text(PY_REFERENCE, encoding="utf-8")
    for index, (a, b) in enumerate([(1, 2), (10, -3), (0, 0)], 1):
        (problem / "in" / f"input{index}.txt").write_text(f"{a}\n{b}\n", encoding="utf-8")
        (problem / "out" / f"output{index}.txt").write_text(f"{a + b}\n", encoding="utf-8")
    return problem


class Campus:
    """2 科目・2 教員・2 学生が同居する環境。"""

    def __init__(self, tmp_path: Path) -> None:
        self.database = Database.connect(f"sqlite+pysqlite:///{tmp_path}/a.db", create=True)
        self.store = FilesystemArtifactStore(tmp_path / "artifacts")
        self.registry = EvaluatorRegistry().load_installed()
        self.submissions = SubmissionService(self.database.unit_of_work, self.store)
        self.worker = GradingWorker(
            self.database, self.store, profiles_dir=PROFILES, registry=self.registry
        )
        self.console = Console(self.database, self.store, profiles_dir=PROFILES)
        self.review = TestClient(review_app(self.console))
        self.web = TestClient(
            student_app(StudentApp(self.database, self.store, profiles_dir=PROFILES))
        )

        # --- 科目 1: C ---
        self.c_course, _ = ensure_course(
            self.database,
            tenant_id=TENANT,
            code="prog2",
            title="プログラミング及び実習 2",
            term="2025-後期",
            subject_profile="cs_intro_c",
            profiles_dir=PROFILES,
        )
        # --- 科目 2: Python ---
        self.py_course, _ = ensure_course(
            self.database,
            tenant_id=TENANT,
            code="network",
            title="ネットワーク及び演習",
            term="2025-後期",
            subject_profile="net_python",
            profiles_dir=PROFILES,
        )

        # 課題（各科目 1 件）
        import_tasks(
            self.database,
            course_id=self.c_course.id,
            directory=C_TASK,
            profiles_dir=PROFILES,
        )
        import_tasks(
            self.database,
            course_id=self.py_course.id,
            directory=_python_task(tmp_path / "pytask"),
            profiles_dir=PROFILES,
        )

        # 受講者と教員。**学生は片方の科目にしか居ない。**
        enrol_roster(
            self.database,
            tenant_id=TENANT,
            course_id=self.c_course.id,
            entries=parse_roster("c_student\nc_teacher x@y.z RANDOM[12] instructor\n"),
        )
        enrol_roster(
            self.database,
            tenant_id=TENANT,
            course_id=self.py_course.id,
            entries=parse_roster("py_student\npy_teacher x@y.z RANDOM[12] instructor\n"),
        )
        for login in ("c_student", "c_teacher", "py_student", "py_teacher"):
            self._set_password(login)

    def _set_password(self, login: str) -> None:
        from aijudge_admin import set_password

        set_password(self.database, tenant_id=TENANT, login=login, password=PASSWORD)

    def tasks_of(self, course_id):
        from aijudge_admin import list_tasks

        return list_tasks(self.database, course_id)

    def login_student(self, login: str) -> TestClient:
        client = TestClient(
            student_app(StudentApp(self.database, self.store, profiles_dir=PROFILES))
        )
        response = client.post(
            "/login", data={"login": login, "password": PASSWORD}, follow_redirects=False
        )
        assert response.status_code == 303, response.text
        client.cookies.set(STUDENT_COOKIE, response.cookies[STUDENT_COOKIE])
        return client

    def login_teacher(self, login: str) -> TestClient:
        client = TestClient(review_app(self.console))
        response = client.post(
            "/login", data={"login": login, "password": PASSWORD}, follow_redirects=False
        )
        assert response.status_code == 303, response.text
        client.cookies.set(REVIEW_COOKIE, response.cookies[REVIEW_COOKIE])
        return client

    def user_id(self, login: str):
        with self.database.unit_of_work() as uow:
            user = uow.identity.find_user_by_login(TENANT, login)
        assert user is not None
        return user.id

    def submit(self, login: str, course, payload: bytes, filename: str):
        (_task, version) = self.tasks_of(course.id)[0]
        return self.submissions.accept(
            tenant_id=TENANT,
            task_version_id=version.id,
            learner_id=self.user_id(login),
            subject_profile=course.subject_profile,
            files=[IncomingFile(filename=filename, kind=ArtifactKind.CODE, payload=payload)],
        )

    def close(self) -> None:
        self.database.dispose()


@pytest.fixture
def campus(tmp_path: Path):
    instance = Campus(tmp_path)
    yield instance
    instance.close()


# --------------------------------------------------------------------------
# 2 科目が同時に成立する
# --------------------------------------------------------------------------


def test_two_subjects_coexist_with_different_languages(campus: Campus) -> None:
    assert campus.c_course.subject_profile == "cs_intro_c"
    assert campus.py_course.subject_profile == "net_python"
    assert len(campus.tasks_of(campus.c_course.id)) == 1
    assert len(campus.tasks_of(campus.py_course.id)) == 1


@needs_c_compiler
def test_both_languages_are_graded_in_one_queue(campus: Campus) -> None:
    """同じキューに C と Python の提出が並び、それぞれ正しく採点される。

    ワーカーは言語を知らない。科目プロファイルが評価器に渡す。
    """
    c_source = (C_TASK / "maxmin.c").read_bytes()
    campus.submit("c_student", campus.c_course, c_source, "main.c")
    campus.submit("py_student", campus.py_course, PY_REFERENCE.encode(), "main.py")

    graded, errors = campus.worker.run_until_empty()
    assert (graded, errors) == (2, ())

    with campus.database.unit_of_work() as uow:
        for login, course in (
            ("c_student", campus.c_course),
            ("py_student", campus.py_course),
        ):
            submissions = uow.submissions.list_for_learner(TENANT, campus.user_id(login))
            run = uow.runs.latest_for(submissions[0].id)
            assert run is not None, login
            assert run.score_ratio == pytest.approx(1.0), (
                f"{login} の参照解答が満点にならない: {run.score_ratio}"
            )
            assert run.context.subject_profile == course.subject_profile


@needs_c_compiler
def test_a_worker_can_be_pinned_to_one_subject(campus: Campus) -> None:
    """GPU を食う科目と食わない科目でキューを分けられること。"""
    campus.submit("c_student", campus.c_course, (C_TASK / "maxmin.c").read_bytes(), "main.c")
    campus.submit("py_student", campus.py_course, PY_REFERENCE.encode(), "main.py")

    graded, _ = campus.worker.run_until_empty(subject_profile="net_python")
    assert graded == 1
    with campus.database.unit_of_work() as uow:
        assert uow.jobs.pending_count(subject_profile="cs_intro_c") == 1
        assert uow.jobs.pending_count(subject_profile="net_python") == 0


# --------------------------------------------------------------------------
# コース間でデータが混ざらない
# --------------------------------------------------------------------------


def test_a_learner_sees_only_their_own_course(campus: Campus) -> None:
    body = campus.login_student("c_student").get("/").text
    assert "プログラミング及び実習 2" in body
    assert "ネットワーク及び演習" not in body


def test_a_learner_cannot_open_the_other_courses_task(campus: Campus) -> None:
    """URL を推測されても他科目の課題は見えない。"""
    (_task, other_version) = campus.tasks_of(campus.py_course.id)[0]
    client = campus.login_student("c_student")
    assert client.get(f"/tasks/{other_version.id}").status_code == 404
    assert client.get(f"/courses/{campus.py_course.id}").status_code == 404


@needs_c_compiler
def test_a_learner_cannot_open_the_other_courses_submission(campus: Campus) -> None:
    accepted = campus.submit("py_student", campus.py_course, PY_REFERENCE.encode(), "main.py")
    client = campus.login_student("c_student")
    assert client.get(f"/submissions/{accepted.submission.id}").status_code == 404


def test_a_teacher_sees_only_the_course_they_grade(campus: Campus) -> None:
    body = campus.login_teacher("c_teacher").get("/").text
    assert "プログラミング及び実習 2" in body
    assert "ネットワーク及び演習" not in body


def test_a_teacher_cannot_open_the_other_courses_queue(campus: Campus) -> None:
    client = campus.login_teacher("c_teacher")
    assert client.get(f"/courses/{campus.py_course.id}").status_code == 404


@needs_c_compiler
def test_a_teacher_cannot_review_the_other_courses_submission(campus: Campus) -> None:
    """採点権限はコース単位。他科目の提出は「無い」と答える。"""
    accepted = campus.submit("py_student", campus.py_course, PY_REFERENCE.encode(), "main.py")
    campus.worker.run_until_empty()
    client = campus.login_teacher("c_teacher")
    assert client.get(f"/review/{accepted.submission.id}/reveal").status_code == 404


@needs_c_compiler
def test_each_teachers_queue_holds_only_their_own_submissions(campus: Campus) -> None:
    c_accepted = campus.submit(
        "c_student", campus.c_course, (C_TASK / "maxmin.c").read_bytes(), "main.c"
    )
    py_accepted = campus.submit("py_student", campus.py_course, PY_REFERENCE.encode(), "main.py")
    campus.worker.run_until_empty()

    c_queue = campus.login_teacher("c_teacher").get(f"/courses/{campus.c_course.id}").text
    py_queue = campus.login_teacher("py_teacher").get(f"/courses/{campus.py_course.id}").text

    assert str(c_accepted.submission.id)[:12] in c_queue
    assert str(py_accepted.submission.id)[:12] not in c_queue
    assert str(py_accepted.submission.id)[:12] in py_queue
    assert str(c_accepted.submission.id)[:12] not in py_queue


# --------------------------------------------------------------------------
# 複数教員での分担
# --------------------------------------------------------------------------


@needs_c_compiler
def test_an_assistant_shares_the_grading(campus: Campus) -> None:
    """TA が同じコースを採点できること（Phase 2 の分担の土台）。"""
    enrol_roster(
        campus.database,
        tenant_id=TENANT,
        course_id=campus.c_course.id,
        entries=parse_roster("c_ta x@y.z RANDOM[12] ta\n"),
    )
    campus._set_password("c_ta")
    accepted = campus.submit(
        "c_student", campus.c_course, (C_TASK / "maxmin.c").read_bytes(), "main.c"
    )
    campus.worker.run_until_empty()

    client = campus.login_teacher("c_ta")
    assert client.get(f"/review/{accepted.submission.id}/reveal").status_code == 200


@needs_c_compiler
def test_only_one_teacher_can_finalise_a_submission(campus: Campus) -> None:
    """2 人目の確定を拒否する。二度確定できると成績が二つ存在する。"""
    enrol_roster(
        campus.database,
        tenant_id=TENANT,
        course_id=campus.c_course.id,
        entries=parse_roster("c_ta x@y.z RANDOM[12] ta\n"),
    )
    campus._set_password("c_ta")
    accepted = campus.submit(
        "c_student", campus.c_course, (C_TASK / "maxmin.c").read_bytes(), "main.c"
    )
    campus.worker.run_until_empty()

    with campus.database.unit_of_work() as uow:
        run = uow.runs.latest_for(accepted.submission.id)
    assert run is not None
    (_task, version) = campus.tasks_of(campus.c_course.id)[0]
    machine = {score.criterion_id: score.level for score in run.criterion_scores}
    data = {f"level_{c.code}": str(machine[c.id]) for c in version.criteria if c.id in machine}

    first = campus.login_teacher("c_teacher").post(
        f"/review/{accepted.submission.id}/finalize", data=data, follow_redirects=False
    )
    assert first.status_code == 303
    second = campus.login_teacher("c_ta").post(
        f"/review/{accepted.submission.id}/finalize", data=data
    )
    assert second.status_code == 409


# --------------------------------------------------------------------------
# 締切集中
# --------------------------------------------------------------------------


@needs_c_compiler
def test_a_burst_of_submissions_drains(campus: Campus) -> None:
    """締切直前の同時提出でキューが捌けること。

    件数は控えめ（実測用の本番規模ではない）。ここで見たいのは
    「詰まらない」ことと、1 件あたりの所要が件数で悪化しないこと。
    """
    source = (C_TASK / "maxmin.c").read_text(encoding="utf-8")
    count = 8
    for index in range(count):
        # 中身を変えて別提出にする（同一内容は冪等に畳まれる）。
        payload = f"{source}\n// submission {index}\n".encode()
        campus.submit("c_student", campus.c_course, payload, "main.c")

    with campus.database.unit_of_work() as uow:
        assert uow.jobs.pending_count() == count

    started = time.monotonic()
    graded, errors = campus.worker.run_until_empty()
    elapsed = time.monotonic() - started

    assert (graded, errors) == (count, ())
    with campus.database.unit_of_work() as uow:
        assert uow.jobs.pending_count() == 0
    # 1 件あたり 30 秒（§9.1 の p95）を大きく下回っていること。
    assert elapsed / count < 10.0, f"1 件あたり {elapsed / count:.1f} 秒"
