"""提出どうしの類似の検査（#203・ADR 0029）。

固定したいこと:

対象     学習者 1 人につき最新の提出 1 件。教員・TA・デモの提出は入れない。
仮名     Dolos に渡す名前は仮のもの（S-001）。実名・学籍番号・提出 ID を渡さない。
記録     報告の CSV と run.json を残す。課題ごとに最新の 1 回だけ。
再実行   入力が同じなら回さない。環境の不調で測れなかったときだけ回し直す。
測れない 件数・言語・道具の失敗は NOT_MEASURED と理由で残す（「0 組」と区別する）。
後始末   入れた提出が消えたら、報告も消す（提出と一緒に消す）。

実物の Dolos は gVisor（CI の runsc の段）で 1 本走らせる（`test_live_dolos_under_runsc`）。
"""

from __future__ import annotations

import contextlib
import csv
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aijudge_core import Artifact, ArtifactKind, ArtifactRole, Role, Submission, SubmissionState
from aijudge_core.ids import ArtifactId, SubmissionId, TenantId, UserId
from aijudge_course_admin import code_similarity
from aijudge_course_admin.code_similarity import (
    DOLOS_IMAGE,
    REPORT_FILES,
    NotMeasuredReason,
    latest_run,
    list_runs,
    read_run,
    run_dir,
    run_for_task,
    sweep_orphans,
)
from aijudge_course_admin.course_definition import apply_course_definition
from aijudge_persistence import Database
from aijudge_sandbox import ExecRequest, ExecResult, Isolation, SandboxUnavailable
from aijudge_submission import FilesystemArtifactStore

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
AUTHOR = UserId("usr_" + "a" * 32)
AT = datetime(2026, 10, 2, 1, 0, tzinfo=UTC)

DEFINITION = """
course:
  code: sim-demo
  title: 類似の検査
  term: 2026-後期
  subject_profile: cs_lang_c_intro
  description: 類似の検査を確かめるためのコース。
units:
  ex01:
    opens_at: 2026-09-18T13:00:00+09:00
    due_at: 2026-10-01T23:59:00+09:00
tasks:
  - key: ex01/p1
    unit: ex01
    session: 1
    position: 1
    title: 合計を出す
    accepted_suffixes: [.c, .py]
    statement: |
      ## [必須] 合計を出す ##
      n までの合計を出してください。
    criteria:
      - code: works
        title: 動く
        description: 動くか。
        weight: 1.0
        evaluator: __human__
        levels:
          - {level: 0, label: 未達, descriptor: 動かない, score_ratio: 0.0}
          - {level: 1, label: 十分, descriptor: 動く, score_ratio: 1.0}
"""

SUMS = b"""#include <stdio.h>

int main(void) {
    int n;
    scanf("%d", &n);
    long VAR = 0;
    for (int IDX = 1; IDX <= n; IDX++) {
        if (IDX % 3 == 0 || IDX % 5 == 0) {
            VAR += IDX;
        }
    }
    printf("%ld\\n", VAR);
    return 0;
}
"""
CLOSED_FORM = b"""#include <stdio.h>

static long multiples(int n, int d) {
    long m = n / d;
    return d * m * (m + 1) / 2;
}

int main(void) {
    int n;
    scanf("%d", &n);
    printf("%ld\\n", multiples(n, 3) + multiples(n, 5) - multiples(n, 15));
    return 0;
}
"""
SOURCE_A = b'#include <stdio.h>\nint main(void){int n;scanf("%d",&n);printf("%d\\n",n);return 0;}\n'


@pytest.fixture
def database(tmp_path: Path):
    db = Database.connect(f"sqlite+pysqlite:///{tmp_path}/sim.db", create=True)
    yield db
    db.dispose()


@pytest.fixture
def store(tmp_path: Path) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def task(database: Database, tmp_path: Path):
    definition = tmp_path / "course.yaml"
    definition.write_text(DEFINITION, encoding="utf-8")
    course = apply_course_definition(
        database, path=definition, tenant_id=TENANT, profiles_dir=PROFILES, authored_by=AUTHOR
    ).course
    with database.unit_of_work() as uow:
        return uow.tasks.list_for_course(course.id)[0]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "similarity"


