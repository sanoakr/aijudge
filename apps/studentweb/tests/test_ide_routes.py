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

from test_studentweb import COURSE, TENANT, World
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
        link = uow.ide_links.for_submission(submission.id)
    (artifact,) = submission.artifacts
    assert artifact.filename == "main.c"
    assert artifact.kind is ArtifactKind.CODE
    # 本人が押した提出として残る（受付終了時の自動提出と区別する）。
    assert link is not None and link.origin.value == "editor"


def test_an_editor_only_task_refuses_a_file_but_takes_the_editor(world: World) -> None:
    """**エディタだけ**（試験・2026-09-24）。ファイルの欄は出さず、受付でも断る ──
    画面から消すだけでは境界にならない（#146）。エディタからは出せる。"""
    _editor(world, file_upload=False)
    _learner(world)

    page = world.client.get(f"/tasks/{world.task_version.id}").text
    assert "ファイルでは提出できません" in page
    assert f'action="/tasks/{world.task_version.id}/submit"' not in page

    refused = world.submit()
    assert refused.status_code == 409
    assert "エディタからだけ" in refused.text

    assert _submit(world).status_code == 200


def test_an_editor_task_still_takes_a_file_by_default(world: World) -> None:
    """既定は両方。エディタが使えない環境の逃げ道を残す。"""
    _editor(world)
    _learner(world)

    page = world.client.get(f"/tasks/{world.task_version.id}").text
    assert f'action="/tasks/{world.task_version.id}/submit"' in page
    assert world.submit().status_code == 303


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


# -- 行動記録（ADR 0023） -----------------------------------------------------


def _with_activity(world: World, tmp_path) -> None:
    world.app.activity_dir = tmp_path / "activity"


def _start(world: World, *, consent: bool = True):
    return world.client.post(
        "/ide/sessions",
        json={"course_id": _course_id(world), "unit": "", "consent": consent, "user_agent": "UA"},
    )


def _batch(world: World, session_id: str, seq: int, events=None, snapshots=None):
    return world.client.post(
        "/ide/activity",
        json={
            "session_id": session_id,
            "seq": seq,
            "client_time": 1_790_000_000_000,
            "events": events if events is not None else [{"type": "edit", "t": 1.5, "text": "a"}],
            "snapshots": snapshots or {},
        },
    )


def test_a_session_needs_consent_the_first_time(world: World, tmp_path) -> None:
    """**告知を確認しなければ始めない**（ADR 0023 §5）。2 回目からは出し直さない。"""
    _editor(world)
    _learner(world)
    assert _config(world.client.get(f"/courses/{_course_id(world)}/ide").text)["needsConsent"]

    refused = _start(world, consent=False)
    assert refused.status_code == 409
    assert refused.json()["reason"] == "consent_required"

    assert _start(world, consent=True).status_code == 201
    assert _start(world, consent=False).status_code == 201
    assert (
        _config(world.client.get(f"/courses/{_course_id(world)}/ide").text)["needsConsent"] is False
    )


def test_a_batch_is_written_to_a_file_and_indexed(world: World, tmp_path) -> None:
    import gzip

    _editor(world)
    _learner(world)
    _with_activity(world, tmp_path)
    session_id = _start(world).json()["session_id"]

    response = _batch(world, session_id, 0)

    assert response.status_code == 200 and response.json() == {"stored": True}
    with world.database.unit_of_work() as uow:
        (batch,) = uow.ide_activity.batches(session_id)
    body = gzip.decompress((tmp_path / "activity" / batch.path).read_bytes()).decode()
    assert '"type":"edit"' in body
    assert batch.event_count == 1


def test_a_resent_batch_is_accepted_but_not_duplicated(world: World, tmp_path) -> None:
    _editor(world)
    _learner(world)
    _with_activity(world, tmp_path)
    session_id = _start(world).json()["session_id"]

    _batch(world, session_id, 0)
    again = _batch(world, session_id, 0)

    assert again.status_code == 200 and again.json() == {"stored": False}


