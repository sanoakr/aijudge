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
import json
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
    DOLOS_MODES,
    REPORT_FILES,
    TEXT_PARAMS,
    NotMeasuredReason,
    SimilarityMode,
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
    kind: ArtifactKind = ArtifactKind.CODE,
    is_demo: bool = False,
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
                is_demo=is_demo,
                attempt=attempt,
                state=SubmissionState.SUBMITTED,
                submitted_at=at,
                artifacts=(
                    Artifact(
                        id=ArtifactId(f"art_{n:032d}"),
                        submission_id=submission_id,
                        role=ArtifactRole.ORIGINAL,
                        kind=kind,
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
    #: `pairs.csv` に書く中身（無ければファイル名だけ）。
    pairs: str | None = None
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
                body = self.sandbox.pairs if name == "pairs.csv" and self.sandbox.pairs else name
                (out / name).write_text(f"{body}\n", encoding="utf-8")
        return ExecResult(
            exit_code=self.sandbox.exit_code, stderr="boom" if self.sandbox.exit_code else ""
        )


@pytest.fixture
def fake(tmp_path: Path, monkeypatch) -> FakeSandbox:
    monkeypatch.setenv("SIM_TEST_WORK", str(tmp_path / "work"))
    return FakeSandbox()


def test_one_submission_per_person_under_pseudonyms(database, store, task, root, fake) -> None:
    submit(database, store, task, "1", b"old", attempt=1, at=AT - timedelta(days=3))
    latest = submit(database, store, task, "1", SOURCE_A, attempt=2)
    other = submit(database, store, task, "2", SOURCE_A + b"// b\n")
    # 教員・TA の提出も比べる（2026-10-30）── 成績には数えないが、模範解答に近い答えである。
    submit(database, store, task, "3", b"check", role=Role.INSTRUCTOR, at=AT - timedelta(days=4))
    teacher = submit(database, store, task, "3", b"teacher", role=Role.INSTRUCTOR, attempt=2)
    assistant = submit(database, store, task, "4", b"assistant", role=Role.ASSISTANT)

    run = run_for_task(
        database, task.id, artifact_store=store, root=root, now=AT, sandbox_factory=fake
    )

    assert run is not None and run.measured and run.language == "c"
    assert {s.submission_id for s in run.submissions} == {latest, other, teacher, assistant}
    assert {s.submission_id for s in run.submissions if s.staff} == {teacher, assistant}
    files = fake.seen[0]
    assert sorted(files) == [
        "ds/S-001.c",
        "ds/S-002.c",
        "ds/T-001.c",
        "ds/T-002.c",
        "ds/info.csv",
    ]
    assert b"old" not in files.values() and b"check" not in files.values()
    # **画面に出る名前に実名も利用者 ID も提出 ID も入れない。**
    info = files["ds/info.csv"].decode()
    rows = list(csv.DictReader(info.splitlines()))
    assert [row["full_name"] for row in rows] == ["S-001", "S-002", "T-001", "T-002"]
    assert [row["labels"] for row in rows] == ["", "", "教員・TA", "教員・TA"]
    assert "usr_" not in info and "sub_" not in info
    assert fake.argv[0][:6] == ("dolos", "run", "-l", "c", "-f", "csv")

    directory = run_dir(root, run)
    assert sorted(p.name for p in (directory / "data").iterdir()) == sorted(REPORT_FILES)
    assert read_run(directory) == run


def test_demo_submissions_are_not_compared(database, store, task, root, fake) -> None:
    """デモへの提出は誰でも出せる場所のものなので比べない（教員・TA の提出とは違う）。"""
    kept = submit(database, store, task, "1", SOURCE_A)
    submit(database, store, task, "2", SOURCE_A)
    demo = submit(database, store, task, "5", b"demo", is_demo=True)

    run = run_for_task(
        database, task.id, artifact_store=store, root=root, now=AT, sandbox_factory=fake
    )

    assert run is not None and kept in {s.submission_id for s in run.submissions}
    assert demo not in {s.submission_id for s in run.submissions}


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


def test_text_mode_compares_the_files_as_characters(database, store, task, root, fake):
    """**テキストとして**: コメントも含めて文字で比べる。Markdown はテキストだけに入る（#569）。"""
    code = submit(database, store, task, "1", SOURCE_A + "// 説明\n".encode())
    notes = submit(
        database, store, task, "2", "# 考察\n".encode(), filename="r.md", kind=ArtifactKind.MARKDOWN
    )
    other = submit(database, store, task, "3", SOURCE_A)
    kwargs = {"artifact_store": store, "root": root, "now": AT, "sandbox_factory": fake}

    as_code = run_for_task(database, task.id, **kwargs)
    as_text = run_for_task(database, task.id, mode=SimilarityMode.TEXT, **kwargs)

    assert as_code is not None and as_text is not None
    assert {s.submission_id for s in as_code.submissions} == {code, other}
    assert {s.submission_id for s in as_text.submissions} == {code, notes, other}
    assert as_text.mode is SimilarityMode.TEXT and as_text.language == "char"
    assert as_text.params == TEXT_PARAMS and as_text.unit_of_length == "文字"
    assert fake.argv[1][:4] == ("dolos", "run", "-l", "char")
    assert fake.argv[1][-len(TEXT_PARAMS) - 1 : -1] == TEXT_PARAMS
    assert sorted(fake.seen[1]) == ["ds/S-001.txt", "ds/S-002.txt", "ds/S-003.txt", "ds/info.csv"]
    assert "説明" in fake.seen[1]["ds/S-001.txt"].decode()

    # **同じ人は同じ仮の名前**（画面で 2 つの類似度を並べる）。Markdown の人は欠番になる。
    names = {s.submission_id: s.pseudonym for s in as_code.submissions}
    assert names == {
        s.submission_id: s.pseudonym for s in as_text.submissions if s.submission_id != notes
    }
    assert sorted(names.values()) == ["S-001", "S-003"]

    # 回は比べ方ごとに 1 つずつ残る（片方を回し直しても、もう片方は消えない）。
    assert as_code.run_id != as_text.run_id
    assert list_runs(root, task.course_id) == [as_code, as_text]
    assert latest_run(root, task.course_id, task.id, SimilarityMode.TEXT) == as_text
    submit(database, store, task, "4", SOURCE_A)
    again = run_for_task(database, task.id, **(kwargs | {"now": AT + timedelta(hours=1)}))
    assert again is not None and run_dir(root, as_text).exists()
    assert not run_dir(root, as_code).exists()


def test_an_earlier_report_reads_as_code(database, store, task, root, fake) -> None:
    """2026-10-28 より前の `run.json` には `mode` が無い。コードとして比べた回として読む。"""
    submit(database, store, task, "1", SOURCE_A)
    submit(database, store, task, "2", SOURCE_A)
    run = run_for_task(
        database, task.id, artifact_store=store, root=root, now=AT, sandbox_factory=fake
    )
    assert run is not None
    path = run_dir(root, run) / "run.json"
    document = path.read_text(encoding="utf-8").replace('"mode": "code",', "")
    assert '"mode"' not in document
    path.write_text(document, encoding="utf-8")
    assert read_run(run_dir(root, run)) == run


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

    # テキストとしては、変数名を変えただけで一致が切れる（文字で比べるので）。
    text = run_for_task(
        database, task.id, artifact_store=store, root=root, now=AT, mode=SimilarityMode.TEXT
    )
    assert text is not None and text.measured and text.language == "char", text
    rows = list(csv.DictReader((run_dir(root, text) / "data" / "pairs.csv").open()))
    assert len(rows) == 3
    assert max(float(row["similarity"]) for row in rows) < 0.9


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

    def record(database, task_id, *, artifact_store, root, force, mode, content):
        called.append((str(task_id), force, mode))
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
    assert called == [(str(task.id), True, mode) for mode in SimilarityMode]
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
    assert [run.mode for run in later.ran] == list(DOLOS_MODES), "コードとテキストの両方"

    # **何度走らせても同じ結果になる。** 入力が同じなら回さない。
    again = code_similarity.sweep(database, now=CLOSE + timedelta(hours=2), **kwargs)
    assert again.ran == [] and len(fake.argv) == 2


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


def test_it_waits_until_grading_is_done(database, store, task, root, fake) -> None:
    """**締切から 1 時間たっても、採点が残っていれば回さない**（試験では締切の後に採点が回る）。

    寝かせてある一括採点のジョブ（試験）も「終わっていない」に数える。
    """
    from aijudge_core.ids import GradingJobId, new_id
    from aijudge_submission import GradingJob, JobState

    graded = submit(database, store, task, "1", SOURCE_A, at=CLOSE - timedelta(hours=1))
    submit(database, store, task, "2", SOURCE_A, at=CLOSE - timedelta(hours=1))
    with database.unit_of_work() as uow:
        version = uow.tasks.latest_version(task.id)
        job = uow.jobs.enqueue(
            GradingJob(
                id=GradingJobId(new_id("job")),
                tenant_id=TENANT,
                submission_id=graded,
                task_version_id=version.id,
                subject_profile="cs_lang_c_intro",
                idempotency_key="sim-wait",
                # 採点開始の時刻まで寝かせてある（試験の一括採点）。
                available_at=CLOSE + timedelta(days=1),
                created_at=CLOSE,
                updated_at=CLOSE,
            )
        )
        uow.commit()
    kwargs = {"root": root, "artifact_store": store, "sandbox_factory": fake}

    waiting = code_similarity.sweep(database, now=CLOSE + timedelta(hours=2), **kwargs)
    assert waiting.ran == [] and fake.argv == []
    assert [(w.task_id, w.pending) for w in waiting.grading] == [(task.id, 1)]
    # 手で回すときは --force で待たずに回せる。
    with pytest.raises(code_similarity.GradingPending):
        run_for_task(database, task.id, now=CLOSE + timedelta(hours=2), **kwargs)

    with database.unit_of_work() as uow:
        uow.jobs.update(job.model_copy(update={"state": JobState.CANCELLED}))
        uow.commit()
    done = code_similarity.sweep(database, now=CLOSE + timedelta(hours=3), **kwargs)
    assert len(done.ran) == 2 and done.grading == []


# --------------------------------------------------------------------------
# 内容として（埋め込み・content_similarity）
# --------------------------------------------------------------------------

ABOUT_TCP = "TCP の接続は SYN、SYN/ACK、ACK の 3 回のやり取りで確立されることを観察した。" * 3
SAID_AGAIN = "観察の結果、TCP では 3 つのパケットを交換してから接続が張られると分かった。" * 3
ABOUT_DNS = "DNS の応答は TTL の間キャッシュされ、2 回目の問い合わせは速かった。" * 3


@dataclass
class FakeGateway:
    """言い換えも同じ向きになる埋め込み（本物のモデルの代わり）。送られた文と種別を覚える。"""

    fail: bool = False
    seen: list[tuple[tuple[str, ...], object]] = field(default_factory=list)

    def embed(self, texts, *, model, data_class, timeout_seconds):
        from aijudge_llm_gateway import LlmError

        self.seen.append((texts, data_class))
        if self.fail:
            raise LlmError("model not found")
        return tuple((1.0, 0.0) if "TCP" in text else (0.0, 1.0) for text in texts)


def _settings(gateway: FakeGateway):
    from aijudge_course_admin.content_similarity import ContentSettings

    return ContentSettings(gateway=gateway, model="qwen3-embedding:4b")  # type: ignore[arg-type]


def _report(database, store, task, learner, body: str, *, kind=ArtifactKind.MARKDOWN):
    suffix = ".md" if kind is ArtifactKind.MARKDOWN else ".pdf"
    return submit(database, store, task, learner, body.encode(), filename=f"r{suffix}", kind=kind)


def test_paragraphs_are_joined_and_split() -> None:
    from aijudge_course_admin.content_similarity import (
        MAX_PARAGRAPH_CHARS,
        MIN_PARAGRAPH_CHARS,
        paragraphs,
    )

    short = "## 目的\n\n" + "あ" * (MIN_PARAGRAPH_CHARS + 5)
    assert paragraphs(short) == ["## 目的 " + "あ" * (MIN_PARAGRAPH_CHARS + 5)], (
        "見出しは次へつなぐ"
    )
    # PDF から取り出した本文には空行が無い。文の切れ目で分ける。
    long = ("い" * 99 + "。") * 20
    parts = paragraphs(long)
    assert len(parts) > 1 and all(len(p) <= MAX_PARAGRAPH_CHARS for p in parts)
    assert "".join(parts) == long


def test_content_finds_the_paraphrase(database, store, task, root, fake) -> None:
    from aijudge_course_admin.content_similarity import PASSAGES_FILE
    from aijudge_llm_gateway import DataClass

    first = _report(database, store, task, "1", ABOUT_TCP)
    second = _report(database, store, task, "2", SAID_AGAIN)
    _report(database, store, task, "3", ABOUT_DNS)
    submit(database, store, task, "4", SOURCE_A)  # コードは内容としては比べない
    gateway = FakeGateway()

    run = run_for_task(
        database,
        task.id,
        artifact_store=store,
        root=root,
        now=AT,
        sandbox_factory=fake,
        mode=SimilarityMode.CONTENT,
        content=_settings(gateway),
    )

    assert run is not None and run.measured and run.mode is SimilarityMode.CONTENT
    assert run.image == "embedding:qwen3-embedding:4b" and run.unit_of_length == "段落"
    assert len(run.submissions) == 3 and fake.argv == [], "Dolos は呼ばない"
    # **学習者の文は PERSONAL で送る**（P7。手元のモデルにしか行かない）。
    assert {data_class for _texts, data_class in gateway.seen} == {DataClass.PERSONAL}
    rows = list(csv.DictReader((run_dir(root, run) / "data" / "pairs.csv").open()))
    top = rows[0]
    names = {s.submission_id: s.pseudonym for s in run.submissions}
    assert {top["leftFilePath"], top["rightFilePath"]} == {
        f"{names[first]}.txt",
        f"{names[second]}.txt",
    }
    assert float(top["similarity"]) > 0.99 and int(top["longestFragment"]) == 1
    assert run.baseline == 0.0, "3 組のうち 2 組は無関係（中央値）"
    evidence = json.loads((run_dir(root, run) / "data" / PASSAGES_FILE).read_text("utf-8"))
    passage = evidence[f"{names[first]}|{names[second]}"][0]
    assert "SYN" in passage["left"] and "3 つのパケット" in passage["right"]

    # 同じ入力・同じモデルなら回さない。モデルを替えたら測り直す（値が比べられない）。
    kwargs = {"artifact_store": store, "root": root, "mode": SimilarityMode.CONTENT}
    assert run_for_task(database, task.id, now=AT, content=_settings(gateway), **kwargs) is None
    from aijudge_course_admin.content_similarity import ContentSettings

    other = ContentSettings(gateway=gateway, model="bge-m3")  # type: ignore[arg-type]
    assert run_for_task(database, task.id, now=AT, content=other, **kwargs) is not None


def test_content_is_off_without_a_model(database, store, task, root, fake) -> None:
    _report(database, store, task, "1", ABOUT_TCP)
    _report(database, store, task, "2", SAID_AGAIN)
    kwargs = {"artifact_store": store, "root": root, "sandbox_factory": fake}
    assert run_for_task(database, task.id, now=AT, mode=SimilarityMode.CONTENT, **kwargs) is None
    report = code_similarity.sweep(database, now=CLOSE + timedelta(hours=1), **kwargs)
    assert SimilarityMode.CONTENT not in {r.mode for r in report.ran}


def test_an_unavailable_model_is_retried(database, store, task, root) -> None:
    _report(database, store, task, "1", ABOUT_TCP)
    _report(database, store, task, "2", SAID_AGAIN)
    gateway = FakeGateway(fail=True)
    kwargs = {
        "artifact_store": store,
        "root": root,
        "mode": SimilarityMode.CONTENT,
        "content": _settings(gateway),
    }
    failed = run_for_task(database, task.id, now=AT, **kwargs)
    assert failed is not None and failed.not_measured is NotMeasuredReason.EMBEDDING_UNAVAILABLE
    gateway.fail = False
    again = run_for_task(database, task.id, now=AT + timedelta(minutes=15), **kwargs)
    assert again is not None and again.measured


def test_pdfs_are_read_with_the_grading_extractor(
    database, store, task, root, fake, monkeypatch
) -> None:
    """PDF は採点と同じ抽出で本文にする。読めない PDF は入れず、数を残す。"""
    import aijudge_ext_document_text as extractor

    def text_of(payload: bytes, kind):
        if payload == b"scan":
            raise extractor.DocumentTextError("文字が埋め込まれていません")
        return payload.decode()

    monkeypatch.setattr(extractor, "text_of", text_of)
    _report(database, store, task, "1", ABOUT_TCP, kind=ArtifactKind.PDF)
    _report(database, store, task, "2", SAID_AGAIN, kind=ArtifactKind.PDF)
    _report(database, store, task, "3", "scan", kind=ArtifactKind.PDF)
    kwargs = {"artifact_store": store, "root": root, "now": AT, "sandbox_factory": fake}

    content = run_for_task(
        database, task.id, mode=SimilarityMode.CONTENT, content=_settings(FakeGateway()), **kwargs
    )
    assert content is not None and len(content.submissions) == 2
    assert "読めなかった提出 1 件" in content.detail
    # テキストとして（Dolos）も、取り出した本文を渡す。
    text = run_for_task(database, task.id, mode=SimilarityMode.TEXT, **kwargs)
    assert text is not None and len(text.submissions) == 2
    assert "SYN" in fake.seen[0]["ds/" + text.submissions[0].pseudonym + ".txt"].decode()
    assert run_for_task(database, task.id, **kwargs) is None, "PDF はコードとしては比べない"
