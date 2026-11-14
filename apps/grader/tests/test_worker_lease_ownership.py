"""リースまわりの配線を確かめる（#704）。

`test_worker.py` は `renew_lease` を直接呼ぶところまでしか見ておらず、採点の
あいだ別スレッドでリースを延ばし続ける `_keeping_lease`（#399）を外しても、
延ばす間隔を誤っても、どのテストも落ちなかった。停止時の解放（`_record_release`）
と列の付け替え（`_record_hand_off`）の持ち主確認も、リースを持ったままの正常系
でしか通っていなかった。

**実時間では待たない。** 延長のスレッドが使う `threading.Event` だけを、
テストから 1 周ずつ進められるものに差し替える。間隔そのもの（`wait` に渡る秒数）
は記録して比べるので、遅い CI でも結果は変わらない。
"""

from __future__ import annotations

import shutil
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import aijudge_grader.worker as worker_module
from aijudge_authoring.importers import sharif_judge
from aijudge_core import ArtifactKind, Role, Task
from aijudge_core.ids import CourseId, TenantId, UserId
from aijudge_grader import GradingWorker
from aijudge_grader.worker import (
    DEFAULT_LEASE_SECONDS,
    LEASE_LOST,
    LEASE_RENEWAL_FRACTION,
)
from aijudge_grading import EvaluatorRegistry
from aijudge_persistence import Database
from aijudge_submission import (
    FilesystemArtifactStore,
    GradingJob,
    IncomingFile,
    JobLane,
    JobState,
    SubmissionService,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_TASK = REPO_ROOT / "evals" / "golden" / "cs_lang_c_intro" / "example-task" / "task"
EXAMPLE_SOURCE = (
    REPO_ROOT / "evals" / "golden" / "cs_lang_c_intro" / "example-task" / "marks" / "s001.c"
)
PROFILES = REPO_ROOT / "subjects"
PROFILE = "cs_lang_c_intro"

TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
LEARNER = UserId("usr_" + "2" * 32)
AUTHOR = UserId("usr_" + "3" * 32)

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
#: 古いワーカーが取ったときのリース。切れるまで時計を進めて別のワーカーに取らせる。
STALE_LEASE_SECONDS = 60.0
#: 延長が 1 周するまでに進める時計の量。リースより短く、延長で期限が後ろへ動く量。
RENEWAL_ADVANCE_SECONDS = 100.0
#: スレッドの合図を待つ上限。**正しい実装では即座に返る**ので、ここに届くのは
#: 延長が起きなかったとき（変異が生き残ったとき）だけ。テストが止まらないための番兵。
WATCHDOG_SECONDS = 5.0

needs_c_compiler = pytest.mark.skipif(
    shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="no C compiler available",
)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class SteppedEvent:
    """延長のスレッドの `stop` の代わり。`wait` は `tick()` 1 回につき 1 周だけ返る。

    本物の `Event.wait(interval)` は interval 秒眠るので、既定のリース（900 秒）では
    テスト中に 1 度も起きない。ここでは `wait` に渡った秒数を記録し、テストが
    `tick()` したときだけ「時間切れ」（False）で返す。`set()` されたら True で返る。
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._set = False
        self._ticks = 0
        self.timeouts: list[float | None] = []

    def set(self) -> None:
        with self._condition:
            self._set = True
            self._condition.notify_all()

    def is_set(self) -> bool:
        with self._condition:
            return self._set

    def tick(self) -> None:
        with self._condition:
            self._ticks += 1
            self._condition.notify_all()

    def wait(self, timeout: float | None = None) -> bool:
        with self._condition:
            self.timeouts.append(timeout)
            self._condition.wait_for(lambda: self._set or self._ticks > 0, WATCHDOG_SECONDS)
            if self._set:
                return True
            if self._ticks > 0:
                self._ticks -= 1
            return False


class World:
    """1 テストぶんの DB・ストア・ワーカー。採点はしないので LLM も評価器の差し替えも要らない。"""

    def __init__(self, tmp_path: Path) -> None:
        self.database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
        self.store = FilesystemArtifactStore(tmp_path / "artifacts")
        self.clock = Clock()
        self.service = SubmissionService(self.database.unit_of_work, self.store, clock=self.clock)
        self.worker = GradingWorker(
            self.database,
            self.store,
            profiles_dir=PROFILES,
            registry=EvaluatorRegistry().load_installed(),
            clock=self.clock,
        )
        self.task_version = sharif_judge.import_problem(
            EXAMPLE_TASK,
            course_id=COURSE,
            subject_profile=PROFILE,
            authored_by=AUTHOR,
            readability_weight=0.3,
        )
        with self.database.unit_of_work() as uow:
            uow.tasks.save_task(Task(id=self.task_version.task_id, course_id=COURSE, title="例題"))
            uow.tasks.save_version(self.task_version)
            uow.commit()

    def submit(self):
        return self.service.accept(
            tenant_id=TENANT,
            task_version_id=self.task_version.id,
            learner_id=LEARNER,
            submitted_as=Role.LEARNER,
            subject_profile=PROFILE,
            files=[
                IncomingFile(
                    filename="main.c", kind=ArtifactKind.CODE, payload=EXAMPLE_SOURCE.read_bytes()
                )
            ],
        )

    def job(self, job: GradingJob) -> GradingJob:
        with self.database.unit_of_work() as uow:
            current = uow.jobs.get(job.id)
        assert current is not None
        return current

    def reserve_stale(self) -> GradingJob:
        """古いワーカー（slow）に取らせ、そのリースが切れるまで時計を進める。"""
        with self.database.unit_of_work() as uow:
            stale = uow.jobs.reserve(self.clock(), worker="slow", lease_seconds=STALE_LEASE_SECONDS)
            assert stale is not None
            uow.commit()
        self.clock.advance(STALE_LEASE_SECONDS * 2)
        return stale

    def close(self) -> None:
        self.database.dispose()


@pytest.fixture
def world(tmp_path: Path):
    instance = World(tmp_path)
    yield instance
    instance.close()


# --------------------------------------------------------------------------
# 採点のあいだリースを延ばし続ける（#399）
# --------------------------------------------------------------------------


class _GradingAborted(Exception):
    """差し替えた採点の終わり。中身の確かめはテスト側でするので、結果は捨ててよい。"""


def test_the_lease_is_kept_alive_while_grading(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """採点が長引いても、そのあいだリースが後ろへ延び続ける（#399）。

    延ばさないと、AI 段階が 900 秒を超えたジョブを別のワーカーが取り直し、同じ
    提出を二重に採点して、先の結果は `LEASE_LOST` で捨てられる。ここで落とす変異:
    `with self._keeping_lease(job)` を外す・間隔をリースより長くする（`* 3`）・
    `stop.wait` の条件を反転して 1 度も延ばさずに抜ける。
    """
    world.submit()

    stop = SteppedEvent()
    # 差し替えるのはワーカーのモジュールから見える `threading` だけ。`Thread` は本物の
    # まま（標準ライブラリの内部が使う `threading.Event` には触れない）。
    monkeypatch.setattr(
        worker_module,
        "threading",
        SimpleNamespace(Event=lambda: stop, Thread=threading.Thread),
    )

    renewed = threading.Event()
    renew_lease = world.worker.renew_lease

    def renew_and_tell(job: GradingJob) -> bool:
        held = renew_lease(job)
        renewed.set()
        return held

    monkeypatch.setattr(world.worker, "renew_lease", renew_and_tell)

    # 差し替えた採点の中で `assert` すると `run_once` の `except Exception` に
    # 飲まれて「一時的な失敗」になるだけなので、見たものを記録して外で比べる。
    seen: dict[str, object] = {}

    def slow_grade(job: GradingJob):
        seen["reserved_until"] = world.job(job).lease_expires_at
        world.clock.advance(RENEWAL_ADVANCE_SECONDS)
        stop.tick()  # 延長のスレッドの 1 周ぶん時間が経った
        seen["renewed"] = renewed.wait(WATCHDOG_SECONDS)
        seen["renewed_until"] = world.job(job).lease_expires_at
        seen["now"] = world.clock()
        seen["keeper"] = f"lease-{job.id}"
        raise _GradingAborted

    monkeypatch.setattr(world.worker, "_grade", slow_grade)
    result = world.worker.run_once()
    assert result is not None

    assert seen["renewed"] is True, "採点のあいだにリースが 1 度も延びなかった"
    reserved_until = seen["reserved_until"]
    renewed_until = seen["renewed_until"]
    assert isinstance(reserved_until, datetime) and isinstance(renewed_until, datetime)
    assert renewed_until > reserved_until
    assert renewed_until == seen["now"] + timedelta(seconds=DEFAULT_LEASE_SECONDS)

    # 延ばす間隔はリースより十分短い（切れる前に必ず 1 度は延びる）。
    interval = stop.timeouts[0]
    assert interval == pytest.approx(DEFAULT_LEASE_SECONDS * LEASE_RENEWAL_FRACTION)
    assert interval is not None and interval < DEFAULT_LEASE_SECONDS

    # 採点が終わったら延長のスレッドも止まっている。止まらないと、終わった
    # ジョブのリースを延ばそうとし続ける。
    assert stop.is_set()
    alive = [thread.name for thread in threading.enumerate() if thread.is_alive()]
    assert seen["keeper"] not in alive


# --------------------------------------------------------------------------
# リースを失ったワーカーは、解放でも付け替えでも何も書かない
# --------------------------------------------------------------------------


def test_a_stale_worker_does_not_release_or_move_a_job_another_worker_holds(
    world: World,
) -> None:
    """リースが切れて別のワーカーが採点中のジョブを、古いワーカーが触らない。

    持ち主の確認が無いと、停止した古いワーカーが `released()` でジョブを QUEUED
    に戻して試行を 1 減らし、採点中の提出がもう一度取られて二重に採点される。
    付け替え（`moved_to`）も同じく、他人の採点中のジョブを別の列へ回してしまう。
    """
    world.submit()
    stale = world.reserve_stale()
    with world.database.unit_of_work() as uow:
        taken_over = uow.jobs.reserve(world.clock(), worker="other", lease_seconds=60.0)
        assert taken_over is not None
        uow.commit()

    released = world.worker._record_release(stale)
    moved = world.worker._record_hand_off(stale, JobLane.TRANSCRIPTION)

    assert released.error == LEASE_LOST
    assert moved.error == LEASE_LOST and not moved.handed_off
    current = world.job(stale)
    assert current.state is JobState.RUNNING
    assert current.worker == "other"
    assert current.attempts == taken_over.attempts
    assert current.lane is taken_over.lane


@needs_c_compiler
def test_a_stale_worker_does_not_release_or_move_a_finished_job(world: World) -> None:
    """取り直した側が採点を終えていたら、古いワーカーの解放・付け替えは引き下がるだけ。

    確認が無いと `released()` / `moved_to()` が DONE のジョブに `ValueError` を
    投げ、SIGTERM の処理中にワーカーが例外で落ちる。
    """
    world.submit()
    stale = world.reserve_stale()
    taken_over = world.worker.run_once()
    assert taken_over is not None and taken_over.graded, taken_over

    released = world.worker._record_release(stale)
    moved = world.worker._record_hand_off(stale, JobLane.TRANSCRIPTION)

    assert released.error == LEASE_LOST
    assert moved.error == LEASE_LOST and not moved.handed_off
    current = world.job(stale)
    assert current.state is JobState.DONE
    assert current.lane is stale.lane