_counter = iter(range(1, 10_000))


def submit(
    database: Database,
    store: FilesystemArtifactStore,
    task,
    learner: str,
    source: bytes,
    *,
    filename: str = "main.c",
    at: datetime = AT - timedelta(days=2),
    role: Role = Role.LEARNER,
    attempt: int = 1,
) -> SubmissionId:
    n = next(_counter)
    submission_id = SubmissionId(f"sub_{n:032d}")
    key = f"code/{n}.c"
    store.put(key, source)
    with database.unit_of_work() as uow:
        version = uow.tasks.latest_version(task.id)
        uow.submissions.save(
            Submission(
                id=submission_id,
                task_version_id=version.id,
                learner_id=UserId(f"usr_{learner:0>32}"),
                submitted_as=role,
                attempt=attempt,
                state=SubmissionState.SUBMITTED,
                submitted_at=at,
                artifacts=(
                    Artifact(
                        id=ArtifactId(f"art_{n:032d}"),
                        submission_id=submission_id,
                        role=ArtifactRole.ORIGINAL,
                        kind=ArtifactKind.CODE,
                        filename=filename,
                        storage_key=key,
                        content_hash=f"{n:064d}",
                        byte_size=len(source),
                        created_at=at,
                    ),
                ),
                created_at=at,
            )
        )
        uow.commit()
    return submission_id


@dataclass
class FakeSandbox:
    """Dolos の代わりに報告の 4 ファイルを書く。渡された入力を覚えておく。"""

    exit_code: int = 0
    seen: list[dict[str, bytes]] = field(default_factory=list)
    argv: list[tuple[str, ...]] = field(default_factory=list)
    name: str = "fake"
    isolation: Isolation = Isolation.KERNEL_ISOLATED
    limitations: frozenset = frozenset()

    @contextlib.contextmanager
    def workspace(self) -> Iterator[FakeWorkspace]:
        yield FakeWorkspace(self)

    def __call__(self, image: str) -> FakeSandbox:
        assert image == DOLOS_IMAGE
        return self


class FakeWorkspace:
    def __init__(self, sandbox: FakeSandbox) -> None:
        self.sandbox = sandbox
        self._dir = Path(os.environ["SIM_TEST_WORK"]) / str(len(sandbox.argv))
        self.path = self._dir
        self.files: dict[str, bytes] = {}

    def write(self, name: str, content: bytes | str) -> Path:
        data = content.encode() if isinstance(content, str) else content
        self.files[name] = data
        target = self._dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target

    def run(self, request: ExecRequest) -> ExecResult:
        self.sandbox.seen.append(dict(self.files))
        self.sandbox.argv.append(request.argv)
        if self.sandbox.exit_code == 0:
            out = self._dir / "out"
            out.mkdir(parents=True, exist_ok=True)
            for name in REPORT_FILES:
                (out / name).write_text(f"{name}\n", encoding="utf-8")
        return ExecResult(
            exit_code=self.sandbox.exit_code, stderr="boom" if self.sandbox.exit_code else ""
        )


@pytest.fixture
def fake(tmp_path: Path, monkeypatch) -> FakeSandbox:
    monkeypatch.setenv("SIM_TEST_WORK", str(tmp_path / "work"))
    return FakeSandbox()


def test_one_submission_per_learner_under_pseudonyms(database, store, task, root, fake) -> None:
    submit(database, store, task, "1", b"old", attempt=1, at=AT - timedelta(days=3))
    latest = submit(database, store, task, "1", SOURCE_A, attempt=2)
    other = submit(database, store, task, "2", SOURCE_A + b"// b\n")
    submit(database, store, task, "3", b"teacher", role=Role.INSTRUCTOR)

    run = run_for_task(
        database, task.id, artifact_store=store, root=root, now=AT, sandbox_factory=fake
    )

    assert run is not None and run.measured and run.language == "c"
    assert {s.submission_id for s in run.submissions} == {latest, other}
    files = fake.seen[0]
    assert sorted(files) == ["ds/S-001.c", "ds/S-002.c", "ds/info.csv"]
    assert b"old" not in files.values() and b"teacher" not in files.values()
    # **画面に出る名前に実名も利用者 ID も提出 ID も入れない。**
    info = files["ds/info.csv"].decode()
    rows = list(csv.DictReader(info.splitlines()))
    assert [row["full_name"] for row in rows] == ["S-001", "S-002"]
    assert "usr_" not in info and "sub_" not in info
    assert fake.argv[0][:6] == ("dolos", "run", "-l", "c", "-f", "csv")

    directory = run_dir(root, run)
    assert sorted(p.name for p in (directory / "data").iterdir()) == sorted(REPORT_FILES)
    assert read_run(directory) == run