def test_an_unknown_event_type_is_refused(world: World, tmp_path) -> None:
    _editor(world)
    _learner(world)
    _with_activity(world, tmp_path)
    session_id = _start(world).json()["session_id"]

    response = _batch(world, session_id, 0, events=[{"type": "screenshot", "t": 1}])

    assert response.status_code == 400


def test_without_a_place_to_write_the_record_says_come_back_later(world: World) -> None:
    """**書けなくても何も止めない**（I7）。503 と Retry-After で間隔を広げさせる。"""
    _editor(world)
    _learner(world)
    world.app.activity_dir = None
    session_id = _start(world).json()["session_id"]

    response = _batch(world, session_id, 0)

    assert response.status_code == 503
    assert response.headers["retry-after"]
    # 記録が書けなくても、実行と提出は通る。
    assert _run(world).status_code == 202


def test_someone_elses_session_does_not_exist(world: World, tmp_path) -> None:
    _editor(world)
    _learner(world)
    _with_activity(world, tmp_path)
    session_id = _start(world).json()["session_id"]

    world.register("s2400002")
    world.login("s2400002")

    assert _batch(world, session_id, 0).status_code == 404


def test_the_completion_setting_reaches_the_editor(world: World) -> None:
    """補完は課題ごとの値（設計書 §5.3）。既定は切。"""
    _editor(world)
    _learner(world)
    assert _config(world.client.get(f"/courses/{_course_id(world)}/ide").text)["completion"] == [
        False
    ]

    _task(world, editor_completion=True)

    assert _config(world.client.get(f"/courses/{_course_id(world)}/ide").text)["completion"] == [
        True
    ]


def test_the_header_and_countdown_are_outside_noscript(world: World) -> None:
    """**見出し（残り時間）は JavaScript のある画面に出る。**

    以前、案内を差し込む目印がテンプレートのコメントの中にもあり、見出しが
    noscript の中に入って消えていた ── 試験では残り時間が見えなくなる。
    """
    from datetime import UTC, datetime, timedelta

    _editor(world, accepts_until=datetime.now(UTC) + timedelta(hours=1))
    _learner(world)

    page = world.client.get(f"/courses/{_course_id(world)}/ide").text

    assert page.count("<noscript>") == 1
    inside = page.split("<noscript>", 1)[1].split("</noscript>", 1)[0]
    assert "ide-head" not in inside and "data-ide-remaining" not in inside
    assert "data-ide-remaining" in page
    assert "data-ide-consent" not in inside


# -- 画像・PDF をエディタの画面から出す（2026-09-25） ---------------------------

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _second_task(world: World, suffixes: tuple[str, ...], *, title: str = "認定証"):
    """同じ問題セットに、画像などを受ける課題を足す（エディタの答え方で）。"""
    from aijudge_core import Task
    from aijudge_core.ids import TaskId, TaskVersionId

    base = world.task_version
    version = base.model_copy(
        update={
            "id": TaskVersionId("tsv_" + "e" * 32),
            "task_id": TaskId("tsk_" + "e" * 32),
            "statement": f"## [必須] {title} ##\n\n画像を出してください。",
        }
    )
    with world.database.unit_of_work() as uow:
        uow.tasks.save_task(
            Task(
                id=version.task_id,
                course_id=COURSE,
                title=title,
                position=2,
                answer_mode=AnswerMode.EDITOR,
                accepted_suffixes=suffixes,
            )
        )
        uow.tasks.save_version(version)
        uow.commit()
    return version


def _upload(world: World, version_id, name: str, payload: bytes, kind: str = "image/png"):
    return world.client.post(
        f"/ide/tasks/{version_id}/upload", files={"upload": (name, payload, kind)}
    )


