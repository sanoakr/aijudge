"""デモコースを作り直す CLI の入口（#707）。

作り直しの規則そのもの（何を消して何を戻すか）は `reset_demo_course` にあり、
`test_demo_reset.py` が関数を直接呼んで確かめている。ここで確かめるのは
**入口の側**である ── `main()` から入って、確認を挟むか、`--yes` で省けるか、
対象を指す環境変数が無いときに断るか。

**提出と受講登録を消す操作の、人の関門はここにしかない。** 関数側のテストは
確認を経由しないので、確認の枝を丸ごと消しても、`--yes` の判定を裏返しても
そちらは落ちない。だからこのファイルは「消えていないこと」を DB まで見る
（出力だけを見ると、先に消してから「中止しました」と出す形でも通ってしまう）。
"""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_admin.cli import main
from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    Enrollment,
    Role,
    Submission,
    SubmissionState,
)
from aijudge_core.ids import ArtifactId, SubmissionId, TenantId, UserId
from aijudge_course_admin.demo_seed import seed_demo_course
from aijudge_identity.demo import ENV_DEMO_COURSE
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
TEACHER = UserId("usr_" + "a" * 32)
LEARNER = UserId("usr_" + "b" * 32)
SUBMISSION = SubmissionId("sub_" + "1" * 32)
NOW = datetime(2026, 9, 11, tzinfo=UTC)


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite+pysqlite:///{tmp_path}/demo_cli.db"


@pytest.fixture
def seeded(db_url: str):
    """定義から作ったデモコースと、その直後の課題数（作り直しが「戻す」先）。"""
    database = Database.connect(db_url, create=True)
    try:
        result = seed_demo_course(
            database, tenant_id=TENANT, profiles_dir=PROFILES, authored_by=TEACHER
        )
        with database.unit_of_work() as uow:
            return result.course, len(uow.tasks.list_for_course(result.course.id))
    finally:
        database.dispose()


@pytest.fixture
def seeded_task_count(seeded) -> int:
    return seeded[1]


@pytest.fixture
def demo_course(db_url: str, seeded, monkeypatch: pytest.MonkeyPatch):
    """使われた状態のデモコース ── 受講登録 1 件・提出 1 件を足し、環境変数で指す。"""
    course = seeded[0]
    database = Database.connect(db_url)
    try:
        with database.unit_of_work() as uow:
            tasks = uow.tasks.list_for_course(course.id)
            version = uow.tasks.latest_version(tasks[0].id)
            uow.identity.save_enrollment(
                Enrollment(
                    tenant_id=TENANT, course_id=course.id, user_id=LEARNER, role=Role.LEARNER
                )
            )
            uow.submissions.save(
                Submission(
                    id=SUBMISSION,
                    task_version_id=version.id,
                    learner_id=LEARNER,
                    # デモの提出は trial（#194 の B）。偽だと削除の規則が拒む。
                    is_demo=True,
                    state=SubmissionState.SUBMITTED,
                    submitted_at=NOW,
                    artifacts=(
                        Artifact(
                            id=ArtifactId("art_" + "1" * 32),
                            submission_id=SUBMISSION,
                            role=ArtifactRole.ORIGINAL,
                            kind=ArtifactKind.CODE,
                            filename="main.c",
                            storage_key="demo/main.c",
                            content_hash="0" * 64,
                            byte_size=10,
                            created_at=NOW,
                        ),
                    ),
                    created_at=NOW,
                )
            )
            uow.commit()
    finally:
        database.dispose()
    monkeypatch.setenv(ENV_DEMO_COURSE, str(course.id))
    return course


def _cli(db_url: str, tmp_path: Path, *args: str) -> int:
    return main(
        [
            "--database-url",
            db_url,
            "--tenant",
            str(TENANT),
            "--profiles",
            str(PROFILES),
            "--artifacts",
            str(tmp_path / "artifacts"),
            "demo",
            "reset",
            *args,
        ]
    )


def _state(db_url: str, course) -> tuple[int, int, int]:
    """(提出, 受講登録, 課題) の件数を DB から読む。"""
    database = Database.connect(db_url)
    try:
        with database.unit_of_work() as uow:
            return (
                len(uow.submissions.list_for_course(course.id)),
                len(uow.identity.list_enrollments(course.id)),
                len(uow.tasks.list_for_course(course.id)),
            )
    finally:
        database.dispose()


def test_answering_no_keeps_everything(
    db_url: str, tmp_path: Path, demo_course, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """**既定では確認を挟む。** 「n」と答えれば何も消えない。

    確認の枝を消す変異、`not getattr(args, "yes", False)` の `not` を落とす
    変異のどちらでも、ここで提出が消えて落ちる。
    """
    before = _state(db_url, demo_course)
    asked: list[str] = []

    def answer_no(prompt: str) -> str:
        asked.append(prompt)
        return "n"

    monkeypatch.setattr("builtins.input", answer_no)
    assert _cli(db_url, tmp_path) == 1

    out = capsys.readouterr().out
    assert asked, "確認を訊いていない"
    assert "中止しました" in out
    # **規模を先に出してから訊く。** 件数を見ずに「はい」を押させない。
    assert "提出          1 件" in out
    assert _state(db_url, demo_course) == before


def test_no_terminal_means_no_reset(db_url: str, tmp_path: Path, demo_course, capsys) -> None:
    """**答えが得られなければ消さない。**

    `< /dev/null` や systemd から呼ぶと `input()` は `EOFError` を投げる。
    それを「はい」と読む実装にしてはいけない。
    """
    before = _state(db_url, demo_course)

    def no_terminal(_prompt: str) -> str:
        raise EOFError

    original, builtins.input = builtins.input, no_terminal
    try:
        assert _cli(db_url, tmp_path) == 1
    finally:
        builtins.input = original

    assert "中止しました" in capsys.readouterr().out
    assert _state(db_url, demo_course) == before


def test_yes_resets_without_asking_and_brings_the_tasks_back(
    db_url: str,
    tmp_path: Path,
    demo_course,
    seeded_task_count: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    """`--yes` は確認を省き、提出と受講登録を消して課題を定義どおりに戻す。"""

    def must_not_ask(_prompt: str) -> str:
        raise AssertionError("--yes なのに確認を訊いた")

    monkeypatch.setattr("builtins.input", must_not_ask)
    assert _cli(db_url, tmp_path, "--yes") == 0

    assert "作り直しました" in capsys.readouterr().out
    assert _state(db_url, demo_course) == (0, 0, seeded_task_count)


def test_answering_yes_resets(
    db_url: str,
    tmp_path: Path,
    demo_course,
    seeded_task_count: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """確認に「y」と答えたときは消す ── 関門が「常に中止」に壊れていないこと。"""
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")
    assert _cli(db_url, tmp_path) == 0
    assert _state(db_url, demo_course) == (0, 0, seeded_task_count)


def test_without_the_environment_variable_it_refuses(
    db_url: str, tmp_path: Path, demo_course, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """**対象は環境変数が指す 1 つに固定。** 無ければ何にも触らず exit 2。

    `demo is None` の拒否を消すと、ここで未処理の例外になって落ちる。
    """
    before = _state(db_url, demo_course)
    monkeypatch.delenv(ENV_DEMO_COURSE, raising=False)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    assert _cli(db_url, tmp_path, "--yes") == 2
    assert ENV_DEMO_COURSE in capsys.readouterr().err
    assert _state(db_url, demo_course) == before
