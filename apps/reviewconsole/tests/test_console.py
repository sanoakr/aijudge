"""レビューコンソールの規則を固定する。

固定したい規則は 3 つ。

1. **コンソールは採点しない。** 採点はワーカーが先に走らせる。レビューが
   採点を起動すると、測定用データの入力が採点の前提条件に戻る（ADR 0007）。
2. **blind 画面に AI の判定が漏れていない。** CSS で隠すのでは不十分なので、
   レスポンス本文を直接検査している。
3. **採点できないコースの提出は見えない。** UI で隠すのは表示の都合であって
   権限ではない。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aijudge_authoring.importers import sharif_judge
from aijudge_core import ArtifactKind, Course, Role, Task
from aijudge_core.ids import CourseId, TenantId, UserId
from aijudge_eval_rubric_ai_judge import EvidenceSpan, RubricAiJudge, Verdict
from aijudge_grader import GradingWorker
from aijudge_grading import EvaluatorRegistry
from aijudge_identity import AuthService
from aijudge_llm_gateway import LlmGateway, ScriptedProvider
from aijudge_persistence import Database, ObservationFileStore
from aijudge_reviewconsole import SESSION_COOKIE, Console, create_app, is_blind_sample
from aijudge_submission import (
    FilesystemArtifactStore,
    IncomingFile,
    SubmissionService,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_TASK = REPO_ROOT / "evals" / "golden" / "cs_intro_c" / "example-task" / "task"
EXAMPLE_SOURCE = REPO_ROOT / "evals" / "golden" / "cs_intro_c" / "example-task" / "marks" / "s001.c"
PROFILES = REPO_ROOT / "subjects"

TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
AUTHOR = UserId("usr_" + "a" * 32)
PASSWORD = "correct horse battery"
PROFILE = "cs_intro_c"
PROFILE_SAMPLES = 3

needs_c_compiler = pytest.mark.skipif(
    shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="no C compiler available",
)

AI_RATIONALE = "AIRATIONALEMARKER 変数名は明快で構造も追えます。"
# **模型そのものから作る。** 文字列で書くと、`Verdict` に必須項目が
# 増えたときに気づけない（実際に `observation` が増えて気づけなかった）。
AI_SAYS_3 = Verdict(
    observation="変数の宣言がまとまっており、処理の流れが追える。",
    level=3,
    evidence=[EvidenceSpan(start_line=5, end_line=5, quote="int b = 0, c = 0, d = 0;")],
    rationale=AI_RATIONALE,
).model_dump_json()


class World:
    def __init__(self, tmp_path: Path, *, blind_rate: float | None = None) -> None:
        self.database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
        self.store = FilesystemArtifactStore(tmp_path / "artifacts")
        self.observations = ObservationFileStore(tmp_path / "observations")
        self.provider = ScriptedProvider([AI_SAYS_3] * PROFILE_SAMPLES * 6)
        registry = EvaluatorRegistry().load_installed()
        registry.replace(RubricAiJudge(LlmGateway(self.provider), model="stub"))

        self.worker = GradingWorker(
            self.database, self.store, profiles_dir=PROFILES, registry=registry
        )
        self.console = Console(
            self.database,
            self.store,
            profiles_dir=PROFILES,
            observations=self.observations,
        )
        if blind_rate is not None:
            self.console._rates[PROFILE] = blind_rate
        self.client = TestClient(create_app(self.console))
        self.submissions = SubmissionService(self.database.unit_of_work, self.store)

        with self.database.unit_of_work() as uow:
            uow.identity.save_course(
                Course(
                    id=COURSE,
                    tenant_id=TENANT,
                    code="prog2",
                    title="プログラミング演習 II",
                    term="2026-前期",
                    subject_profile=PROFILE,
                )
            )
            uow.commit()
        self.task_version = sharif_judge.import_problem(
            EXAMPLE_TASK,
            subject_profile=PROFILE,
            authored_by=AUTHOR,
            readability_weight=0.3,
        )
        with self.database.unit_of_work() as uow:
            uow.tasks.save_task(Task(id=self.task_version.task_id, course_id=COURSE, title="例題"))
            uow.tasks.save_version(self.task_version)
            uow.commit()

    def register(self, login: str, *, role: Role):
        with self.database.unit_of_work() as uow:
            service = AuthService(uow.identity)
            principal = service.register(
                tenant_id=TENANT, login=login, display_name=login, password=PASSWORD
            )
            service.enroll(tenant_id=TENANT, course_id=COURSE, user_id=principal.user_id, role=role)
            uow.commit()
        return principal

    def login(self, login: str) -> None:
        response = self.client.post(
            "/login", data={"login": login, "password": PASSWORD}, follow_redirects=False
        )
        assert response.status_code == 303, response.text
        self.client.cookies.set(SESSION_COOKIE, response.cookies[SESSION_COOKIE])

    def submit(self, learner, payload: bytes | None = None):
        return self.submissions.accept(
            tenant_id=TENANT,
            task_version_id=self.task_version.id,
            learner_id=learner.user_id,
            subject_profile=PROFILE,
            files=[
                IncomingFile(
                    filename="main.c",
                    kind=ArtifactKind.CODE,
                    payload=payload or EXAMPLE_SOURCE.read_bytes(),
                )
            ],
        )

    def close(self) -> None:
        self.database.dispose()


@pytest.fixture
def world(tmp_path: Path):
    instance = World(tmp_path, blind_rate=0.0)
    yield instance
    instance.close()


@pytest.fixture
def blind_world(tmp_path: Path):
    """全件が blind 抽出に当たる世界（抽出の当落に依存しないテスト用）。"""
    instance = World(tmp_path, blind_rate=1.0)
    yield instance
    instance.close()


JUSTIFICATION = "テスト実行の結果を確認しました。判定は妥当です。"


def _agree_form(world: World, machine: dict) -> dict[str, str]:
    """AI の判定に同意するフォーム。

    **ブラウザが送る形と同じにする。** 段階の項目名は観点ごとに違う
    （`level_<code>`）。共有の名前で送るテストは、実際のフォームで
    観点をまたいで 1 つしか選べないバグを見逃す（実際に見逃した）。

    根拠説明は必須（ADR 0009）。学習者には AI の判定が既に示されており、
    「確認した」だけでは何も返らない。
    """
    form = {
        f"level_{c.code}": str(machine[c.id])
        for c in world.task_version.criteria
        if c.id in machine
    }
    form["comment"] = JUSTIFICATION
    return form


def _request_review(world: World, submission_id) -> None:
    """学習者として再確認を依頼する。教員の待ち行列に載せるため。"""
    from datetime import UTC, datetime

    from aijudge_core import ReviewRequest
    from aijudge_core.ids import ReviewRequestId, new_id

    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(submission_id)
        assert run is not None
        submission = uow.submissions.get(submission_id)
        assert submission is not None
        uow.reviews.save_request(
            ReviewRequest(
                id=ReviewRequestId(new_id("rrq")),
                submission_id=submission_id,
                grading_run_id=run.id,
                learner_id=submission.learner_id,
                reason="テストケース 3 の想定出力が仕様と違うと思います。",
                requested_at=datetime.now(UTC),
            )
        )
        uow.commit()


def _instructor_and_submission(world: World):
    learner = world.register("s2400001", role=Role.LEARNER)
    instructor = world.register("instructor", role=Role.INSTRUCTOR)
    accepted = world.submit(learner)
    world.login("instructor")
    return instructor, accepted


# --------------------------------------------------------------------------
# 採点はレビューの前提条件ではない — ADR 0007 の中心
# --------------------------------------------------------------------------


@needs_c_compiler
def test_the_console_never_grades(blind_world: World) -> None:
    """コンソールを一巡させても LLM は呼ばれない。

    以前は blind 採点の保存が採点を起動していた。順序が逆だった。
    """
    _instructor_and_submission(blind_world)
    accepted = blind_world.submissions
    del accepted  # 使わない（提出は上で作られている）

    with blind_world.database.unit_of_work() as uow:
        submission = uow.submissions.list_for_learner(
            TENANT, blind_world.register("other", role=Role.LEARNER).user_id
        )
    del submission

    calls_before = len(blind_world.provider.calls)
    blind_world.client.get("/")
    assert len(blind_world.provider.calls) == calls_before, "コンソールが LLM を呼んでいる"


@needs_c_compiler
def test_an_ungraded_submission_cannot_be_reviewed(world: World) -> None:
    """採点が届いていない提出は 409。ここで採点し始めない。"""
    _, accepted = _instructor_and_submission(world)
    response = world.client.get(f"/review/{accepted.submission.id}/reveal")
    assert response.status_code == 409
    assert "aijudge-worker" in response.json()["detail"]


@needs_c_compiler
def test_the_queue_lists_only_review_requests(world: World) -> None:
    """待ち行列は**学習者が再確認を依頼したもの**だけ（ADR 0009）。

    全提出を並べると受講 91 名 × 課題数になり、何から見ればよいか分からない。
    AI の判定は採点直後に学習者へ示しているので、疑いが出たものだけが
    人間の判断を要する。
    """
    _, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty()

    body = world.client.get(f"/courses/{COURSE}/queue").text
    assert str(accepted.submission.id)[:12] not in body, "依頼が無いのに並んでいる"

    _request_review(world, accepted.submission.id)
    body = world.client.get(f"/courses/{COURSE}/queue").text
    assert str(accepted.submission.id)[:12] in body


@needs_c_compiler
def test_a_resolved_request_leaves_the_queue(world: World) -> None:
    _, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty()
    _request_review(world, accepted.submission.id)

    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(accepted.submission.id)
    assert run is not None
    machine = {score.criterion_id: score.level for score in run.criterion_scores}
    world.client.post(
        f"/review/{accepted.submission.id}/finalize", data=_agree_form(world, machine)
    )
    body = world.client.get(f"/courses/{COURSE}/queue").text
    assert str(accepted.submission.id)[:12] not in body


# --------------------------------------------------------------------------
# 権限
# --------------------------------------------------------------------------


def test_an_anonymous_visitor_is_sent_to_the_login_page(world: World) -> None:
    response = world.client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@needs_c_compiler
def test_a_learner_cannot_open_the_queue(world: World) -> None:
    """採点権限が無いコースは「無い」と答える。"""
    world.register("s2400001", role=Role.LEARNER)
    world.login("s2400001")
    assert world.client.get(f"/courses/{COURSE}").status_code == 404


@needs_c_compiler
def test_a_learner_cannot_open_a_submission(world: World) -> None:
    learner = world.register("s2400001", role=Role.LEARNER)
    accepted = world.submit(learner)
    world.worker.run_until_empty()
    world.login("s2400001")
    assert world.client.get(f"/review/{accepted.submission.id}/reveal").status_code == 404


@needs_c_compiler
def test_an_assistant_can_review(world: World) -> None:
    """複数教員での採点分担（Phase 2）の土台。"""
    learner = world.register("s2400001", role=Role.LEARNER)
    world.register("ta", role=Role.ASSISTANT)
    accepted = world.submit(learner)
    world.worker.run_until_empty()
    world.login("ta")
    assert world.client.get(f"/review/{accepted.submission.id}/reveal").status_code == 200


# --------------------------------------------------------------------------
# blind 抽出
# --------------------------------------------------------------------------


def test_sampling_is_deterministic() -> None:
    first = [is_blind_sample(f"sub_{i:032d}", 0.3) for i in range(200)]
    second = [is_blind_sample(f"sub_{i:032d}", 0.3) for i in range(200)]
    assert first == second


def test_sampling_hits_roughly_the_declared_rate() -> None:
    hits = sum(1 for i in range(2000) if is_blind_sample(f"sub_{i:032d}", 0.25))
    assert 0.20 < hits / 2000 < 0.30


def test_a_rate_of_zero_never_asks_for_blind_marking() -> None:
    assert not any(is_blind_sample(f"sub_{i:032d}", 0.0) for i in range(100))


def test_the_rate_comes_from_the_subject_profile(world: World) -> None:
    """設定で宣言する。教員に選ばせない（選択バイアスが入る）。"""
    world.console._rates.clear()
    assert world.console.blind_sample_rate(PROFILE) == 0.05


def test_an_unreadable_profile_falls_back_to_no_sampling(world: World) -> None:
    """測定のためにレビューを止めない。"""
    world.console._rates.clear()
    assert world.console.blind_sample_rate("no-such-profile") == 0.0


@needs_c_compiler
def test_a_submission_outside_the_sample_skips_blind_marking(world: World) -> None:
    _, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty()
    response = world.client.get(f"/review/{accepted.submission.id}/blind", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/reveal")


@needs_c_compiler
def test_a_sampled_submission_must_be_marked_first(blind_world: World) -> None:
    _, accepted = _instructor_and_submission(blind_world)
    blind_world.worker.run_until_empty()
    response = blind_world.client.get(
        f"/review/{accepted.submission.id}/reveal", follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/blind")


# --------------------------------------------------------------------------
# blind 画面 — AI の判定が漏れていないこと
# --------------------------------------------------------------------------


@needs_c_compiler
def test_the_blind_page_contains_no_trace_of_the_ai_verdict(blind_world: World) -> None:
    """採点済みでも、blind 画面には判定を載せない。

    隠すのではなくレスポンスに含めない。
    """
    _, accepted = _instructor_and_submission(blind_world)
    blind_world.worker.run_until_empty()
    body = blind_world.client.get(f"/review/{accepted.submission.id}/blind").text

    for leak in ("AIRATIONALEMARKER", "確信度", "不一致", "ln hl", "最終確定", "L5–5"):
        assert leak not in body, f"blind 画面に {leak!r} が漏れている"

    assert "int b = 0, c = 0, d = 0;" in body
    assert "変数名と構造の分かりやすさ" in body


# --------------------------------------------------------------------------
# blind 採点と確定
# --------------------------------------------------------------------------


@needs_c_compiler
def test_a_blind_mark_is_stored_and_cannot_be_overwritten(blind_world: World) -> None:
    """二度目を受け付けると、AI を見たあとの段階で上書きできてしまう。"""
    _, accepted = _instructor_and_submission(blind_world)
    blind_world.worker.run_until_empty()
    data = {"level_correctness": "3", "level_readability": "1", "notes": "変数名が読めない"}

    first = blind_world.client.post(
        f"/review/{accepted.submission.id}/blind", data=data, follow_redirects=False
    )
    assert first.status_code == 303

    with blind_world.database.unit_of_work() as uow:
        mark = uow.reviews.find_blind_mark(accepted.submission.id)
    assert mark is not None
    assert mark.notes == "変数名が読めない"

    second = blind_world.client.post(f"/review/{accepted.submission.id}/blind", data=data)
    assert second.status_code == 409


@needs_c_compiler
def test_a_missing_criterion_is_rejected(blind_world: World) -> None:
    """欠けたまま確定すると、誰も見ていない観点が成績に入る。"""
    _, accepted = _instructor_and_submission(blind_world)
    blind_world.worker.run_until_empty()
    response = blind_world.client.post(
        f"/review/{accepted.submission.id}/blind", data={"level_correctness": "3"}
    )
    assert response.status_code == 400
    assert "readability" in response.json()["detail"]


@needs_c_compiler
def test_a_level_outside_the_rubric_is_rejected(blind_world: World) -> None:
    _, accepted = _instructor_and_submission(blind_world)
    blind_world.worker.run_until_empty()
    response = blind_world.client.post(
        f"/review/{accepted.submission.id}/blind",
        data={"level_correctness": "3", "level_readability": "9"},
    )
    assert response.status_code == 400


@needs_c_compiler
def test_the_reveal_page_shows_the_disagreement_and_the_evidence(blind_world: World) -> None:
    _, accepted = _instructor_and_submission(blind_world)
    blind_world.worker.run_until_empty()
    blind_world.client.post(
        f"/review/{accepted.submission.id}/blind",
        data={"level_correctness": "3", "level_readability": "1"},
    )
    body = blind_world.client.get(f"/review/{accepted.submission.id}/reveal").text

    assert AI_RATIONALE in body
    assert "不一致" in body
    assert "L5–5" in body
    assert 'class="ln hl"' in body


@needs_c_compiler
def test_finalizing_records_only_what_changed(world: World) -> None:
    """触っていない観点は AI に同意した意味。全部を記録しない。"""
    instructor, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty()

    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(accepted.submission.id)
    assert run is not None
    machine = {score.criterion_id: score.level for score in run.criterion_scores}
    readability = next(c for c in world.task_version.criteria if c.code == "readability")
    correctness = next(c for c in world.task_version.criteria if c.code == "correctness")

    world.client.post(
        f"/review/{accepted.submission.id}/finalize",
        data={
            "level_correctness": str(machine[correctness.id]),
            "level_readability": "0",
            "comment": "変数名が役割を表しておらず、読み手が追えないため下げました。",
        },
        follow_redirects=False,
    )

    with world.database.unit_of_work() as uow:
        review = uow.reviews.find_review_for_run(run.id)
    assert review is not None
    assert set(review.adjusted_levels) == {readability.id}
    assert not review.agreed
    assert review.grader_id == instructor.user_id


@needs_c_compiler
def test_agreeing_leaves_no_adjustment(world: World) -> None:
    _, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty()
    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(accepted.submission.id)
    assert run is not None
    machine = {score.criterion_id: score.level for score in run.criterion_scores}

    world.client.post(
        f"/review/{accepted.submission.id}/finalize",
        data=_agree_form(world, machine),
        follow_redirects=False,
    )
    with world.database.unit_of_work() as uow:
        review = uow.reviews.find_review_for_run(run.id)
    assert review is not None
    assert review.agreed


@needs_c_compiler
def test_finalizing_twice_is_refused(world: World) -> None:
    """二度確定できると成績が二つ存在する。やり直しは再採点から。"""
    _, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty()
    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(accepted.submission.id)
    assert run is not None
    machine = {score.criterion_id: score.level for score in run.criterion_scores}
    data = _agree_form(world, machine)
    world.client.post(f"/review/{accepted.submission.id}/finalize", data=data)
    again = world.client.post(f"/review/{accepted.submission.id}/finalize", data=data)
    assert again.status_code == 409


@needs_c_compiler
def test_the_grading_run_is_never_rewritten(world: World) -> None:
    """教員の修正は追記であって上書きではない（P8）。"""
    _, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty()
    with world.database.unit_of_work() as uow:
        before = uow.runs.latest_for(accepted.submission.id)
    assert before is not None

    machine = {score.criterion_id: score.level for score in before.criterion_scores}
    correctness = next(c for c in world.task_version.criteria if c.code == "correctness")
    world.client.post(
        f"/review/{accepted.submission.id}/finalize",
        data={
            "level_correctness": str(machine[correctness.id]),
            "level_readability": "0",
            "comment": JUSTIFICATION,
        },
    )
    with world.database.unit_of_work() as uow:
        after = uow.runs.latest_for(accepted.submission.id)
    assert after == before


# --------------------------------------------------------------------------
# 観測（測定用の記録）
# --------------------------------------------------------------------------


@needs_c_compiler
def test_a_blind_mark_becomes_a_usable_observation(blind_world: World) -> None:
    """記録は Phase 0。計算は Phase 1（ADR 0007）。"""
    _, accepted = _instructor_and_submission(blind_world)
    blind_world.worker.run_until_empty()
    blind_world.client.post(
        f"/review/{accepted.submission.id}/blind",
        data={"level_correctness": "3", "level_readability": "1"},
    )

    stored = blind_world.observations.load(
        PROFILE, str(blind_world.task_version.task_id), str(accepted.submission.id)
    )
    readability = next(item for item in stored if item.criterion_code == "readability")
    assert readability.human_level == 1
    assert readability.machine_level == 3
    assert readability.blind is True
    assert readability.usable_for_agreement


@needs_c_compiler
def test_accepting_the_ai_is_not_recorded_as_a_correction(blind_world: World) -> None:
    """教員が blind の判断を翻して AI に合わせても、それは「訂正」ではない。

    `machine_corrected` が測るのは「機械の段階を人間が上書きしたか」で、
    見逃し率（自動確定したのに実は直すべきだった割合）の材料になる。
    教員が AI に合わせた提出は、見逃しではない。

    blind 採点から最終段階への移動（アンカリングの度合い）は別の量で、
    現在の合格基準には入っていないので記録していない。
    """
    _, accepted = _instructor_and_submission(blind_world)
    blind_world.worker.run_until_empty()
    blind_world.client.post(
        f"/review/{accepted.submission.id}/blind",
        data={"level_correctness": "3", "level_readability": "1"},
    )
    blind_world.client.post(
        f"/review/{accepted.submission.id}/finalize",
        data={
            "level_correctness": "3",
            "level_readability": "3",
            "comment": "AI の指摘で考えを変えた",
        },
    )

    stored = blind_world.observations.load(
        PROFILE, str(blind_world.task_version.task_id), str(accepted.submission.id)
    )
    readability = next(item for item in stored if item.criterion_code == "readability")
    # blind の側は上書きされない（ADR 0005）。κ はこちらで測る。
    assert readability.human_level == 1
    assert readability.machine_corrected is False


@needs_c_compiler
def test_overriding_the_ai_is_recorded_as_a_correction(blind_world: World) -> None:
    """見逃し率の材料。教員が機械の段階を上書きしたか。"""
    _, accepted = _instructor_and_submission(blind_world)
    blind_world.worker.run_until_empty()
    blind_world.client.post(
        f"/review/{accepted.submission.id}/blind",
        data={"level_correctness": "3", "level_readability": "1"},
    )
    blind_world.client.post(
        f"/review/{accepted.submission.id}/finalize",
        data={
            "level_correctness": "3",
            "level_readability": "1",
            "comment": "AI の判定は甘いと考えます。変数名が役割を表していません。",
        },
    )

    stored = blind_world.observations.load(
        PROFILE, str(blind_world.task_version.task_id), str(accepted.submission.id)
    )
    readability = next(item for item in stored if item.criterion_code == "readability")
    assert readability.machine_corrected is True


@needs_c_compiler
def test_a_broken_observation_store_does_not_block_the_review(world: World) -> None:
    """測定の都合でレビューを落とさない（ADR 0007）。"""

    def explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk on fire")

    world.observations.save = explode  # type: ignore[method-assign]
    _, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty()
    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(accepted.submission.id)
    assert run is not None
    machine = {score.criterion_id: score.level for score in run.criterion_scores}

    response = world.client.post(
        f"/review/{accepted.submission.id}/finalize",
        data=_agree_form(world, machine),
        follow_redirects=False,
    )
    assert response.status_code == 303
    with world.database.unit_of_work() as uow:
        assert uow.reviews.find_review_for_run(run.id) is not None


# --------------------------------------------------------------------------
# フォームそのもの — 画面を見て初めて分かった不具合
# --------------------------------------------------------------------------


@needs_c_compiler
def test_each_criterion_has_its_own_radio_group(blind_world: World) -> None:
    """段階のラジオは観点ごとに独立していること。

    name を共有すると、ブラウザは全観点を 1 つのグループとして扱い、
    **観点をまたいで 1 つしか選べない**。実際にそうなっていた。
    テストがフォームを経由せず直接 POST していたので、画面を見るまで
    分からなかった。
    """
    _, accepted = _instructor_and_submission(blind_world)
    blind_world.worker.run_until_empty()

    for path in ("blind", "reveal"):
        if path == "reveal":
            blind_world.client.post(
                f"/review/{accepted.submission.id}/blind",
                data={"level_correctness": "3", "level_readability": "1"},
            )
        body = blind_world.client.get(f"/review/{accepted.submission.id}/{path}").text
        names = set(re.findall(r'<input type="radio" name="([^"]+)"', body))
        assert len(names) >= 2, f"{path}: ラジオ群が {names} しかない（観点ごとに要る）"
        assert "levels" not in names, f"{path}: 共有の name が残っている"


@needs_c_compiler
def test_the_finalize_form_round_trips_as_a_browser_sends_it(world: World) -> None:
    """画面のフォームをそのまま送って確定できること。

    フォームから項目名と既定値を読み取り、ブラウザと同じ形で送る。
    """
    _, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty()
    body = world.client.get(f"/review/{accepted.submission.id}/reveal").text

    # 「選択済み」になっている項目を拾う（ブラウザが送るのはこれ）。
    checked = dict(
        re.findall(
            r'<input type="radio" name="(level_[^"]+)" required\s+value="(\d+)"\s+checked',
            body,
        )
    )
    assert len(checked) == 2, f"既定選択が {checked}（観点ごとに 1 つ要る）"

    response = world.client.post(
        f"/review/{accepted.submission.id}/finalize",
        data=checked | {"comment": JUSTIFICATION},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text


@needs_c_compiler
def test_the_system_verdict_is_labelled_on_every_option(world: World) -> None:
    """段階を動かしたあとでも「システムは何と言ったか」が分かること。

    分からないと、確定の判断が記憶に頼ることになる。
    """
    _, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty()
    body = world.client.get(f"/review/{accepted.submission.id}/reveal").text
    assert "システム判定" in body

    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(accepted.submission.id)
    assert run is not None
    # 判定が付いた観点の数だけ印が出ていること。
    scored = [s for s in run.criterion_scores]
    assert body.count("システム判定") == len(scored), (
        f"印が {body.count('システム判定')} 個 / 判定は {len(scored)} 件"
    )


@needs_c_compiler
def test_the_blind_mark_is_labelled_after_reveal(blind_world: World) -> None:
    """自分が blind で何を付けたかも、選択肢の上で分かること。"""
    _, accepted = _instructor_and_submission(blind_world)
    blind_world.worker.run_until_empty()
    blind_world.client.post(
        f"/review/{accepted.submission.id}/blind",
        data={"level_correctness": "3", "level_readability": "1"},
    )
    body = blind_world.client.get(f"/review/{accepted.submission.id}/reveal").text
    assert "あなたの blind 採点" in body


@needs_c_compiler
def test_an_already_finalized_run_can_still_be_reviewed(world: World) -> None:
    """自動確定した成績を教員が読んで確定できること。

    確定の記録は 1 採点につき 1 つなので、素直に書くと二度目で 409 になる。
    確定は最初のものを残し、教員が読んだ事実は `HumanReview` の側に付ける
    （ADR 0010、追記のみ P8）。この経路は実在する ── 自動確定した成績に
    学習者が異議を申し立て、教員が読む。
    """
    from datetime import UTC, datetime

    from aijudge_core import Finalization, FinalizationSource, new_id
    from aijudge_core.ids import FinalizationId

    _, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty()

    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(accepted.submission.id)
        assert run is not None
        uow.reviews.save_finalization(
            Finalization(
                id=FinalizationId(new_id("fin")),
                grading_run_id=run.id,
                source=FinalizationSource.DEADLINE_ELAPSED,
                justification="締切から所定の時間が経過したため自動確定しました。",
                finalized_at=datetime.now(UTC),
            )
        )
        uow.commit()

    machine = {score.criterion_id: score.level for score in run.criterion_scores}
    response = world.client.post(
        f"/review/{accepted.submission.id}/finalize",
        data=_agree_form(world, machine),
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text

    with world.database.unit_of_work() as uow:
        review = uow.reviews.find_review_for_run(run.id)
        finalization = uow.reviews.find_finalization_for_run(run.id)
    assert review is not None, "教員が読んだ記録が残っていない"
    # 確定の記録は最初のものを残す。上書きしない。
    assert finalization is not None
    assert finalization.source is FinalizationSource.DEADLINE_ELAPSED


# --------------------------------------------------------------------------
# 提出の一覧と絞り込み
# --------------------------------------------------------------------------


@needs_c_compiler
def test_the_submissions_page_lists_every_submission(world: World) -> None:
    """待ち行列と違い、**依頼の有無に関係なく全部出す**（submissions.py）。"""
    _, accepted = _instructor_and_submission(world)
    world.worker.run_until_empty()

    body = world.client.get(f"/courses/{COURSE}/submissions").text
    assert str(accepted.submission.id)[:12] in body or "確認する" in body
    assert "得点の分布" in body


@needs_c_compiler
def test_the_submissions_page_filters_by_learner_prefix(world: World) -> None:
    """受講 91 名の学籍番号を選択肢に並べても選べない。前方一致で絞る。"""
    _instructor_and_submission(world)
    world.worker.run_until_empty()

    hit = world.client.get(f"/courses/{COURSE}/submissions?learner=s24").text
    miss = world.client.get(f"/courses/{COURSE}/submissions?learner=zzz").text
    assert "条件に合う提出がありません" not in hit
    assert "条件に合う提出がありません" in miss


@needs_c_compiler
def test_the_submissions_page_can_show_only_the_adopted_ones(world: World) -> None:
    """採用提出だけに絞れること。全提出の分布は到達度として読めない。"""
    _instructor_and_submission(world)
    world.worker.run_until_empty()

    body = world.client.get(f"/courses/{COURSE}/submissions?adopted=1").text
    assert "採用" in body


def test_an_unknown_task_filter_shows_nothing(world: World) -> None:
    """絞り込みは URL に載る。壊れた値でも 500 にしない。"""
    world.register("instructor", role=Role.INSTRUCTOR)
    world.login("instructor")
    response = world.client.get(f"/courses/{COURSE}/submissions?task=tsk_x")
    assert response.status_code == 200
    assert "条件に合う提出がありません" in response.text


@needs_c_compiler
def test_choosing_a_set_narrows_the_task_choices(world: World) -> None:
    """選んだセットに無い問題を選べると、結果が常に空になる。

    絞り込みが壊れたように見えるので、選択肢の方を絞る。
    """
    _instructor_and_submission(world)
    world.worker.run_until_empty()
    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(COURSE)[0]
    unit = task.unit or "_"

    narrowed = world.client.get(f"/courses/{COURSE}/submissions?unit={unit}").text
    other = world.client.get(f"/courses/{COURSE}/submissions?unit=nosuchunit").text
    assert str(task.id) in narrowed
    assert str(task.id) not in other
    # 問題セットの選択肢そのものは絞らない（別のセットに移れなくなる）。
    assert unit in other


@needs_c_compiler
def test_a_task_outside_the_chosen_set_is_dropped(world: World) -> None:
    """URL に残った古い問題の指定で、結果が黙って空にならないこと。"""
    _instructor_and_submission(world)
    world.worker.run_until_empty()
    with world.database.unit_of_work() as uow:
        task = uow.tasks.list_for_course(COURSE)[0]

    body = world.client.get(f"/courses/{COURSE}/submissions?unit=nosuchunit&task={task.id}").text
    assert "条件に合う提出がありません" in body


def test_the_choice_filters_apply_without_a_button(world: World) -> None:
    """選ぶ欄は見比べながら動かすもの。1 回ごとに押させると手が止まる。"""
    world.register("instructor", role=Role.INSTRUCTOR)
    world.login("instructor")
    body = world.client.get(f"/courses/{COURSE}/submissions").text
    assert "requestSubmit()" in body


def test_the_learner_box_is_not_submitted_per_keystroke(world: World) -> None:
    """打つたびに送ると、読み直しの往復で打った字が捨てられる。

    焦点を戻しても同じで、1 文字ごとに手が止まる。この欄だけは確定して
    から送る（Enter か絞り込むボタン）。
    """
    world.register("instructor", role=Role.INSTRUCTOR)
    world.login("instructor")
    body = world.client.get(f"/courses/{COURSE}/submissions").text
    assert "setTimeout" not in body, "打つたびに送っている"
    # 確定は Enter か欄を離れたとき（`change`）。送信ボタンは置かない。
    assert "onchange=" in body
    assert body.count(">絞り込む<") == 1, "JavaScript 無しのための 1 つだけ"


def test_the_learner_box_keeps_the_focus(world: World) -> None:
    """読み直すたびに入力欄は作り直される。焦点を戻さないと続きが打てない。"""
    world.register("instructor", role=Role.INSTRUCTOR)
    world.login("instructor")
    body = world.client.get(f"/courses/{COURSE}/submissions?learner=y23").text
    assert "autofocus" in body
    assert "setSelectionRange" in body
    # 何も入っていないときは焦点を奪わない。
    assert "autofocus" not in world.client.get(f"/courses/{COURSE}/submissions").text
