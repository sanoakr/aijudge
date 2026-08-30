"""採点 → 習熟度の配線を固定する（S5 → S7、設計方針 §2.3）。

固定したいのは 4 つ。

繋がる       Q-matrix を宣言した課題を採点すると、習熟度が付く。
落ちても採点は完了 S7 が壊れていても採点は成立する（P2）。イベントは残り、再送される。
冪等         リレーが再送しても習熟度は一度しか動かない。
宣言が無ければ何もしない KC を宣言していない課題では、習熟度は付かないが失敗もしない。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from aijudge_authoring.importers import sharif_judge
from aijudge_core import ArtifactKind, Course, Task
from aijudge_core.ids import CourseId, KcId, TenantId, UserId
from aijudge_core.knowledge import KnowledgeComponent
from aijudge_eval_rubric_ai_judge import EvidenceSpan, RubricAiJudge, Verdict
from aijudge_grader import EventRelay, GradingWorker, subscribe_skills
from aijudge_grading import EvaluatorRegistry
from aijudge_llm_gateway import LlmGateway, ScriptedProvider
from aijudge_persistence import Database
from aijudge_submission import FilesystemArtifactStore, IncomingFile, SubmissionService

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_TASK = REPO_ROOT / "evals" / "golden" / "cs_intro_c" / "example-task" / "task"
EXAMPLE_SOURCE = REPO_ROOT / "evals" / "golden" / "cs_intro_c" / "example-task" / "marks" / "s001.c"
PROFILES = REPO_ROOT / "subjects"

TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
LEARNER = UserId("usr_" + "4" * 32)
AUTHOR = UserId("usr_" + "a" * 32)
KC_KEYS = ("cs.loops.termination", "cs.io.formatted_input")

AI_SAYS = Verdict(
    observation="1 文字の変数名で最大・最小・合計を保持している。",
    level=1,
    evidence=[EvidenceSpan(start_line=5, end_line=5, quote="int b = 0, c = 0, d = 0;")],
    rationale="変数名から役割が読み取れません。",
).model_dump_json()

needs_c_compiler = pytest.mark.skipif(
    shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="no C compiler available",
)


class World:
    def __init__(self, tmp_path: Path, *, kcs: tuple[str, ...] = KC_KEYS) -> None:
        self.database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
        self.store = FilesystemArtifactStore(tmp_path / "artifacts")
        registry = EvaluatorRegistry().load_installed()
        registry.replace(RubricAiJudge(LlmGateway(ScriptedProvider([AI_SAYS] * 12)), model="stub"))

        self.service = SubmissionService(self.database.unit_of_work, self.store)
        self.worker = GradingWorker(
            self.database, self.store, profiles_dir=PROFILES, registry=registry
        )
        self.relay = EventRelay(self.database)

        self.task_version = sharif_judge.import_problem(
            EXAMPLE_TASK,
            subject_profile="cs_intro_c",
            authored_by=AUTHOR,
            readability_weight=0.3,
            knowledge_components=kcs,
        )
        with self.database.unit_of_work() as uow:
            uow.identity.save_course(
                Course(
                    id=COURSE,
                    tenant_id=TENANT,
                    code="prog2",
                    title="プログラミング演習 II",
                    term="2026-前期",
                    subject_profile="cs_intro_c",
                )
            )
            uow.tasks.save_task(Task(id=self.task_version.task_id, course_id=COURSE, title="例題"))
            uow.tasks.save_version(self.task_version)
            for key in kcs:
                namespace, *path = key.split(".")
                uow.skills.save_kc(
                    KnowledgeComponent(
                        id=KcId(_kc_id(key)),
                        namespace=namespace,
                        path=tuple(path),
                        label=key,
                    )
                )
            uow.commit()

    def submit(self):
        return self.service.accept(
            tenant_id=TENANT,
            task_version_id=self.task_version.id,
            learner_id=LEARNER,
            subject_profile="cs_intro_c",
            files=[
                IncomingFile(
                    filename="main.c",
                    kind=ArtifactKind.CODE,
                    payload=EXAMPLE_SOURCE.read_bytes(),
                )
            ],
        )

    def states(self):
        with self.database.unit_of_work() as uow:
            return uow.skills.list_states(TENANT, LEARNER)


def _kc_id(key: str) -> str:
    from aijudge_core import derived_id

    return derived_id("kc", key)


@pytest.fixture
def world(tmp_path: Path):
    made = World(tmp_path)
    yield made
    made.database.dispose()


@needs_c_compiler
def test_grading_a_task_with_a_q_matrix_updates_mastery(world: World) -> None:
    subscribe_skills(world.relay, world.database)
    world.submit()
    world.worker.run_until_empty()
    world.relay.drain()

    states = world.states()
    assert {state.kc_id for state in states} == {KcId(_kc_id(key)) for key in KC_KEYS}
    for state in states:
        assert state.observation_count == 1
        assert state.evidence, "根拠が空のまま習熟度だけ付いている"


@needs_c_compiler
def test_the_relay_can_redeliver_without_moving_the_estimate_twice(world: World) -> None:
    """**購読側は冪等**（relay.py 冒頭）。再送は起こる前提で作ってある。"""
    subscribe_skills(world.relay, world.database)
    world.submit()
    world.worker.run_until_empty()
    world.relay.drain()
    before = {s.kc_id: s.mastery for s in world.states()}

    # 同じイベントをもう一度届ける（送信済みにする前に落ちた場合と同じ状況）。
    with world.database.unit_of_work() as uow:
        events = uow.outbox.unpublished(100)
    from aijudge_grader.skill_subscriber import SkillSubscriber

    subscriber = SkillSubscriber(world.database)
    for event in events:
        subscriber(event)

    assert {s.kc_id: s.mastery for s in world.states()} == before


@needs_c_compiler
def test_grading_completes_even_when_the_skill_side_is_broken(world: World) -> None:
    """**S7 が落ちても採点は完了する**（P2）。イベントは outbox に残る。

    習熟度が遅れて付くことはあっても、採点が止まってはならない。
    """

    def explode(event) -> None:
        raise RuntimeError("S7 は落ちている")

    world.relay.subscribe("grading.completed", explode)
    world.submit()
    graded, errors = world.worker.run_until_empty()

    assert graded == 2, errors
    assert errors == ()

    # 配れなかったイベントは送信済みにしない ── 次回に再送される。
    # **他のイベントは通る**（購読者が居ないものは配達成功として扱う）ので、
    # 残っているのが grading.completed だけであることまで見る。
    world.relay.drain()
    with world.database.unit_of_work() as uow:
        remaining = uow.outbox.unpublished(100)
    assert remaining, "落ちたイベントが失われている"
    assert {event.type for event in remaining} == {"grading.completed"}


@needs_c_compiler
def test_a_task_without_knowledge_components_updates_nothing(tmp_path: Path) -> None:
    """KC を宣言していない課題では習熟度が付かない。**それは失敗ではない。**"""
    made = World(tmp_path, kcs=())
    try:
        subscribe_skills(made.relay, made.database)
        made.submit()
        graded, errors = made.worker.run_until_empty()
        made.relay.drain()

        assert graded == 2, errors
        assert made.states() == ()
    finally:
        made.database.dispose()