def test_the_same_input_is_not_run_again(database, store, task, root, fake) -> None:
    submit(database, store, task, "1", SOURCE_A)
    submit(database, store, task, "2", SOURCE_A)
    kwargs = {"artifact_store": store, "root": root, "sandbox_factory": fake}

    first = run_for_task(database, task.id, now=AT, **kwargs)
    assert run_for_task(database, task.id, now=AT + timedelta(hours=1), **kwargs) is None

    # 遅れて出た提出で入力が変わると回し直し、**古い回は消える**（写しを増やさない）。
    submit(database, store, task, "3", SOURCE_A)
    second = run_for_task(database, task.id, now=AT + timedelta(hours=2), **kwargs)
    assert second is not None and len(second.submissions) == 3
    assert not run_dir(root, first).exists()
    assert latest_run(root, task.course_id, task.id) == second
    assert list_runs(root, task.course_id) == [second]


def test_a_tool_failure_is_recorded_and_retried(database, store, task, root, fake) -> None:
    submit(database, store, task, "1", SOURCE_A)
    submit(database, store, task, "2", SOURCE_A)
    fake.exit_code = 2
    kwargs = {"artifact_store": store, "root": root, "sandbox_factory": fake}

    failed = run_for_task(database, task.id, now=AT, **kwargs)
    assert failed is not None and failed.not_measured is NotMeasuredReason.TOOL_FAILED
    assert "boom" in failed.detail
    assert not (run_dir(root, failed) / "data").exists()

    # **入力が同じでも、環境の不調なら回し直す。**
    fake.exit_code = 0
    again = run_for_task(database, task.id, now=AT + timedelta(minutes=5), **kwargs)
    assert again is not None and again.measured


def test_an_unavailable_sandbox_is_not_measured(database, store, task, root) -> None:
    submit(database, store, task, "1", SOURCE_A)
    submit(database, store, task, "2", SOURCE_A)

    def unavailable(image: str):
        raise SandboxUnavailable("no container backend")

    run = run_for_task(
        database, task.id, artifact_store=store, root=root, now=AT, sandbox_factory=unavailable
    )
    assert run is not None and run.not_measured is NotMeasuredReason.SANDBOX_UNAVAILABLE
    assert run.not_measured.retryable


@pytest.mark.parametrize(
    ("sources", "reason"),
    [
        ([("1", "main.c")], NotMeasuredReason.TOO_FEW),
        ([("1", "main.c"), ("2", "notes.txt")], NotMeasuredReason.UNSUPPORTED_LANGUAGE),
        ([("1", "main.c"), ("2", "main.py")], NotMeasuredReason.MIXED_LANGUAGES),
    ],
)
def test_what_cannot_be_measured_says_why(
    database, store, task, root, fake, sources, reason
) -> None:
    for learner, filename in sources:
        submit(database, store, task, learner, SOURCE_A, filename=filename)

    run = run_for_task(
        database, task.id, artifact_store=store, root=root, now=AT, sandbox_factory=fake
    )

    assert run is not None and run.not_measured is reason and run.detail
    assert fake.argv == []  # 道具は呼ばない
    assert not reason.retryable
    # 入力が変わらない限り、同じ理由で何度も回さない。
    later = AT + timedelta(hours=1)
    assert (
        run_for_task(
            database, task.id, artifact_store=store, root=root, now=later, sandbox_factory=fake
        )
        is None
    )


