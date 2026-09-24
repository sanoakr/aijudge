"""ブラウザ IDE の受け口（`docs/design/online-coding-test.md` §5・§8・§9）。

固定したいのは次のこと。

I5    `upload` の課題には何も起きない（画面も経路も 404）。
形式  選べる形式は課題の提出形式のうち .c・.py・.md だけ。画面の選択肢を信じず、
      受け口がもう一度確かめる。
実行  採点の言語と同じ形式だけ積む。web は行を書くだけで、動かさない。
I8    実行・提出・保存は `/submit` と同じ関門を通る（受付の外・学外では断る）。
I2    提出は既存の `accept()` を通り、ファイルで出したのと同じ形になる。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_studentweb import TENANT, World
from test_studentweb import world as world  # フィクスチャを借りる

from aijudge_core import AnswerMode, ArtifactKind
from aijudge_core.ids import TenantId
from aijudge_identity import CampusNetworkSettings

SOURCE = '#include <stdio.h>\nint main(void){puts("hi");return 0;}\n'
REPORT = "# 考察\n\n配列の探索は O(n) である。\n"


def _task(world: World, **update) -> None:
    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(world.task_version.task_id)
        uow.tasks.save_task(task.model_copy(update=update))
        uow.commit()


def _editor(world: World, **update) -> None:
    _task(world, answer_mode=AnswerMode.EDITOR, **update)


def _learner(world: World) -> None:
    world.register("s2400001")
    world.login("s2400001")


def _run(world: World, **body):
    payload = {"suffix": ".c", "source": SOURCE} | body
    return world.client.post(f"/ide/tasks/{world.task_version.id}/run", json=payload)


def _submit(world: World, **body):
    payload = {"suffix": ".c", "source": SOURCE} | body
    return world.client.post(f"/ide/tasks/{world.task_version.id}/submit", json=payload)


# -- I5: upload の課題には何も起きない ----------------------------------------


def test_an_upload_task_has_no_editor(world: World) -> None:
    _learner(world)

    assert world.client.get(f"/courses/{_course_id(world)}/ide").status_code == 404
    assert _run(world).status_code == 404
    assert _submit(world).status_code == 404
    page = world.client.get(f"/courses/{_course_id(world)}").text
    assert "エディタで解く" not in page


def _config(page: str) -> dict:
    """画面の設定（`#ide-config`）を読む。エディタの中身はここから入る。"""
    import json
    import re

    match = re.search(r'<script type="application/json" id="ide-config">(.*?)</script>', page, re.S)
    assert match, "設定のブロックが無い"
    return json.loads(match.group(1))


def _course_id(world: World) -> str:
    from test_studentweb import COURSE

    return str(COURSE)


# -- 画面 --------------------------------------------------------------------


def test_the_course_page_offers_the_editor(world: World) -> None:
    _editor(world)
    _learner(world)

    page = world.client.get(f"/courses/{_course_id(world)}").text

    assert "エディタで解く" in page
    assert f"/courses/{_course_id(world)}/ide" in page


def test_the_editor_page_lists_only_the_tasks_formats(world: World) -> None:
    """**選べる形式は課題の提出形式に限る。** .java や .pdf は出さない。"""
    _editor(world, accepted_suffixes=(".c", ".md", ".pdf", ".java"))
    _learner(world)

    page = world.client.get(f"/courses/{_course_id(world)}/ide").text

    assert 'value=".c"' in page
    assert 'value=".md"' in page
    assert 'value=".py"' not in page
    assert 'value=".java"' not in page
    assert 'value=".pdf"' not in page


def test_the_editor_page_does_not_leak_hidden_cases(world: World) -> None:
    """**非公開のテストケースは画面に出さない**（ADR 0024 §4）。"""
    _editor(world)
    _learner(world)

    page = world.client.get(f"/courses/{_course_id(world)}/ide").text

    # 例題のケースはすべて非公開。入力の 1 つがそのまま出ていないこと。
    assert "9 -3 -2 -1 4 5 6 7 8 11" not in page
    assert "公開サンプル" not in page


def test_the_editor_page_restores_the_autosave(world: World) -> None:
    _editor(world)
    _learner(world)
    world.client.put(
        f"/ide/tasks/{world.task_version.id}/buffer", json={"suffix": ".md", "source": REPORT}
    )

    page = world.client.get(f"/courses/{_course_id(world)}/ide").text

    config = _config(page)
    assert config["sources"] == [REPORT]
    assert config["selected"] == [".md"]
    assert '<option value=".md" selected' in page


# -- 実行 --------------------------------------------------------------------


def test_a_run_is_queued_not_executed(world: World) -> None:
    """web は行を書くだけ（ADR 0024）。結果は runner が書き戻す。"""
    _editor(world)
    _learner(world)

    response = _run(world, stdin="1 2\n")

    assert response.status_code == 202
    run_id = response.json()["id"]
    state = world.client.get(f"/ide/runs/{run_id}").json()
    assert state["state"] == "queued"
    assert state["ahead"] == 0
    assert state["outcome"] is None


def test_only_the_grading_language_runs(world: World) -> None:
    """採点が C の課題で .py や .md は実行しない（提出はできる）。"""
    _editor(world, accepted_suffixes=(".c", ".py", ".md"))
    _learner(world)

    assert _run(world, suffix=".py", source="print(1)\n").status_code == 409
    assert _run(world, suffix=".md", source=REPORT).status_code == 409


def test_a_format_the_task_does_not_accept_is_refused(world: World) -> None:
    """**画面の選択肢を信じない。** 受け付けない形式を送られたら断る。"""
    _editor(world, accepted_suffixes=(".md",))
    _learner(world)

    assert _run(world, suffix=".c").status_code == 400
    assert _submit(world, suffix=".c").status_code == 400


def test_a_second_run_while_one_waits_is_refused(world: World) -> None:
    _editor(world)
    _learner(world)
    assert _run(world).status_code == 202

    again = _run(world)

    assert again.status_code == 429
    assert again.json()["reason"] == "already_pending"


def test_someone_elses_run_is_not_found(world: World) -> None:
    _editor(world)
    _learner(world)
    run_id = _run(world).json()["id"]

    world.register("s2400002")
    world.login("s2400002")

    assert world.client.get(f"/ide/runs/{run_id}").status_code == 404


# -- I8: 同じ関門 -----------------------------------------------------------


def test_nothing_runs_after_the_window_closes(world: World) -> None:
    """**受付の外では実行もさせない。** 任意のコードを走らせられる窓口にしない。"""
    past = datetime.now(UTC) - timedelta(hours=1)
    _editor(world, due_at=past - timedelta(hours=1), accepts_until=past)
    _learner(world)

    assert _run(world).status_code == 409
    assert _submit(world).status_code == 409
    saved = world.client.put(
        f"/ide/tasks/{world.task_version.id}/buffer", json={"suffix": ".c", "source": SOURCE}
    )
    assert saved.status_code == 409


def test_a_campus_only_task_refuses_the_editor_from_outside(world: World) -> None:
    _editor(world, campus_only=True)
    with world.database.unit_of_work() as uow:
        uow.identity.save_campus_networks(
            CampusNetworkSettings(tenant_id=TenantId(TENANT), cidrs=("133.83.80.0/24",))
        )
        uow.commit()
    _learner(world)

    outside = world.client.post(
        f"/ide/tasks/{world.task_version.id}/run",
        json={"suffix": ".c", "source": SOURCE},
        headers={"x-forwarded-for": "82.26.195.14"},
    )

    assert outside.status_code == 409
    assert "学内からのみ" in outside.text


# -- I2: 提出 ----------------------------------------------------------------


def test_a_program_is_submitted_as_the_file_would_be(world: World) -> None:
    _editor(world)
    _learner(world)

    response = _submit(world)

    assert response.status_code == 200
    data = response.json()
    assert data["attempt"] == 1
    with world.database.unit_of_work() as uow:
        from aijudge_core.ids import SubmissionId

        submission = uow.submissions.get(SubmissionId(data["submission_id"]))
        assert uow.jobs.pending_count() == 1
    (artifact,) = submission.artifacts
    assert artifact.filename == "main.c"
    assert artifact.kind is ArtifactKind.CODE


def test_a_report_is_submitted_as_text(world: World) -> None:
    """オンラインのレポート試験。.md はテキストとして提出する。"""
    _editor(world, accepted_suffixes=(".md",))
    _learner(world)

    response = _submit(world, suffix=".md", source=REPORT)

    assert response.status_code == 200
    with world.database.unit_of_work() as uow:
        from aijudge_core.ids import SubmissionId

        submission = uow.submissions.get(SubmissionId(response.json()["submission_id"]))
    (artifact,) = submission.artifacts
    assert artifact.filename == "answer.md"
    assert artifact.kind is ArtifactKind.MARKDOWN


def test_submitting_also_updates_the_autosave(world: World) -> None:
    """提出したのに、リロードすると前の書きかけに戻る、を起こさない。"""
    _editor(world)
    _learner(world)
    world.client.put(
        f"/ide/tasks/{world.task_version.id}/buffer",
        json={"suffix": ".c", "source": "int main(void){}\n"},
    )

    _submit(world)

    page = world.client.get(f"/courses/{_course_id(world)}/ide").text
    assert _config(page)["sources"] == [SOURCE]


def test_the_same_content_twice_is_folded(world: World) -> None:
    _editor(world)
    _learner(world)
    _submit(world)

    again = _submit(world).json()

    assert again["deduplicated"] is True
