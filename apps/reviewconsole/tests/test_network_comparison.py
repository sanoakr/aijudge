"""クライアント・サーバの課題の突き合わせと、検証データの表示（2026-10-01）。

`network_test_runner` は行を揃えて比べるのではなく、期待する断片が出力に含まれるかを
見る。記録の形も入出力の課題と違う。以前は入出力の形で読んでいたので、出力が
記録されているのに「出力は記録されていません」と出ていた。固定したいのは 5 つ。

断片ごとに見せる   期待する断片それぞれが、見つかったかどうか
出力を見せる       提出物と伴走プロセスの出力（採点のとき記録したもの）
入力は埋めて       `{host}`・`{port}` は採点で渡した値にして見せる
待ち受けなし       理由を人の言葉で、待たれた側の標準エラーも
データを見せる     伴走ソース（1 つにまとめて）とケースの中身を、編集・TA 画面に
"""

from __future__ import annotations

from datetime import UTC, datetime

from aijudge_core import (
    GradingRun,
    Provenance,
    RubricCriterion,
    RubricLevel,
    TaskVersion,
    TestCase,
)
from aijudge_core.ids import CriterionId, TaskId, TaskVersionId, UserId
from aijudge_reviewconsole.app import TEMPLATES
from aijudge_reviewconsole.companion_view import companion_view
from aijudge_reviewconsole.io_results import io_results

NOW = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
CORRECT = CriterionId("crt_" + "1" * 32)
RESULT = "evr_" + "3" * 32
RUNNER = "network_test_runner"
SERVER = "import socket\nprint('echo server')\n"


def _payload(**extra) -> dict:
    return {
        "role": "client",
        "companion": SERVER,
        "companion_name": "echoServer.py",
        "port": 50007,
        "input": "{host}\n{port}\nhello\n",
        "companion_input": "",
        "fixtures": {"data.txt": "abc\n"},
        "expected_contains": ["hello", "bye"],
        "companion_expected_contains": ["connected"],
    } | extra


def _task() -> TaskVersion:
    return TaskVersion(
        id=TaskVersionId("tsv_" + "5" * 32),
        task_id=TaskId("tsk_" + "6" * 32),
        version=1,
        subject_profile="cs_network_python",
        statement="エコーのクライアント",
        criteria=(
            RubricCriterion(
                id=CORRECT,
                code="correctness",
                title="正しく通信できる",
                description="サーバとやりとりできるか。",
                weight=1.0,
                evaluator_id=RUNNER,
                levels=(
                    RubricLevel(level=0, label="未達", descriptor="通信できない", score_ratio=0.0),
                    RubricLevel(level=1, label="達成", descriptor="通信できる", score_ratio=1.0),
                ),
            ),
        ),
        max_score=100.0,
        test_cases=(
            TestCase(name="case1", evaluator_id=RUNNER, payload=_payload(), hidden=True),
            TestCase(name="case2", evaluator_id=RUNNER, payload=_payload()),
        ),
        provenance=Provenance(authored_by=UserId("usr_" + "7" * 32)),
        created_at=NOW,
    )


def _run(cases: list[dict]) -> GradingRun:
    return GradingRun.model_validate(
        {
            "id": "grn_" + "9" * 32,
            "submission_id": "sub_" + "a" * 32,
            "context": {
                "task_version_id": "tsv_" + "5" * 32,
                "subject_profile": "cs_network_python",
                "rubric_version": "r@1",
                "input_hash": "sha256:x",
                "pipeline_version": "1",
            },
            "evaluator_results": [
                {
                    "id": RESULT,
                    "evaluator_id": RUNNER,
                    "kind": "deterministic",
                    "raw_output": {"cases": cases, "sandbox": "docker", "isolation": "container"},
                }
            ],
            "criterion_scores": [
                {
                    "id": "cs_" + "1" * 32,
                    "criterion_id": str(CORRECT),
                    "evaluator_result_id": RESULT,
                    "kind": "deterministic",
                    "level": 0,
                    "score_ratio": 0.0,
                    "weight": 1.0,
                    "confidence": 1.0,
                    "conclusive": True,
                    "evidence": [
                        {
                            "artifact_id": "art_" + "8" * 32,
                            "artifact_content_hash": "x",
                            "span": {"kind": "whole"},
                        }
                    ],
                    "rationale": "2 件中 1 件が一致しました。",
                }
            ],
            "score_ratio": 0.0,
            "confidence": 1.0,
            "routing": "review_required",
            "created_at": "2026-10-01T00:00:00Z",
        }
    )


