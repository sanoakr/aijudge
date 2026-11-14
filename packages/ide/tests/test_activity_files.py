"""行動記録の本体の書き方と、受け口の形の検査（ADR 0023）。"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_core.ids import CourseId, TenantId, UserId
from aijudge_ide import (
    MAX_SOURCE_BYTES,
    ActivityFiles,
    ActivityRejected,
    ActivityTampered,
    IdeSession,
    IdeSessionId,
    check_events,
    check_snapshots,
    snapshot_name,
)

NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
SESSION = IdeSession(
    id=IdeSessionId("ide_" + "5" * 32),
    tenant_id=TenantId("ten_" + "0" * 32),
    learner_id=UserId("usr_" + "1" * 32),
    course_id=CourseId("crs_" + "3" * 32),
    unit="../第3回/配列",
    started_at=NOW,
    consented_at=NOW,
)


def test_a_batch_is_written_as_gzipped_ndjson(tmp_path: Path) -> None:
    files = ActivityFiles(tmp_path)
    events = [{"type": "edit", "t": 12.5, "text": "for"}, {"type": "paste", "t": 30, "len": 3}]

    path, digest, size = files.write_batch(SESSION, 7, events, {})

    written = tmp_path / path
    assert written.name == "00000007.ndjson.gz"
    lines = gzip.decompress(written.read_bytes()).decode().splitlines()
    assert [json.loads(line) for line in lines] == events
    assert size == len(gzip.decompress(written.read_bytes()))
    assert len(digest) == 64


def test_an_existing_batch_is_never_replaced(tmp_path: Path) -> None:
    """同じ seq を別の中身で書いても、最初の本体と、その指紋・大きさが返る。"""
    files = ActivityFiles(tmp_path)
    first = [{"type": "paste", "t": 2, "len": 120, "origin": "external"}]
    path, digest, size = files.write_batch(SESSION, 0, first, {})
    stored = (tmp_path / path).read_bytes()

    again = files.write_batch(SESSION, 0, [{"type": "heartbeat", "t": 3}], {})

    assert again == (path, digest, size)
    assert (tmp_path / path).read_bytes() == stored
    assert files.read_batch(path, digest) == first
    # 一時ファイルを残さない。
    assert [p.name for p in (tmp_path / path).parent.iterdir()] == ["00000000.ndjson.gz"]


def test_a_file_without_an_index_row_is_indexed_from_its_own_contents(tmp_path: Path) -> None:
    """ファイルを書いた直後に落ちて索引が無いとき、再送は既存のファイルで索引を作る。"""
    files = ActivityFiles(tmp_path)
    original = [{"type": "edit", "t": 1, "text": "a"}]
    path, _, _ = files.write_batch(SESSION, 4, original, {})
    body = gzip.decompress((tmp_path / path).read_bytes())

    _, digest, size = files.write_batch(SESSION, 4, [{"type": "heartbeat", "t": 9}], {})

    assert digest == hashlib.sha256(body).hexdigest()
    assert size == len(body)


def test_a_body_that_does_not_match_its_index_is_refused(tmp_path: Path) -> None:
    """置き場所で直接置き換えられた本体は、索引の指紋と照合して読まない。"""
    files = ActivityFiles(tmp_path)
    path, digest, _ = files.write_batch(SESSION, 0, [{"type": "edit", "t": 1, "text": "a"}], {})
    (tmp_path / path).write_bytes(gzip.compress(b'{"type":"heartbeat","t":1}\n'))

    with pytest.raises(ActivityTampered):
        files.read_batch(path, digest)
    assert files.read_batch(path) == [{"type": "heartbeat", "t": 1}]


def test_the_unit_name_cannot_escape_the_root(tmp_path: Path) -> None:
    """問題セットの名前に `../` や `/` があっても、根の外に書かない。"""
    files = ActivityFiles(tmp_path)

    path, _, _ = files.write_batch(SESSION, 0, [{"type": "hello", "t": 0}], {})

    assert (tmp_path / path).resolve().is_relative_to(tmp_path.resolve())
    assert len(Path(path).parts) == 6  # course/unit/learner/session/events/file


def test_snapshots_are_named_by_their_content(tmp_path: Path) -> None:
    files = ActivityFiles(tmp_path)
    text = "int main(void){}\n"

    files.write_batch(SESSION, 0, [], {snapshot_name(text): text})

    snapshot = files.session_dir(SESSION) / "snapshots" / f"{snapshot_name(text)}.txt"
    assert snapshot.read_text() == text


def test_no_temporary_files_are_left_behind(tmp_path: Path) -> None:
    files = ActivityFiles(tmp_path)
    files.write_batch(SESSION, 0, [{"type": "hello", "t": 0}], {})
    assert not [p for p in tmp_path.rglob(".tmp-*")]


@pytest.mark.parametrize(
    "events",
    [
        "not a list",
        [42],
        [{"type": "keylogger", "t": 1}],
        [{"type": "edit"}],
        [{"type": "edit", "t": -1}],
        [{"type": "edit", "t": True}],
    ],
)
def test_malformed_events_are_refused(events: object) -> None:
    """**知らない種類は受け取らない** ── 何を記録するかを告知で約束できなくなる。"""
    with pytest.raises(ActivityRejected):
        check_events(events)


def test_a_snapshot_must_be_named_by_its_hash() -> None:
    """鍵と中身が食い違うと、後で「このハッシュの内容」が別物になる。"""
    with pytest.raises(ActivityRejected):
        check_snapshots({"0" * 64: "text"})
    with pytest.raises(ActivityRejected):
        big = "a" * (MAX_SOURCE_BYTES + 1)
        check_snapshots({snapshot_name(big): big})
    assert check_snapshots({snapshot_name("x"): "x"}) == {snapshot_name("x"): "x"}


def test_what_was_written_reads_back(tmp_path: Path) -> None:
    files = ActivityFiles(tmp_path)
    events = [{"type": "hello", "t": 0, "hashes": []}]
    text = "print(1)\n"
    path, _, _ = files.write_batch(SESSION, 0, events, {snapshot_name(text): text})

    assert files.read_batch(path) == events
    assert files.read_snapshot(SESSION, snapshot_name(text)) == text
    assert files.read_snapshot(SESSION, "f" * 64) is None
    # 指紋の形でない名前では、ファイルを探しに行かない。
    assert files.read_snapshot(SESSION, "../../etc/passwd") is None


def test_reading_outside_the_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ActivityFiles(tmp_path / "root").read_batch("../elsewhere.ndjson.gz")
