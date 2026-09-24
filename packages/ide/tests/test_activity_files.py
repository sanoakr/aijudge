"""行動記録の本体の書き方と、受け口の形の検査（ADR 0023）。"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_core.ids import CourseId, TenantId, UserId
from aijudge_ide import (
    MAX_SOURCE_BYTES,
    ActivityFiles,
    ActivityRejected,
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