def test_too_many_submissions_are_not_measured(
    database, store, task, root, fake, monkeypatch
) -> None:
    monkeypatch.setattr(code_similarity, "MAX_SUBMISSIONS", 2)
    for learner in "123":
        submit(database, store, task, learner, SOURCE_A)

    run = run_for_task(
        database, task.id, artifact_store=store, root=root, now=AT, sandbox_factory=fake
    )
    assert run is not None and run.not_measured is NotMeasuredReason.TOO_MANY


def test_a_report_goes_with_its_submissions(database, store, task, root, fake) -> None:
    """**提出と一緒に消す**（#203 の決定 4）。1 件でも元が消えたら報告ごと消す。"""
    gone = submit(database, store, task, "1", SOURCE_A)
    submit(database, store, task, "2", SOURCE_A)
    run = run_for_task(
        database, task.id, artifact_store=store, root=root, now=AT, sandbox_factory=fake
    )
    assert run is not None

    assert sweep_orphans(database, root) == []
    with database.unit_of_work() as uow:
        uow.submissions.delete([gone])
        uow.commit()

    assert sweep_orphans(database, root) == [run_dir(root, run)]
    assert not run_dir(root, run).exists()


def test_a_deleted_course_takes_its_reports(database, store, task, root, fake) -> None:
    stray = root / ("crs_" + "9" * 32) / "tsk_x" / "20260101T000000000000Z"
    stray.mkdir(parents=True)
    (stray / "run.json").write_text("{}", encoding="utf-8")

    assert sweep_orphans(database, root) == [root / ("crs_" + "9" * 32)]


# --------------------------------------------------------------------------
# 実物の Dolos（CI の runsc の段で走る）
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    (
        os.environ.get("AIJUDGE_SANDBOX") != "gvisor"
        and os.environ.get("AIJUDGE_SIMILARITY_LIVE") != "1"
    )
    or shutil.which("docker") is None,
    reason=(
        "実物の Dolos を動かす（CI の runsc の段: AIJUDGE_SANDBOX=gvisor。"
        "手元の runc なら AIJUDGE_SIMILARITY_LIVE=1 AIJUDGE_SANDBOX=docker）"
    ),
)
def test_live_dolos_under_runsc(database, store, task, root, monkeypatch) -> None:
    """運用機と同じ制限（gVisor・pids 64・512 MiB・ネットワークなし）で実物が動くこと。"""
    monkeypatch.setenv("AIJUDGE_SANDBOX_IMAGES", DOLOS_IMAGE)
    # 変数名だけを変えた写し（Dolos は識別子を抽象化するので 1.0 になる）と、別の解き方。
    original = SUMS.replace(b"VAR", b"total").replace(b"IDX", b"i")
    renamed = SUMS.replace(b"VAR", b"sum").replace(b"IDX", b"k")
    for learner, source in (("1", original), ("2", renamed), ("3", CLOSED_FORM)):
        submit(database, store, task, learner, source)

    run = run_for_task(database, task.id, artifact_store=store, root=root, now=AT)

    assert run is not None and run.measured, run
    if os.environ.get("AIJUDGE_SANDBOX") == "gvisor":
        assert run.isolation == Isolation.KERNEL_ISOLATED.value
    pairs = list(csv.DictReader((run_dir(root, run) / "data" / "pairs.csv").open()))
    assert len(pairs) == 3
    assert max(float(pair["similarity"]) for pair in pairs) > 0.9


# --------------------------------------------------------------------------
# CLI（aijudge-admin similarity run）
# --------------------------------------------------------------------------


def test_the_cli_needs_a_place_for_reports(tmp_path: Path, monkeypatch, capsys) -> None:
    from aijudge_admin.cli import main

    monkeypatch.delenv(code_similarity.ENV_SIMILARITY_DIR, raising=False)
    database_url = f"sqlite+pysqlite:///{tmp_path}/cli.db"
    code = main(["--database-url", database_url, "similarity", "run", "--task", "tsk_x"])
    assert code == 2
    assert code_similarity.ENV_SIMILARITY_DIR in capsys.readouterr().err


