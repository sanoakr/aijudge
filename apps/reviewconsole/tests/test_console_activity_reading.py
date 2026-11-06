"""作業の記録の量が多いときは裏で読み、すぐに進み具合を返す（2026-11-06）。

本番の prog2 ex07 は 1 回の表示に 111〜167 秒かかり、その間ブラウザに何も出なかった。
教員は壊れたと思って別の問題を押し、同じ読み込みが何本も並んだ。固定したいのは次のこと。

すぐ返す       読み終わるのを待たず、進み具合の画面を返す。学習者の情報は出さない。
1 本だけ       何度開いても、同じセットを読むのは 1 本。
切り替わる     読み終われば同じ URL が本来の画面になり、そこで監査ログに残る。
止まらない     読めない束があっても、読み込み中のまま終わらない画面にしない。
倒れたら見える 読み込み自体が倒れたら、次の要求で本当の原因が見える。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from test_console_activity_by_problem import (
    UNIT,
    _audited,
    _base,
    _hello,
    _paste,
    _record,
    _two_problems,
)
from test_console_activity_by_problem import (
    root as root,  # フィクスチャを借りる
)
from test_manage import World
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import Role
from aijudge_reviewconsole import activity_by_problem

READING = "全員分の記録を読んでいます"
TIMEOUT_SECONDS = 10.0


def _eventually(condition: Callable[[], bool]) -> None:
    limit = time.monotonic() + TIMEOUT_SECONDS
    while time.monotonic() < limit:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("読み込みが終わりませんでした")


@pytest.fixture
def background(monkeypatch: pytest.MonkeyPatch) -> None:
    """どんなに少なくても裏で読ませる。"""
    monkeypatch.setattr(activity_by_problem, "INLINE_BATCHES", 0)


def test_a_small_unit_is_read_on_the_spot(world: World, root) -> None:
    """少ないときに、1 回余計に待たせない。"""
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    (first_id, _second), versions = _two_problems(world)
    _record(world, learner, root, [_hello(versions), _paste(1000, 0, 100)])

    page = world.client("teacher").get(f"{_base(world)}/tasks/{first_id}").text

    assert READING not in page
    assert "100 字" in page


def test_a_large_unit_answers_at_once_then_becomes_the_page(
    world: World, root, background, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    (first_id, _second), versions = _two_problems(world)
    _record(world, learner, root, [_hello(versions), _paste(1000, 0, 100)])
    release = threading.Event()
    reads: list[str] = []
    real = activity_by_problem._read_digest

    def slow(files, session, batches):
        reads.append(str(session.id))
        assert release.wait(TIMEOUT_SECONDS)
        return real(files, session, batches)

    monkeypatch.setattr(activity_by_problem, "_read_digest", slow)
    client = world.client("teacher")
    url = f"{_base(world)}/tasks/{first_id}"

    waiting = client.get(url)

    # すぐ返る。読み終わるまで待っていれば、ここへ来ない（release を引いていない）。
    assert waiting.status_code == 200
    assert READING in waiting.text
    assert 'http-equiv="refresh"' in waiting.text
    # 学習者の情報は出ず、見せていないので監査ログにも残らない。
    assert "s2400001" not in waiting.text
    assert not _audited(world)

    # 別の問題・問題セットの画面を開いても、読み込みは増えない。
    assert READING in client.get(f"{_base(world)}/units/{UNIT}").text
    assert READING in client.get(url).text
    _eventually(lambda: bool(reads))
    assert len(reads) == 1

    release.set()
    _eventually(lambda: READING not in client.get(url).text)
    page = client.get(url).text

    assert "100 字" in page
    assert 'http-equiv="refresh"' not in page
    assert _audited(world)
    assert len(reads) == 1


def test_an_unreadable_batch_does_not_keep_the_page_reading(
    world: World, root: Path, background
) -> None:
    """読めない分は覚えない。それを未読と数え続けると、画面が永久に切り替わらない。"""
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    _ids, versions = _two_problems(world)
    session = _record(world, learner, root, [_hello(versions), _paste(1000, 0, 100)])
    with world.database.unit_of_work() as uow:
        batch = uow.ide_activity.batches(session.id)[0]
    (root / batch.path).write_bytes(b"not gzip")
    client = world.client("teacher")
    url = f"{_base(world)}/units/{UNIT}"

    assert READING in client.get(url).text
    _eventually(lambda: READING not in client.get(url).text)

    assert "読めない記録" in client.get(url).text


def test_a_crashed_reader_does_not_hide_behind_the_waiting_page(
    world: World, root, background, monkeypatch: pytest.MonkeyPatch
) -> None:
    """裏のスレッドが倒れても、進み具合の画面のままにしない。次からは読み直す。"""
    world.register("teacher", Role.INSTRUCTOR)
    learner = world.register("s2400001", Role.LEARNER)
    (first_id, _second), versions = _two_problems(world)
    _record(world, learner, root, [_hello(versions), _paste(1000, 0, 100)])
    real = activity_by_problem._read_digest
    crashed = threading.Event()

    def broken(files, session, batches):
        crashed.set()
        raise RuntimeError("裏で倒れた")

    monkeypatch.setattr(activity_by_problem, "_read_digest", broken)
    client = world.client("teacher")
    url = f"{_base(world)}/tasks/{first_id}"

    assert READING in client.get(url).text
    assert crashed.wait(TIMEOUT_SECONDS)
    # 倒れた後は覆わずにその場で読む。ここでは読み手を元に戻して、画面が開くことを見る。
    monkeypatch.setattr(activity_by_problem, "_read_digest", real)
    _eventually(lambda: READING not in client.get(url).text)

    assert "100 字" in client.get(url).text
