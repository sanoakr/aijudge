"""学習者向け画面の規則を固定する。

固定したいのは 2 つ。

見せない  確定前の AI 判定と、他人の提出。前者は設計原則 P5、後者は
          そもそも見えてはいけないもの。
見せる    テスト実行の結果はすぐ。それが Sharif Judge から引き継ぐ価値の中心。
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aijudge_authoring.importers import sharif_judge
from aijudge_core import (
    Course,
    Finalization,
    FinalizationSource,
    HumanReview,
    Role,
    Task,
)
from aijudge_core.ids import (
    CourseId,
    FinalizationId,
    HumanReviewId,
    TenantId,
    UserId,
    new_id,
)
from aijudge_eval_rubric_ai_judge import RubricAiJudge
from aijudge_grader import GradingWorker
from aijudge_grading import EvaluatorRegistry
from aijudge_identity import AuthService
from aijudge_llm_gateway import LlmGateway, ScriptedProvider
from aijudge_persistence import Database
from aijudge_studentweb import SESSION_COOKIE, StudentApp, create_app
from aijudge_submission import FilesystemArtifactStore

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_TASK = REPO_ROOT / "evals" / "golden" / "cs_intro_c" / "example-task" / "task"
EXAMPLE_SOURCE = REPO_ROOT / "evals" / "golden" / "cs_intro_c" / "example-task" / "marks" / "s001.c"
PROFILES = REPO_ROOT / "subjects"

TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
PASSWORD = "correct horse battery"
AUTHOR = UserId("usr_" + "a" * 32)
PROFILE_SAMPLES = 3

needs_c_compiler = pytest.mark.skipif(
    shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="no C compiler available",
)

AI_RATIONALE = "AIRATIONALEMARKER 変数名から役割が読み取れません。"
AI_SAYS_1 = (
    '{"level": 1, "evidence": [{"start_line": 5, "end_line": 5, '
    f'"quote": "int b = 0, c = 0, d = 0;"}}], "rationale": "{AI_RATIONALE}"}}'
)


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
        self.store = FilesystemArtifactStore(tmp_path / "artifacts")
        self.provider = ScriptedProvider([AI_SAYS_1] * PROFILE_SAMPLES * 4)
        registry = EvaluatorRegistry().load_installed()
        registry.replace(RubricAiJudge(LlmGateway(self.provider), model="stub"))
        self.worker = GradingWorker(
            self.database, self.store, profiles_dir=PROFILES, registry=registry
        )
        self.app = StudentApp(self.database, self.store, profiles_dir=PROFILES)
        self.client = TestClient(create_app(self.app))

        # 課題とコースを用意する。
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
            uow.commit()
        self.task_version = sharif_judge.import_problem(
            EXAMPLE_TASK,
            subject_profile="cs_intro_c",
            authored_by=AUTHOR,
            readability_weight=0.3,
        )
        with self.database.unit_of_work() as uow:
            uow.tasks.save_task(Task(id=self.task_version.task_id, course_id=COURSE, title="例題"))
            uow.tasks.save_version(self.task_version)
            uow.commit()

    def register(self, login: str, *, role: Role = Role.LEARNER):
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

    def submit(self, payload: bytes | None = None):
        return self.client.post(
            f"/tasks/{self.task_version.id}/submit",
            files={"upload": ("main.c", payload or EXAMPLE_SOURCE.read_bytes(), "text/plain")},
            follow_redirects=False,
        )

    def finalize(
        self,
        submission_id: str,
        *,
        grader=None,
        source: FinalizationSource = FinalizationSource.INSTRUCTOR_REVIEW,
        comment: str = "テスト実行の結果を確認しました。判定は妥当です。",
    ) -> None:
        """成績を確定させる（レビューコンソールと自動確定の代わり）。

        確定は `Finalization`。教員が 1 件を読んだ場合はそれに加えて
        `HumanReview` が生まれる（ADR 0010）。ここで両方を書くのは、
        コンソールの `finalize` がそうしているから。
        """
        with self.database.unit_of_work() as uow:
            run = uow.runs.latest_for(submission_id)
            assert run is not None
            review_id = None
            if source is FinalizationSource.INSTRUCTOR_REVIEW:
                assert grader is not None
                review_id = HumanReviewId(new_id("hrv"))
                uow.reviews.save_review(
                    HumanReview(
                        id=review_id,
                        grading_run_id=run.id,
                        grader_id=grader.user_id,
                        comment=comment,
                        reviewed_at=datetime.now(UTC),
                    )
                )
            uow.reviews.save_finalization(
                Finalization(
                    id=FinalizationId(new_id("fin")),
                    grading_run_id=run.id,
                    source=source,
                    actor_id=None if grader is None else grader.user_id,
                    review_id=review_id,
                    justification=comment,
                    finalized_at=datetime.now(UTC),
                )
            )
            uow.commit()

    def close(self) -> None:
        self.database.dispose()


@pytest.fixture
def world(tmp_path: Path):
    instance = World(tmp_path)
    yield instance
    instance.close()


# --------------------------------------------------------------------------
# 認証
# --------------------------------------------------------------------------


def test_an_anonymous_visitor_is_sent_to_the_login_page(world: World) -> None:
    response = world.client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_a_wrong_password_does_not_set_a_session(world: World) -> None:
    world.register("s2400001")
    response = world.client.post("/login", data={"login": "s2400001", "password": "wrong"})
    assert response.status_code == 401
    assert SESSION_COOKIE not in response.cookies


def test_the_session_cookie_is_not_readable_by_scripts(world: World) -> None:
    """XSS が起きてもセッションを盗まれないようにする。"""
    world.register("s2400001")
    response = world.client.post(
        "/login", data={"login": "s2400001", "password": PASSWORD}, follow_redirects=False
    )
    header = response.headers["set-cookie"]
    assert "HttpOnly" in header
    assert "SameSite=lax" in header or "samesite=lax" in header.lower()


def test_logging_out_clears_the_session(world: World) -> None:
    world.register("s2400001")
    world.login("s2400001")
    world.client.post("/logout", follow_redirects=False)
    world.client.cookies.clear()
    assert world.client.get("/", follow_redirects=False).status_code == 303


def test_a_protected_page_needs_a_session(world: World) -> None:
    assert world.client.get(f"/tasks/{world.task_version.id}").status_code == 401


# --------------------------------------------------------------------------
# 見えてはいけないもの
# --------------------------------------------------------------------------


def test_a_course_you_are_not_enrolled_in_looks_like_it_does_not_exist(world: World) -> None:
    """存在と権限を区別しない。区別するとコースを列挙できる。"""
    with world.database.unit_of_work() as uow:
        uow.identity.save_course(
            Course(
                id=CourseId("crs_" + "9" * 32),
                tenant_id=TENANT,
                code="secret",
                title="別のコース",
                term="2026-前期",
                subject_profile="cs_intro_c",
            )
        )
        uow.commit()
    world.register("s2400001")
    world.login("s2400001")
    response = world.client.get(f"/courses/{'crs_' + '9' * 32}")
    assert response.status_code == 404


@needs_c_compiler
def test_another_learners_submission_is_not_visible(world: World) -> None:
    """URL を推測されても他人の提出は見えない。"""
    world.register("owner")
    world.register("stranger")
    world.login("owner")
    response = world.submit()
    submission_url = response.headers["location"]

    world.client.cookies.clear()
    world.login("stranger")
    assert world.client.get(submission_url).status_code == 404


def test_a_learner_only_sees_their_own_courses(world: World) -> None:
    world.register("s2400001")
    world.login("s2400001")
    body = world.client.get("/").text
    assert "プログラミング演習 II" in body


# --------------------------------------------------------------------------
# 提出
# --------------------------------------------------------------------------


@needs_c_compiler
def test_submitting_queues_the_work_and_shows_a_pending_result(world: World) -> None:
    """提出したら採点が始まる。結果はあとから届く（設計方針 §10）。"""
    world.register("s2400001")
    world.login("s2400001")
    response = world.submit()
    assert response.status_code == 303

    body = world.client.get(response.headers["location"]).text
    assert "採点を待っています" in body

    with world.database.unit_of_work() as uow:
        assert uow.jobs.pending_count() == 1


@needs_c_compiler
def test_submitting_the_same_file_twice_does_not_create_a_second_attempt(world: World) -> None:
    """二度押しで提出が増えない。"""
    world.register("s2400001")
    world.login("s2400001")
    world.submit()
    second = world.submit()
    assert "again=1" in second.headers["location"]

    body = world.client.get(second.headers["location"]).text
    assert "すでに提出されています" in body


def test_an_unsupported_file_type_is_refused(world: World) -> None:
    world.register("s2400001")
    world.login("s2400001")
    response = world.client.post(
        f"/tasks/{world.task_version.id}/submit",
        files={"upload": ("notes.docx", b"binary", "application/octet-stream")},
    )
    assert response.status_code == 400
    assert "提出できません" in response.json()["detail"]


def test_an_empty_file_is_refused(world: World) -> None:
    world.register("s2400001")
    world.login("s2400001")
    response = world.client.post(
        f"/tasks/{world.task_version.id}/submit",
        files={"upload": ("main.c", b"", "text/plain")},
    )
    assert response.status_code == 400


def test_an_oversized_file_is_refused(world: World) -> None:
    """学生のコードに 1MB を超えるものは無い。超えるなら事故か攻撃。"""
    world.app.max_upload_bytes = 128
    world.register("s2400001")
    world.login("s2400001")
    response = world.client.post(
        f"/tasks/{world.task_version.id}/submit",
        files={"upload": ("main.c", b"x" * 1024, "text/plain")},
    )
    assert response.status_code == 413


# --------------------------------------------------------------------------
# 結果の見せ方 — 設計原則 P5
# --------------------------------------------------------------------------


@needs_c_compiler
def test_the_ai_verdict_is_shown_immediately(world: World) -> None:
    """AI の判定は採点直後に見せる（ADR 0009）。

    以前は教員の確認まで伏せていた。だが設計原則 P5 が要求するのは
    **最終権限が教員にあること**であって、途中経過を伏せることではない。
    伏せると、速く返せるという AI 採点の価値が教員の作業速度で消える。
    """
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()

    body = world.client.get(location).text
    assert AI_RATIONALE in body, "AI の判定が出ていない"
    assert "確認中" not in body
    assert 'class="score"' in body, "総合点が出ていない"
    assert "担当教員の確認を経て確定します" in body


@needs_c_compiler
def test_a_confirmed_grade_is_distinguished_from_a_provisional_one(world: World) -> None:
    """区別しないと、AI の判定と教員の判定が同じ重みに見える。"""
    world.register("s2400001")
    instructor = world.register("instructor", role=Role.INSTRUCTOR)
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()

    assert "担当教員の確認を経て確定します" in world.client.get(location).text

    submission_id = location.split("/")[-1].split("?")[0]
    world.finalize(submission_id, grader=instructor)
    body = world.client.get(location).text
    assert "担当教員が確認した成績です" in body


@needs_c_compiler
def test_which_criteria_the_ai_judged_is_visible(world: World) -> None:
    """テスト実行の結果と AI の判定を学習者が区別できること。"""
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()

    body = world.client.get(location).text
    assert ">AI<" in body
    assert ">テスト実行<" in body


# --------------------------------------------------------------------------
# 再確認の依頼 — 異議申し立ての導線（設計方針 §9.4）
# --------------------------------------------------------------------------


@needs_c_compiler
def test_a_learner_can_request_a_review(world: World) -> None:
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()
    submission_id = location.split("/")[-1].split("?")[0]

    response = world.client.post(
        f"/submissions/{submission_id}/request-review",
        data={"reason": "テストケース 3 の想定出力が仕様と違うと思います。"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    body = world.client.get(location).text
    assert "再確認を依頼済みです" in body


@needs_c_compiler
def test_a_request_without_a_reason_is_refused(world: World) -> None:
    """「納得できない」だけの依頼を受け付けると、教員は何を確認すべきか
    分からないまま全件を見ることになり、導線が機能しなくなる。"""
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()
    submission_id = location.split("/")[-1].split("?")[0]

    for reason in ("", "違う", "   "):
        response = world.client.post(
            f"/submissions/{submission_id}/request-review", data={"reason": reason}
        )
        assert response.status_code == 400, reason


@needs_c_compiler
def test_a_second_request_is_refused(world: World) -> None:
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()
    submission_id = location.split("/")[-1].split("?")[0]
    data = {"reason": "テストケース 3 の想定出力が仕様と違うと思います。"}

    world.client.post(f"/submissions/{submission_id}/request-review", data=data)
    assert (
        world.client.post(f"/submissions/{submission_id}/request-review", data=data).status_code
        == 409
    )


@needs_c_compiler
def test_a_confirmed_grade_cannot_be_requested(world: World) -> None:
    """確定済みの成績に依頼を出す導線は無い。やり直しは再採点から。"""
    world.register("s2400001")
    instructor = world.register("instructor", role=Role.INSTRUCTOR)
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()
    submission_id = location.split("/")[-1].split("?")[0]
    world.finalize(submission_id, grader=instructor)

    response = world.client.post(
        f"/submissions/{submission_id}/request-review",
        data={"reason": "やはり納得できないので再度お願いします。"},
    )
    assert response.status_code == 409


@needs_c_compiler
def test_the_instructors_justification_is_shown_to_the_learner(world: World) -> None:
    """教員の根拠が学習者に返ること。返らないなら書かせる意味がない。"""
    world.register("s2400001")
    instructor = world.register("instructor", role=Role.INSTRUCTOR)
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()
    submission_id = location.split("/")[-1].split("?")[0]

    world.finalize(
        submission_id,
        grader=instructor,
        comment="テストケース 3 を確認しました。仕様どおりで判定は妥当です。",
    )

    body = world.client.get(location).text
    assert "テストケース 3 を確認しました" in body


@needs_c_compiler
def test_an_instructor_adjustment_changes_the_score_the_learner_sees(world: World) -> None:
    """教員が段階を変えたら総合点もそれに従う。

    `GradingRun.score_ratio` は AI の判定に基づく値なので、そのまま
    見せると教員の修正が反映されない。
    """
    world.register("s2400001")
    instructor = world.register("instructor", role=Role.INSTRUCTOR)
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()
    submission_id = location.split("/")[-1].split("?")[0]

    readability = next(c for c in world.task_version.criteria if c.code == "readability")
    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(submission_id)
        assert run is not None
        machine_score = run.score_ratio
        uow.reviews.save_review(
            HumanReview(
                id=HumanReviewId(new_id("hrv")),
                grading_run_id=run.id,
                grader_id=instructor.user_id,
                adjusted_levels={readability.id: 3},
                comment="読みやすさは十分だと判断しました。命名は一貫しています。",
                reviewed_at=datetime.now(UTC),
            )
        )
        uow.commit()

    body = world.client.get(location).text
    assert "教員が調整" in body
    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(submission_id)
    assert run is not None
    assert run.score_ratio == machine_score, "GradingRun が書き換えられている（P8 違反）"


@needs_c_compiler
def test_the_feedback_appears_without_waiting_for_confirmation(world: World) -> None:
    """フィードバックは学習者に返す価値の本体。確定を待たせない。"""
    from aijudge_feedback import FeedbackResult

    class Fixed:
        def generate(self, *_args: object, **_kwargs: object) -> FeedbackResult:
            return FeedbackResult(message="n <= 0 の場合を確かめてください。")

    world.worker._feedback = Fixed()
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()

    body = world.client.get(location).text
    assert "次の一手" in body


@needs_c_compiler
def test_the_statement_is_rendered_not_shown_as_raw_markdown(world: World) -> None:
    """課題文は Markdown。生のまま出すと `##` や ``` が見える。

    実測（2026-08-28）で学生 UI に生の Markdown が出ていた。プログラミング
    課題の課題文はコードブロックが本体なので、これは読めない。
    """
    world.register("s2400001")
    world.login("s2400001")
    body = world.client.get(f"/tasks/{world.task_version.id}").text

    statement = body.split('class="statement"')[1].split("</div>")[0]
    assert "<h2>" in statement or "<h3>" in statement, "見出しが描画されていない"
    assert "## " not in statement, "生の Markdown が出ている"


def test_html_in_a_statement_is_not_executed(world: World) -> None:
    """AI 作問（Phase 4）が入れば課題文はモデルの出力になる。

    そのとき `<script>` が通る経路があってはならないので、最初から塞ぐ。
    """
    from aijudge_authoring import render_statement

    rendered = render_statement("<script>alert(1)</script>\n\n## 見出し\n")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "<h2>見出し</h2>" in rendered


def test_the_session_cookie_becomes_secure_behind_a_tls_proxy(world: World) -> None:
    """`tailscale serve` や nginx が前に居る配置で `Secure` が付くこと。

    付かないと、TLS で配置しても平文の経路が残る。
    """
    world.register("s2400001")
    response = world.client.post(
        "/login",
        data={"login": "s2400001", "password": PASSWORD},
        headers={"X-Forwarded-Proto": "https"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Secure" in response.headers["set-cookie"]


def test_the_session_cookie_is_not_secure_on_plain_localhost(world: World) -> None:
    """真にすると localhost の平文アクセスでログインできない。"""
    world.register("s2400001")
    response = world.client.post(
        "/login",
        data={"login": "s2400001", "password": PASSWORD},
        follow_redirects=False,
    )
    assert "Secure" not in response.headers["set-cookie"]


@needs_c_compiler
def test_the_verdict_column_does_not_wrap(world: World) -> None:
    """評価のピルが縦に積まれないこと。

    `白space:nowrap` が無いと、狭い列で「採点できませんでした」が
    1 文字ずつ縦に並ぶ（実際にそうなった）。表がその分の幅を確保するよう、
    列に `fit` を付けて幅を内容に合わせる。
    """
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()
    body = world.client.get(location).text

    assert "white-space:nowrap" in body, "ピルの折り返しを止めていない"
    # 評価列が内容幅、説明列が残りを取る指定になっていること。
    assert 'class="fit">評価' in body
    assert 'class="grow">説明' in body


# --------------------------------------------------------------------------
# 確定の出所（ADR 0010）
# --------------------------------------------------------------------------


@needs_c_compiler
def test_an_automatically_finalized_grade_does_not_claim_a_human_read_it(world: World) -> None:
    """**締切後に機械が確定した成績を「教員が確認した」と出さない。**

    出せば学習者に嘘をつくことになる。誰も読んでいないという事実が、
    異議を申し立てるかどうかの判断を変える。
    """
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()
    submission_id = location.split("/")[-1].split("?")[0]

    world.finalize(
        submission_id,
        source=FinalizationSource.DEADLINE_ELAPSED,
        comment="締切から所定の時間が経過したため、AI の判定のまま確定しました。",
    )

    body = world.client.get(location).text
    assert "担当教員が確認した成績です" not in body
    assert "個別の確認は経ていません" in body
    assert "担当教員の確認を経て確定します" not in body, "確定したことは伝える"


@needs_c_compiler
def test_a_bulk_finalized_grade_says_it_was_not_read_individually(world: World) -> None:
    world.register("s2400001")
    instructor = world.register("instructor", role=Role.INSTRUCTOR)
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()
    submission_id = location.split("/")[-1].split("?")[0]

    world.finalize(
        submission_id,
        grader=instructor,
        source=FinalizationSource.INSTRUCTOR_BULK,
        comment="テスト全通の提出について、抽出して確認の上まとめて確定しました。",
    )

    body = world.client.get(location).text
    assert "個別の確認は経ていません" in body
    assert "担当教員が確認した成績です" not in body


@needs_c_compiler
def test_a_finalized_grade_can_no_longer_be_contested(world: World) -> None:
    """確定したら依頼は締め切る。

    締切と同時に「MM/DD HH:MM に確定します」と告げ、n 時間の窓を与えて
    いる（ADR 0010）。締め切らないと、教員の待ち行列は学期末まで新しい
    依頼を受け続ける。確定後の申し出は画面の外（担当教員）に回す。
    """
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()
    submission_id = location.split("/")[-1].split("?")[0]
    world.finalize(submission_id, source=FinalizationSource.DEADLINE_ELAPSED)

    response = world.client.post(
        f"/submissions/{submission_id}/request-review",
        data={"reason": "自動で確定しましたが、テストケース 3 の想定出力が違うと思います。"},
    )
    assert response.status_code == 409
    assert "担当教員に直接" in world.client.get(location).text


@needs_c_compiler
def test_an_instructor_can_still_settle_an_automatically_finalized_grade(world: World) -> None:
    """自動確定 → 学習者が異議 → 教員が読む、の経路が通ること。

    ここで二重確定になって落ちていた。確定の記録は最初のもの（自動確定）が
    残り、教員が読んだ事実は `HumanReview` の側に付く（追記のみ、P8）。
    学習者に出る顔は教員が読んだ方になる。
    """
    world.register("s2400001")
    instructor = world.register("instructor", role=Role.INSTRUCTOR)
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()
    submission_id = location.split("/")[-1].split("?")[0]

    world.finalize(submission_id, source=FinalizationSource.DEADLINE_ELAPSED)
    world.client.post(
        f"/submissions/{submission_id}/request-review",
        data={"reason": "自動で確定しましたが、テストケース 3 の想定出力が違うと思います。"},
    )

    # 教員が読んで確定する（コンソールと同じ操作）。
    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(submission_id)
        uow.reviews.save_review(
            HumanReview(
                id=HumanReviewId(new_id("hrv")),
                grading_run_id=run.id,
                grader_id=instructor.user_id,
                comment="ご指摘の入力例を確認しました。仕様どおりで判定は妥当です。",
                reviewed_at=datetime.now(UTC),
            )
        )
        uow.commit()

    body = world.client.get(location).text
    assert "担当教員が確認した成績です" in body
    assert "個別の確認は経ていません" not in body
    assert "ご指摘の入力例を確認しました" in body, "教員の言葉が自動確定の定型文に負けている"


# --------------------------------------------------------------------------
# 仮確定の窓（ADR 0010）
# --------------------------------------------------------------------------


def _schedule(world: World, *, due_offset_hours: float, grace: float | None) -> None:
    """締切と猶予を入れる。締切は「いまから何時間後か」で指定する。"""
    from datetime import timedelta

    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(world.task_version.task_id)
        uow.tasks.save_task(
            task.model_copy(
                update={"due_at": datetime.now(UTC) + timedelta(hours=due_offset_hours)}
            )
        )
        course = uow.identity.get_course(COURSE)
        uow.identity.save_course(course.model_copy(update={"auto_finalize_after_hours": grace}))
        uow.commit()


@needs_c_compiler
def test_before_the_deadline_the_confirmation_time_is_not_announced_as_provisional(
    world: World,
) -> None:
    """締切前は仮確定ではない。まだ提出をやり直せる段階である。"""
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()
    _schedule(world, due_offset_hours=24, grace=24.0)

    body = world.client.get(location).text
    assert "仮確定です" not in body
    assert "担当教員の確認を経て確定します" in body


@needs_c_compiler
def test_after_the_deadline_the_grade_is_provisional_and_says_when_it_settles(
    world: World,
) -> None:
    """**いつ確定するかを示す。** 示さずに確定させると事後にしか分からない。"""
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()
    _schedule(world, due_offset_hours=-1, grace=24.0)

    body = world.client.get(location).text
    assert "仮確定です" in body
    assert "に確定します" in body
    assert "再確認を依頼する" in body, "異議の窓が開いていない"


@needs_c_compiler
def test_a_provisional_grade_can_still_be_contested(world: World) -> None:
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()
    submission_id = location.split("/")[-1].split("?")[0]
    _schedule(world, due_offset_hours=-1, grace=24.0)

    response = world.client.post(
        f"/submissions/{submission_id}/request-review",
        data={"reason": "入力例 3 の想定出力が仕様と違うと思います。"},
        follow_redirects=False,
    )
    assert response.status_code == 303


@needs_c_compiler
def test_the_window_closes_on_time_even_before_the_sweep_runs(world: World) -> None:
    """**締め切りは時刻で決める。確定の有無では決めない。**

    自動確定は定期実行なので、期限と実際の確定の間に隙がある。そこで
    出された依頼は自動確定を恒久的に止め、誰も気づかないまま学期末まで残る。
    """
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()
    submission_id = location.split("/")[-1].split("?")[0]
    # 締切 25 時間前 + 猶予 24 時間 = 期限は 1 時間前。まだ確定処理は走って
    # いない（Finalization は無い）。
    _schedule(world, due_offset_hours=-25, grace=24.0)

    response = world.client.post(
        f"/submissions/{submission_id}/request-review",
        data={"reason": "期限を過ぎてから依頼しようとしています。"},
    )
    assert response.status_code == 409
    assert "仮確定です" not in world.client.get(location).text


@needs_c_compiler
def test_without_a_grace_the_window_never_closes(world: World) -> None:
    """自動確定を設定していないコースでは期限を示していない。締め切れない。"""
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_once()
    submission_id = location.split("/")[-1].split("?")[0]
    _schedule(world, due_offset_hours=-100, grace=None)

    response = world.client.post(
        f"/submissions/{submission_id}/request-review",
        data={"reason": "締切をとうに過ぎていますが依頼できるはずです。"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "仮確定です" not in world.client.get(location).text