MISMATCH = {
    "name": "case1",
    "weight": 1.0,
    "role": "client",
    "port": 50007,
    "passed": False,
    "reason": "output mismatch",
    "submission_stdout": "hello\n",
    "companion_stdout": "connected\n",
    "missing": ["bye"],
    "missing_companion": [],
}
NOT_LISTENING = {
    "name": "case2",
    "weight": 1.0,
    "role": "client",
    "port": 50007,
    "passed": False,
    "reason": "not_listening",
    "detail": "伴走サーバが 5 秒以内にポート 50007 で待ち受けを始めませんでした",
    "background_stderr": "OSError: Address already in use",
}


def test_each_expected_fragment_says_whether_it_was_found() -> None:
    result = io_results(_task(), _run([MISMATCH]))[CORRECT]

    assert result.is_network and (result.passed, result.total) == (0, 1)
    (case,) = result.network_cases
    assert [(n.text, n.found) for n in case.expected] == [("hello", True), ("bye", False)]
    assert [(n.text, n.found) for n in case.companion_expected] == [("connected", True)]
    assert case.submission_stdout == "hello\n" and case.companion_stdout == "connected\n"
    assert case.recorded and case.hidden, "非公開は課題の版から引く"


def test_the_input_is_shown_with_host_and_port_filled_in() -> None:
    (case,) = io_results(_task(), _run([MISMATCH]))[CORRECT].network_cases

    assert case.input == "127.0.0.1\n50007\nhello\n"


def test_not_listening_says_why_and_shows_the_waiting_side() -> None:
    (case,) = io_results(_task(), _run([NOT_LISTENING]))[CORRECT].network_cases

    assert case.reason == "待ち受けが始まらない"
    assert "ポート 50007" in case.detail
    assert case.background_stderr == "OSError: Address already in use"
    assert not case.recorded
    assert all(n.found is None for n in case.expected), "出力が無ければ見つかったかは分からない"


def test_the_page_shows_fragments_and_outputs_not_a_missing_output_note() -> None:
    html = TEMPLATES.env.get_template("_io_result.html").render(
        io=io_results(_task(), _run([MISMATCH, NOT_LISTENING]))[CORRECT]
    )

    assert "クライアント・サーバの突き合わせ" in html
    assert "0 / 2 件一致" in html
    assert "bye" in html and "connected" in html
    # 出力が記録された case1 に「記録されていません」を出さない（以前の誤り）。
    case1 = html[html.index("case1") : html.index("case2")]
    assert "出力は記録されていません" not in case1
    assert "待ち受けが始まらない" in html


def test_the_companion_source_is_shown_once_for_all_cases() -> None:
    view = companion_view(_task().test_cases)

    assert [source.name for source in view.sources] == ["echoServer.py"]
    assert [case.name for case in view.cases] == ["case1", "case2"]
    html = TEMPLATES.env.get_template("_companion_cases.html").render(companion=view)
    assert html.count("print(&#39;echo server&#39;)") == 1
    assert "data.txt" in html and "bye" in html and "50007" in html


def test_the_task_pages_show_the_client_server_cases() -> None:
    """**教員の編集画面と TA の閲覧画面に、ケースの中身が出る**（以前はどこにも無かった）。"""
    import tempfile
    from pathlib import Path

    from test_manage import World, _user_id

    from aijudge_authoring import TaskSpec
    from aijudge_authoring.spec import CriterionSpec, LevelSpec, TestCaseSpec
    from aijudge_core import Role
    from aijudge_course_admin.authoring import save_task

    world = World(Path(tempfile.mkdtemp()))
    try:
        world.register("teacher", Role.INSTRUCTOR)
        world.register("ta", Role.ASSISTANT)
        saved = save_task(
            world.database,
            course_id=world.course.id,
            spec=TaskSpec(
                key="ex4/p1",
                unit="ex4",
                statement="## [必須] エコー ##\n\nサーバに接続する。",
                criteria=(
                    CriterionSpec(
                        code="network",
                        title="通信できる",
                        description="サーバとやり取りできるか。",
                        weight=1.0,
                        evaluator=RUNNER,
                        levels=(
                            LevelSpec(
                                level=0, label="未達", descriptor="通らない", score_ratio=0.0
                            ),
                            LevelSpec(level=1, label="達成", descriptor="通る", score_ratio=1.0),
                        ),
                    ),
                ),
                test_cases=(TestCaseSpec(name="connect", evaluator=RUNNER, payload=_payload()),),
            ),
            subject_profile="cs_lang_c_intro",
            authored_by=_user_id(world, "teacher"),
        )
        path = f"/manage/courses/{world.course.id}/tasks/{saved.task.id}/edit"
        for login in ("teacher", "ta"):
            body = world.client(login).get(path).text
            assert "クライアント・サーバのケース（1 件）" in body, login
            assert "echoServer.py" in body and "bye" in body, login
    finally:
        world.close()
