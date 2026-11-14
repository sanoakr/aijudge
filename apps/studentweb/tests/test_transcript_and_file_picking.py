"""画像の読み取り結果を学習者に見せる／ファイルをまとめて選ぶ（2026-11-15）。

読み取り結果: 採点は画像ではなく読み取った文字を見る（ADR 0021）。読み違いで点が付かな
かったとき、何が読まれたかが分からないと直しようがない。自分が出した画像の読み取り結果だけを、
結果の画面に出す。

ファイルの選び方: 課題の画面は「1 枚選んで追加」を繰り返す作りだった。一度に複数を選べ、
ドラッグでも足せ、選んだものを 1 つずつ外せることを、画面の部品で固定する。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from test_studentweb import World, _set_task, _submit_image
from test_studentweb import world as world  # フィクスチャを借りる

from aijudge_core import Extraction
from aijudge_core.ids import GradingRunId, new_id

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "aijudge_studentweb" / "templates"


def _with_extraction(world: World, submission_id: str, **update) -> None:
    """最新の採点に、画像の読み取り結果を付けた新しい採点を重ねる（追記のみ・P8）。"""
    with world.database.unit_of_work() as uow:
        run = uow.runs.latest_for(submission_id)
        assert run is not None
        submission = uow.submissions.get(submission_id)
        artifact = submission.gradable_artifacts[0]
        extraction = Extraction(artifact_id=str(artifact.id), engine="image_text", **update)
        uow.runs.save(
            run.model_copy(
                update={
                    "id": GradingRunId(new_id("grn")),
                    "extractions": (extraction,),
                    "created_at": run.created_at + timedelta(seconds=1),
                }
            )
        )
        uow.commit()


def test_the_result_page_shows_what_was_read_from_the_image(world: World) -> None:
    world.register("s2400001")
    world.login("s2400001")
    location = _submit_image(world)
    world.worker.run_until_empty()
    submission_id = location.split("/")[-1].split("?")[0]
    _with_extraction(world, submission_id, text="修了証\nPython 体験編 1".encode(), model_id="vlm")

    body = world.client.get(location).text

    assert "システムが画像から読み取った文字" in body
    assert "Python 体験編 1" in body
    assert "違っているときは" in body, "読み違いのときの行き先が無い"


def test_a_failed_reading_is_said_and_opened(world: World) -> None:
    """**読み取れなかったことを黙らない。** 0 点にはせず、教員の確認に回る（core/extraction）。"""
    world.register("s2400001")
    world.login("s2400001")
    location = _submit_image(world)
    world.worker.run_until_empty()
    submission_id = location.split("/")[-1].split("?")[0]
    _with_extraction(world, submission_id, failed_reason="vision model returned nothing")

    body = world.client.get(location).text

    assert "この画像から文字を読み取れませんでした" in body
    assert '<details class="transcript" open>' in body


def test_no_reading_section_for_a_code_submission(world: World) -> None:
    """コードの提出に「画像から読み取った文字」は出さない。"""
    world.register("s2400001")
    world.login("s2400001")
    location = world.submit().headers["location"]
    world.worker.run_until_empty()

    assert "システムが画像から読み取った文字" not in world.client.get(location).text


def test_the_task_page_lets_learners_pick_several_files_and_remove_one(world: World) -> None:
    """複数を一度に選べる欄・ドラッグ・1 つずつ外す・すべて外す（JavaScript の部品）。"""
    world.register("s2400001")
    world.login("s2400001")
    _set_task(world, accepted_suffixes=(".png", ".jpg"))
    body = world.client.get(f"/tasks/{world.task_version.id}").text

    assert 'id="upload-picker" type="file" multiple' in body
    assert 'id="upload-drop"' in body
    assert 'id="upload-clear"' in body
    assert "file_pick.js" in body
    assert "この 1 枚を追加" not in body, "1 枚ずつ追加する作りが残っている"


def test_the_editor_attach_box_offers_drop_and_clear() -> None:
    template = (TEMPLATES / "ide.html").read_text(encoding="utf-8")

    assert "data-attach-drop" in template
    assert "ide-attach-clear" in template
    assert "file_pick.js" in template
    assert '"maxFiles": max_files' in template