def test_the_cli_runs_every_task_of_a_unit(
    database, store, task, root, tmp_path: Path, monkeypatch, capsys
) -> None:
    from aijudge_admin.cli import main

    called: list[tuple[str, bool]] = []

    def record(database, task_id, *, artifact_store, root, force):
        called.append((str(task_id), force))
        return None

    monkeypatch.setattr(code_similarity, "run_for_task", record)
    code = main(
        [
            "--database-url",
            f"sqlite+pysqlite:///{tmp_path}/sim.db",
            "similarity",
            "run",
            "--course",
            str(task.course_id),
            "--unit",
            "ex01",
            "--force",
            "--similarity-dir",
            str(root),
        ]
    )
    assert code == 0
    assert called == [(str(task.id), True)]
    assert "--force で回し直す" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 自動実行（aijudge-similarity・受付が閉じた課題だけ）
# --------------------------------------------------------------------------

# 課題の締切は 2026-10-01T23:59+09:00（UTC で 14:59）。受付終了は無いので締切が起点。
CLOSE = datetime(2026, 10, 1, 14, 59, tzinfo=UTC)


def test_only_tasks_past_the_close_are_run(database, store, task, root, fake) -> None:
    submit(database, store, task, "1", SOURCE_A, at=CLOSE - timedelta(hours=1))
    submit(database, store, task, "2", SOURCE_A, at=CLOSE - timedelta(hours=1))
    kwargs = {"root": root, "artifact_store": store, "sandbox_factory": fake}

    # 締切から 1 時間はまだ回さない（試験では締切の後にまず採点が回る）。
    early = code_similarity.sweep(database, now=CLOSE + timedelta(minutes=59), **kwargs)
    assert early.due == [] and early.ran == []

    later = code_similarity.sweep(database, now=CLOSE + timedelta(hours=1), **kwargs)
    assert [t.id for t in later.due] == [task.id]
    assert len(later.ran) == 1

    # **何度走らせても同じ結果になる。** 入力が同じなら回さない。
    again = code_similarity.sweep(database, now=CLOSE + timedelta(hours=2), **kwargs)
    assert again.ran == [] and len(fake.argv) == 1


def test_the_dry_run_touches_nothing(database, store, task, root, fake) -> None:
    submit(database, store, task, "1", SOURCE_A)
    submit(database, store, task, "2", SOURCE_A)
    stray = root / ("crs_" + "9" * 32)
    stray.mkdir(parents=True)

    report = code_similarity.sweep(
        database,
        root=root,
        artifact_store=store,
        now=CLOSE + timedelta(hours=1),
        dry_run=True,
        sandbox_factory=fake,
    )

    assert [t.id for t in report.due] == [task.id]
    assert fake.argv == [] and stray.exists() and report.removed == []


def test_a_task_without_code_leaves_no_record(database, store, task, root, fake) -> None:
    """レポートや動画の課題は記録も残さない（入口のページが「測れない」で埋まる）。"""
    run = run_for_task(
        database, task.id, artifact_store=store, root=root, now=AT, sandbox_factory=fake
    )
    assert run is None and not root.exists()


def test_a_task_without_a_close_is_never_run_automatically(database, store, task, root, fake):
    with database.unit_of_work() as uow:
        uow.tasks.save_task(task.model_copy(update={"due_at": None, "accepts_until": None}))
        uow.commit()
    report = code_similarity.sweep(
        database,
        root=root,
        artifact_store=store,
        now=AT + timedelta(days=365),
        sandbox_factory=fake,
    )
    assert report.due == []


def test_the_timer_does_nothing_until_the_place_is_set(monkeypatch, tmp_path: Path) -> None:
    """設定の前に timer が動いても失敗にしない（毎時の知らせを送らない）。"""
    from aijudge_admin.similarity_cli import main

    monkeypatch.delenv(code_similarity.ENV_SIMILARITY_DIR, raising=False)
    assert main(["--once", "--database-url", f"sqlite+pysqlite:///{tmp_path}/x.db"]) == 0
    assert main(["--database-url", f"sqlite+pysqlite:///{tmp_path}/x.db"]) == 2
