"""作業の記録を問題セットごと・問題ごとに見る画面（2026-11-06）。

固定したいのは次のこと。

教員だけ       TA には開けない。受講していない人の記録は数えない。
見たら残る     一覧も問題セットも問題も、開いたことが監査ログに残る。
問題に分ける   1 回のセッションで問題を行き来しても、問題ごとの数字に分かれる。
読めなくても   記録の形が悪い学習者がいても、全員分の画面は落ちない。
無いと 0 は別  置き場所が無い・記録が無いときに「0 字」と書かない。
学習者と同じ   印と採用の規則は学習者の画面と同じ。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_manage import World, _import_example
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import AnswerMode, Role
from aijudge_core.ids import TaskId, TaskVersionId, new_id
from aijudge_ide import ActivityFiles, EventBatch, IdeSession, IdeSessionId, snapshot_name

NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
UNIT = "ex01"
START = "int main(void){}\n"
SECOND_TITLE = "第二の問題"


def _record(
    world: World, learner, root: Path, events: list[dict], *, unit: str | None = UNIT
) -> IdeSession:
    session = IdeSession(
        id=IdeSessionId(new_id("ide")),
        tenant_id=world.course.tenant_id,
        learner_id=learner.user_id,
        course_id=world.course.id,
        unit=unit,
        started_at=NOW,
        consented_at=NOW,
    )
    path, digest, size = ActivityFiles(root).write_batch(
        session, 0, events, {snapshot_name(START): START}
    )
    with world.database.unit_of_work() as uow:
        uow.ide_activity.start_session(session)
        uow.ide_activity.add_batch(
            EventBatch(
                ide_session_id=session.id,
                seq=0,
                received_at=NOW,
                event_count=len(events),
                snapshot_count=1,
                byte_size=size,
                sha256=digest,
                path=path,
            )
        )
        uow.commit()
    return session


def _hello(versions: list[str]) -> dict:
    return {
        "type": "hello",
        "t": 0,
        "tabs": max(1, len(versions)),
        "hashes": [snapshot_name(START)],
        "tasks": versions,
    }


def _paste(t: int, tab: int, length: int) -> dict:
    return {"type": "paste", "t": t, "tab": tab, "len": length, "origin": "external"}


def _two_problems(world: World) -> tuple[list[str], list[str]]:
    """同じ問題セットに課題を 2 つ用意する。(課題 ID, 課題版 ID) を返す。"""
    from aijudge_course_admin.operations import list_tasks

    _import_example(world)
    (task, version) = list_tasks(world.database, world.course.id)[0]
    first = task.model_copy(update={"unit": UNIT, "answer_mode": AnswerMode.EDITOR})
    second = task.model_copy(
        update={
            "id": TaskId("tsk_" + "e" * 32),
            "title": SECOND_TITLE,
            "unit": UNIT,
            "answer_mode": AnswerMode.EDITOR,
        }
    )
    second_version = version.model_copy(
        update={"id": TaskVersionId("tsv_" + "e" * 32), "task_id": second.id}
    )
    with world.database.unit_of_work() as uow:
        uow.tasks.save_task(first)
        uow.tasks.save_task(second)
        uow.tasks.save_version(second_version)
        uow.commit()
    return [str(first.id), str(second.id)], [str(version.id), str(second_version.id)]


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "activity"
    monkeypatch.setenv("AIJUDGE_ACTIVITY_DIR", str(directory))
    return directory


def _audited(world: World) -> list:
    with world.database.unit_of_work() as uow:
        return [
            e
            for e in uow.audit.list_recent(world.course.tenant_id, limit=50)
            if e.action.value == "activity.viewed"
        ]


def _base(world: World) -> str:
    return f"/courses/{world.course.id}/activity"


def test_the_course_list_offers_the_other_way_in(world: World, root) -> None:
    world.register("teacher", Role.INSTRUCTOR)

    page = world.client("teacher").get(_base(world)).text

    assert f'{_base(world)}/units"' in page
    assert "問題セット・問題ごと" in page


def test_the_routes_are_not_taken_for_a_learner(world: World, root) -> None:
    """`/activity/units` が学習者の画面（`/activity/{learner_id}`）に当たらない。"""
    world.register("teacher", Role.INSTRUCTOR)

    response = world.client("teacher").get(f"{_base(world)}/units")

    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/units", f"/units/{UNIT}", "/tasks/tsk_x"])
def test_an_assistant_cannot_open_them(world: World, root, path: str) -> None:
    world.register("ta", Role.ASSISTANT)

    assert world.client("ta").get(_base(world) + path).status_code == 403


def test_the_unit_list_counts_from_the_index(world: World, root) -> None:
    """記録のある学習者と回数を出す。**本体は読まない**（本体が無くても数えられる）。"""
    world.register("teacher", Role.INSTRUCTOR)
    wrote = world.register("s2400001", Role.LEARNER)
    world.register("s2400002", Role.LEARNER)
    (first_id, second_id), versions = _two_problems(world)
    _record(world, wrote, root, [_hello(versions)])
    _record(world, wrote, root, [_hello(versions)])
    # 本体を消しても、索引だけで作る一覧は同じ数を出す。
    for path in root.rglob("*.ndjson.gz"):
        path.unlink()

    page = world.client("teacher").get(f"{_base(world)}/units").text

    assert "1 / 2 名" in page
    assert "2 回" in page
    assert f"{_base(world)}/tasks/{first_id}" in page
    assert f"{_base(world)}/tasks/{second_id}" in page
    assert f"{_base(world)}/units/{UNIT}" in page
    assert len(_audited(world)) == 1


def test_a_unit_without_the_editor_or_records_is_not_listed(world: World, root) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    _import_example(world)  # ファイルで出す課題だけ

    page = world.client("teacher").get(f"{_base(world)}/units").text

    assert "エディタで解く問題セットがありません" in page


def test_a_unit_whose_tasks_are_gone_can_still_be_opened(world: World, root) -> None:
    """記録は課題より長く残る。課題の無くなったセットにも辿れる。"""
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    _record(world, learner, root, [_hello([]), _paste(1000, 0, 300)], unit="old/set")
    client = world.client("teacher")

    listing = client.get(f"{_base(world)}/units").text
    assert f"{_base(world)}/units/old%2Fset" in listing

    page = client.get(f"{_base(world)}/units/old%2Fset")
    assert page.status_code == 200
    assert "300 字" in page.text
    assert "問題が分からない記録" in page.text


def test_an_unknown_unit_or_task_is_not_found(world: World, root) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    client = world.client("teacher")

    assert client.get(f"{_base(world)}/units/nothing").status_code == 404
    assert client.get(f"{_base(world)}/tasks/tsk_nothing").status_code == 404


def test_the_unit_page_splits_by_problem_and_sums_by_learner(world: World, root) -> None:
    """問題の表には問題ごとの人数、学習者の表にはセットの合計が出る。"""
    world.register("teacher", Role.INSTRUCTOR)
    both = world.register("s2400001", Role.LEARNER)
    one = world.register("s2400002", Role.LEARNER)
    silent = world.register("s2400003", Role.LEARNER)
    (first_id, second_id), versions = _two_problems(world)
    _record(
        world,
        both,
        root,
        [
            _hello(versions),
            _paste(1000, 0, 100),
            {"type": "tab", "t": 2000, "from": 0, "to": 1},
            _paste(3000, 1, 500),
        ],
    )
    _record(world, one, root, [_hello(versions), _paste(1000, 0, 10)])

    page = world.client("teacher").get(f"{_base(world)}/units/{UNIT}")

    assert page.status_code == 200
    problems = page.text.split("<h2>問題ごと</h2>", 1)[1].split("学習者ごと（", 1)[0]
    first_row = problems.split(first_id, 1)[1].split("</tr>", 1)[0]
    second_row = problems.split(second_id, 1)[1].split("</tr>", 1)[0]
    assert "2 名" in first_row  # 2 人とも第一の問題で手を動かした
    assert "1 名" in second_row  # 第二の問題は 1 人だけ
    learners = page.text.split("学習者ごと（", 1)[1]
    row = learners.split("s2400001", 1)[1].split("</tr>", 1)[0]
    assert "2</strong> 回・600 字" in row  # 2 問の合計
    assert "最大 500 字" in row
    # 開かなかった受講者は表に混ぜず、畳んだ欄で分かるようにする。
    assert "エディタを開かず提出もしていない受講者 1 名" in page.text
    assert str(silent.user_id) not in page.text
    assert _audited(world)[0].detail == {
        "course_id": str(world.course.id),
        "unit": UNIT,
        "sessions": 2,
    }


def test_the_task_page_shows_only_that_problem(world: World, root) -> None:
    """もう一方の問題の分は出ない。再生はその問題のタブを開く。"""
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    (first_id, second_id), versions = _two_problems(world)
    session = _record(
        world,
        learner,
        root,
        [
            _hello(versions),
            _paste(1000, 0, 100),
            {"type": "tab", "t": 2000, "from": 0, "to": 1},
            _paste(3000, 1, 500),
        ],
    )
    client = world.client("teacher")

    first = client.get(f"{_base(world)}/tasks/{first_id}").text
    second = client.get(f"{_base(world)}/tasks/{second_id}").text

    assert "100 字" in first and "500 字" not in first
    assert "500 字" in second and "100 字" not in second
    assert f"/{session.id}?tab=0" in first
    assert f"/{session.id}?tab=1" in second
    # 学習者の画面の、この問題の節へ飛べる。
    assert f"/activity/{learner.user_id}#task-{second_id}" in second
    learner_page = client.get(f"{_base(world)}/{learner.user_id}").text
    assert f'id="task-{second_id}"' in learner_page
    # 同じセットのほかの問題へ、戻らずに移れる。
    assert f"{_base(world)}/tasks/{first_id}" in second
    assert {e.detail.get("task_id") for e in _audited(world)} >= {first_id, second_id}


def test_flags_link_to_the_replay_at_that_problem(world: World, root) -> None:
    """印は種類でまとめ、回数を添える。規則は学習者の画面と同じ。"""
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    (_first, second_id), versions = _two_problems(world)
    session = _record(
        world,
        learner,
        root,
        [
            _hello(versions),
            {"type": "tab", "t": 500, "from": 0, "to": 1},
            _paste(1000, 1, 300),
            _paste(2000, 1, 400),
        ],
    )

    page = world.client("teacher").get(f"{_base(world)}/tasks/{second_id}").text

    assert f'/{session.id}?tab=1" title="300 字"' in page
    assert "大きな外からの貼り付け × 2" in page


def test_a_paste_shared_with_another_learner_is_marked(world: World, root) -> None:
    """ほかの学生と同じ内容の貼り付けも、学習者の画面と同じように目印が付く。"""
    from aijudge_ide import paste_marks

    world.register("teacher", Role.INSTRUCTOR)
    one = world.register("s2400001", Role.LEARNER)
    two = world.register("s2400002", Role.LEARNER)
    (first_id, _second), versions = _two_problems(world)
    paste = {**_paste(1000, 0, 100), "hash": "a" * 64}
    for learner in (one, two):
        session = _record(world, learner, root, [_hello(versions), paste])
        with world.database.unit_of_work() as uow:
            uow.ide_activity.add_paste_marks(paste_marks(session, 0, [paste]))
            uow.commit()

    page = world.client("teacher").get(f"{_base(world)}/tasks/{first_id}").text

    assert "ほかの学生と同じ内容の貼り付け" in page
    assert "ほかの学生 1 人も同じ内容" in page


def test_someone_not_enrolled_is_not_counted(world: World, root) -> None:
    """受講していない人（教員の動作確認など）の記録は、全員分の画面に混ぜない。"""
    teacher = world.register("teacher", Role.INSTRUCTOR)
    (first_id, _second), versions = _two_problems(world)
    _record(world, teacher, root, [_hello(versions), _paste(1000, 0, 777)])
    client = world.client("teacher")

    assert "777 字" not in client.get(f"{_base(world)}/units/{UNIT}").text
    assert "777 字" not in client.get(f"{_base(world)}/tasks/{first_id}").text
    assert "なし" in client.get(f"{_base(world)}/units").text


def test_a_malformed_record_does_not_take_the_page_down(world: World, root) -> None:
    """1 人の記録の形が悪くても全員分は見える。その人の行に「読めない記録」と出る。"""
    world.register("teacher", Role.INSTRUCTOR)
    hostile = world.register("s2400001", Role.LEARNER)
    fine = world.register("s2400002", Role.LEARNER)
    (first_id, _second), versions = _two_problems(world)
    # 受け口は形の一部しか確かめない（`check_events`）ので、こういう記録は入りうる。
    _record(
        world,
        hostile,
        root,
        [_hello(versions), {"type": "paste", "t": 1000, "tab": 0, "len": "many"}],
    )
    _record(world, fine, root, [_hello(versions), _paste(1000, 0, 123)])
    client = world.client("teacher")

    for path in (f"/units/{UNIT}", f"/tasks/{first_id}"):
        page = client.get(_base(world) + path)
        assert page.status_code == 200
        assert "123 字" in page.text
    unit_page = client.get(f"{_base(world)}/units/{UNIT}").text
    assert "読めない記録 1" in unit_page.split("s2400001", 1)[1].split("</tr>", 1)[0]


def test_hello_tasks_of_the_wrong_shape_do_not_take_the_page_down(world: World, root) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    _two_problems(world)
    _record(world, learner, root, [{**_hello([]), "tasks": 5}, _paste(1000, 0, 321)])

    page = world.client("teacher").get(f"{_base(world)}/units/{UNIT}")

    assert page.status_code == 200
    assert "問題が分からない記録" in page.text
    assert "321 字" in page.text


def test_a_broken_body_is_counted_and_read_again_later(world: World, root) -> None:
    """読めなかった本体は「読めない記録」と出し、**結果を覚えない**（直れば読める）。"""
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    (first_id, _second), versions = _two_problems(world)
    _record(world, learner, root, [_hello(versions), _paste(1000, 0, 123)])
    target = next(root.rglob("*.ndjson.gz"))
    body = target.read_bytes()
    target.write_bytes(body[: len(body) // 2])  # 途中で切れた gzip
    client = world.client("teacher")

    broken = client.get(f"{_base(world)}/tasks/{first_id}")
    assert broken.status_code == 200
    assert "123 字" not in broken.text
    assert "読めない記録 1" in client.get(f"{_base(world)}/units/{UNIT}").text

    target.write_bytes(body)
    assert "123 字" in client.get(f"{_base(world)}/tasks/{first_id}").text


def test_a_new_batch_is_read_even_after_the_first_view(world: World, root) -> None:
    """読み取りを覚えていても、開いている途中のセッションの続きは読む。"""
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    (first_id, _second), versions = _two_problems(world)
    session = _record(world, learner, root, [_hello(versions), _paste(1000, 0, 100)])
    client = world.client("teacher")
    assert "100 字" in client.get(f"{_base(world)}/tasks/{first_id}").text

    later = [_paste(5000, 0, 900)]
    path, digest, size = ActivityFiles(root).write_batch(session, 1, later, {})
    with world.database.unit_of_work() as uow:
        uow.ide_activity.add_batch(
            EventBatch(
                ide_session_id=session.id,
                seq=1,
                received_at=NOW,
                event_count=1,
                snapshot_count=0,
                byte_size=size,
                sha256=digest,
                path=path,
            )
        )
        uow.commit()

    assert "1000 字" in client.get(f"{_base(world)}/tasks/{first_id}").text


def test_unknown_records_are_not_silently_dropped_from_the_task_page(world: World, root) -> None:
    """どの問題か分からない記録は問題の数字に入らない。**そう言う。**"""
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    (first_id, _second), _versions = _two_problems(world)
    _record(world, learner, root, [_hello([]), _paste(1000, 0, 321)])

    page = world.client("teacher").get(f"{_base(world)}/tasks/{first_id}").text

    assert "どの問題の作業か分からない記録が 1 名分" in page
    assert "321 字" not in page


def test_without_a_configured_place_no_numbers_are_shown(
    world: World, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """**読めないのに 0 字と書かない。** 数えられないことと 0 は別のこと。"""
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    (first_id, _second), versions = _two_problems(world)
    _record(world, learner, tmp_path / "somewhere", [_hello(versions), _paste(1000, 0, 100)])
    monkeypatch.delenv("AIJUDGE_ACTIVITY_DIR", raising=False)
    client = world.client("teacher")

    for path in (f"/units/{UNIT}", f"/tasks/{first_id}"):
        page = client.get(_base(world) + path)
        assert page.status_code == 200
        assert "AIJUDGE_ACTIVITY_DIR" in page.text
        assert " 字" not in page.text


def test_a_file_submission_is_listed_without_claiming_zero_typing(world: World, root) -> None:
    """ファイルで出した人は、打鍵が 0 字なのではなく記録が無い。提出はリンクで開ける。"""
    from aijudge_core import ArtifactKind
    from aijudge_submission import FilesystemArtifactStore, IncomingFile, SubmissionService

    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    (first_id, _second), versions = _two_problems(world)
    service = SubmissionService(world.database.unit_of_work, FilesystemArtifactStore(root / "a"))
    accepted = service.accept(
        tenant_id=world.course.tenant_id,
        task_version_id=TaskVersionId(versions[0]),
        learner_id=learner.user_id,
        subject_profile="cs_lang_c_intro",
        files=[IncomingFile(filename="main.c", kind=ArtifactKind.CODE, payload=START.encode())],
    )

    page = world.client("teacher").get(f"{_base(world)}/tasks/{first_id}").text

    row = page.split("s2400001", 1)[1].split("</tr>", 1)[0]
    assert "エディタの記録なし" in row
    assert " 字" not in row
    assert f"/review/{accepted.submission.id}/reveal" in row
    assert "1 回" in row


def test_sorting_changes_only_the_order(world: World, root) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    few = world.register("s2400001", Role.LEARNER)
    many = world.register("s2400002", Role.LEARNER)
    (first_id, _second), versions = _two_problems(world)
    _record(world, few, root, [_hello(versions), _paste(1000, 0, 10)])
    _record(world, many, root, [_hello(versions), _paste(1000, 0, 900)])
    client = world.client("teacher")

    for path in (f"/units/{UNIT}", f"/tasks/{first_id}"):
        by_login = client.get(_base(world) + path).text
        by_paste = client.get(f"{_base(world)}{path}?sort=paste").text
        assert by_login.index("s2400001") < by_login.index("s2400002")
        assert by_paste.index("s2400002") < by_paste.index("s2400001")
        # 知らない値は既定の並びに戻す。
        odd = client.get(f"{_base(world)}{path}?sort=nonsense")
        assert odd.status_code == 200
        assert odd.text.index("s2400001") < odd.text.index("s2400002")


def test_a_hostile_unit_name_is_escaped(world: World, root) -> None:
    """`unit` はクライアントが送る。一覧にそのまま出しても、タグとして読まれない。"""
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    _record(world, learner, root, [_hello([])], unit="<img src=x>")

    page = world.client("teacher").get(f"{_base(world)}/units").text

    assert "<img src=x>" not in page
    assert "&lt;img src=x&gt;" in page