def test_an_image_task_gets_a_tab_with_a_file_picker(world: World) -> None:
    """画像だけの課題も、セットにエディタで書く課題があればタブとして並ぶ（中身はファイル欄）。"""
    _editor(world)
    image = _second_task(world, (".png", ".pdf"))
    _learner(world)

    page = world.client.get(f"/courses/{_course_id(world)}/ide").text
    config = _config(page)

    assert str(image.id) in config["tabs"]
    assert config["modes"][config["tabs"].index(str(image.id))] == "attach"
    assert 'class="ide-attach-input"' in page and 'accept=".png,.pdf"' in page


def test_an_image_is_submitted_from_the_editor_page(world: World) -> None:
    """**課題の画面と同じ検査**（`accept_uploads`）を通り、エディタの経路の提出として残る。"""
    _editor(world)
    image = _second_task(world, (".png", ".pdf"))
    _learner(world)

    response = _upload(world, image.id, "cert.png", PNG)

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["attempt"] == 1 and data["files"][0]["name"] == "cert.png"
    with world.database.unit_of_work() as uow:
        from aijudge_core.ids import SubmissionId

        submission = uow.submissions.get(SubmissionId(data["submission_id"]))
        link = uow.ide_links.for_submission(submission.id)
    assert submission.artifacts[0].kind is ArtifactKind.IMAGE
    assert link is not None and link.origin.value == "editor"


def test_the_editor_page_takes_no_video_and_no_code_files(world: World) -> None:
    """動画は分割送信の経路（課題の画面）だけ。コードはエディタで書く（ファイル欄では受けない）。"""
    _editor(world)
    image = _second_task(world, (".png", ".mp4", ".c"))
    _learner(world)

    assert _upload(world, image.id, "demo.mp4", b"\x00" * 64, "video/mp4").status_code == 400
    assert _upload(world, image.id, "main.c", SOURCE.encode(), "text/plain").status_code == 400
    page = world.client.get(f"/courses/{_course_id(world)}/ide").text
    assert "動画はエディタの画面からは提出できません" in page


def test_an_editor_only_set_still_takes_images_in_the_editor(world: World) -> None:
    """「エディタだけ」でも、エディタの画面からは画像を出せる（課題の画面の欄は止まる）。"""
    _editor(world, file_upload=False)
    image = _second_task(world, (".png",))
    with world.database.unit_of_work() as uow:
        task = uow.tasks.get_task(image.task_id)
        uow.tasks.save_task(task.model_copy(update={"file_upload": False}))
        uow.commit()
    _learner(world)

    assert _upload(world, image.id, "cert.png", PNG).status_code == 200
    refused = world.client.post(
        f"/tasks/{image.id}/submit",
        files={"upload": ("cert.png", PNG, "image/png")},
        follow_redirects=False,
    )
    assert refused.status_code == 409


def test_a_set_of_images_only_does_not_open_the_editor(world: World) -> None:
    """書ける課題が 1 つも無いセットはエディタで開かない（`editor_blockers` と同じ規則）。"""
    _task(world, answer_mode=AnswerMode.EDITOR, accepted_suffixes=(".png",))
    _learner(world)

    assert world.client.get(f"/courses/{_course_id(world)}/ide").status_code == 404


def test_a_large_external_paste_leaves_only_its_fingerprint(world: World, tmp_path) -> None:
    """受け口は大きな外からの貼り付けの**指紋だけ**を索引に残す（2026-09-25）。再送で増えない。"""
    from aijudge_ide import IdeSessionId

    _editor(world)
    _learner(world)
    _with_activity(world, tmp_path)
    session_id = _start(world).json()["session_id"]
    paste = {"type": "paste", "t": 5, "tab": 0, "len": 120, "hash": "f" * 64, "origin": "external"}

    assert _batch(world, session_id, 0, events=[paste]).status_code == 200
    assert _batch(world, session_id, 0, events=[paste]).status_code == 200  # 再送

    with world.database.unit_of_work() as uow:
        session = uow.ide_activity.get_session(IdeSessionId(session_id))
        found = uow.ide_activity.learners_sharing(session.course_id, ["f" * 64])
    assert found == {"f" * 64: frozenset({session.learner_id})}
