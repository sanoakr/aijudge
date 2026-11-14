"""AI が何も判定しない課題の提出は、blind 採点の母集団に入れない（2026-11-15）。

blind 採点は AI と人の一致度を測るためのもの（ADR 0005）。決定的評価器だけの
課題（network のアンケート等）には測る相手がおらず、抽出されても教員の手間が
増えるだけである。抽出率が 1.0 のコースでも、そうした課題は抽出しない。

固定したいのは 4 つ。

抽出しない   AI が判定する観点が無い課題版の提出は blind の対象でない
数えない     帯・ダイジェストの blind の件数にも、/blind の一覧にも出ない
確認へ進める  確認画面は /blind へ回さずにそのまま開く
今までどおり  AI の観点がある課題は、同じコースでも抽出される
"""

from __future__ import annotations

from test_console import COURSE, World, needs_c_compiler
from test_console import blind_world as blind_world  # フィクスチャを借りる

from aijudge_core import Role
from aijudge_core.ids import TaskVersionId
from aijudge_reviewconsole.rail_context import course_attention
from aijudge_reviewconsole.sampling import judged_by_ai

DETERMINISTIC_ONLY = "code_test_runner"
NO_AI = {"ai_evaluators": []}


def test_judged_by_ai_follows_the_pipeline_rule(blind_world: World) -> None:
    criteria = blind_world.task_version.criteria
    assert judged_by_ai(criteria, ["rubric_ai_judge"])
    assert not judged_by_ai(criteria, [])
    named = [c.model_copy(update={"evaluator_id": DETERMINISTIC_ONLY}) for c in criteria]
    assert not judged_by_ai(named, ["rubric_ai_judge"])
    assert judged_by_ai(named, [DETERMINISTIC_ONLY])


def test_a_version_whose_criteria_name_deterministic_evaluators_is_not_drawn(
    blind_world: World,
) -> None:
    learner = blind_world.register("s2400001", role=Role.LEARNER)
    submission = blind_world.submit(learner).submission
    named = blind_world.task_version.model_copy(
        update={
            "id": TaskVersionId("tv-deterministic-only"),
            "version": blind_world.task_version.version + 1,
            "criteria": tuple(
                c.model_copy(update={"evaluator_id": DETERMINISTIC_ONLY})
                for c in blind_world.task_version.criteria
            ),
        }
    )
    with blind_world.database.unit_of_work() as uow:
        uow.tasks.save_version(named)
        course = uow.identity.get_course(COURSE)
        uow.commit()

    console = blind_world.console
    assert console.needs_blind_mark(submission, course)
    assert not console.needs_blind_mark(submission, course, task_version_id=named.id)


@needs_c_compiler
def test_a_course_without_ai_evaluators_gets_no_blind_work(blind_world: World) -> None:
    blind_world.register("instructor", role=Role.INSTRUCTOR)
    accepted = blind_world.submit(blind_world.register("s2400001", role=Role.LEARNER))
    blind_world.login("instructor")
    blind_world.worker.run_until_empty()
    console = blind_world.console

    def attention_blind() -> int:
        with blind_world.database.unit_of_work() as uow:
            course = uow.identity.get_course(COURSE)
            return course_attention(
                uow, course, blind_rate=1.0, judged_by_ai=console.judged_by_ai
            ).blind

    # 今までどおり: AI の観点がある課題は抽出され、帯に数えられ、確認は blind へ回る。
    assert attention_blind() == 1
    drawn = blind_world.client.get(
        f"/review/{accepted.submission.id}/reveal", follow_redirects=False
    )
    assert drawn.status_code == 303
    assert str(accepted.submission.id) in blind_world.client.get(f"/courses/{COURSE}/blind").text

    # 実効設定から AI 評価器が無くなると、同じ提出が母集団から外れる。
    with blind_world.database.unit_of_work() as uow:
        course = uow.identity.get_course(COURSE)
        uow.identity.save_course(course.model_copy(update={"grading_overrides": NO_AI}))
        uow.commit()

    assert attention_blind() == 0
    listing = blind_world.client.get(f"/courses/{COURSE}/blind").text
    assert str(accepted.submission.id) not in listing
    opened = blind_world.client.get(
        f"/review/{accepted.submission.id}/reveal", follow_redirects=False
    )
    assert opened.status_code == 200
