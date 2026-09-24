"""行動記録の欠落と食い違いの数え方（`aijudge_ide.integrity`）。

判断を加えずに事実として教員に示すためのもの。ここで固定するのは数え方で、
それをどう読むかではない。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from aijudge_ide import EventBatch, IdeSessionId, check_session

SESSION = IdeSessionId("ide_" + "5" * 32)
NOW = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)


def h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def batch(seq: int, *, skew: float = 0.0) -> EventBatch:
    received = NOW + timedelta(seconds=10 * seq)
    return EventBatch(
        ide_session_id=SESSION,
        seq=seq,
        received_at=received,
        client_time=received - timedelta(seconds=skew),
        event_count=0,
        snapshot_count=0,
        byte_size=0,
        sha256="0" * 64,
        path="x",
    )


def edit(t: float, off: int, ins: str = "", delete: int = 0, tab: int = 0) -> dict:
    return {"type": "edit", "t": t, "tab": tab, "off": off, "del": delete, "ins": ins}


def store(*texts: str):
    known = {h(text): text for text in texts}
    return known.get


def test_a_faithful_record_rebuilds_to_the_reported_content() -> None:
    start, end = "int main(){}", "int main(){return 0;}"
    events = [
        {"type": "hello", "t": 0, "hashes": [h(start)]},
        edit(100, 11, "return 0;"),
        {"type": "tabs", "t": 200, "hashes": [h(end)]},
    ]

    report = check_session([(batch(0), events)], store(start))

    assert report.complete
    assert (report.checks, report.matched) == (1, 1)


def test_content_the_edits_do_not_explain_is_a_mismatch() -> None:
    """**差分に無い内容が現れた。** 記録か内容のどちらかが改変された印。"""
    start = "int main(){}"
    events = [
        {"type": "hello", "t": 0, "hashes": [h(start)]},
        edit(100, 11, "x"),
        {"type": "tabs", "t": 200, "hashes": [h("something else entirely")]},
    ]

    report = check_session([(batch(0), events)], store(start))

    assert report.mismatched == 1
    assert report.first_mismatch_ms == 200


def test_a_missing_batch_is_counted_and_makes_the_next_check_unverifiable() -> None:
    """欠けた束の差分が無いので、次の全文までは確かめられない（食い違いではない）。"""
    start, later = "a", "abc"
    first = [{"type": "hello", "t": 0, "hashes": [h(start)]}, edit(10, 1, "b")]
    third = [edit(30_000, 2, "c"), {"type": "tabs", "t": 30_100, "hashes": [h(later)]}]

    report = check_session([(batch(0), first), (batch(2), third)], store(start))

    assert report.missing_seqs == [1]
    assert report.unverifiable == 1 and report.mismatched == 0
    assert not report.complete


def test_a_snapshot_after_a_gap_restores_checking() -> None:
    start, middle, end = "a", "abc", "abcd"
    first = [{"type": "hello", "t": 0, "hashes": [h(start)]}]
    third = [
        {"type": "tabs", "t": 30_000, "hashes": [h(middle)]},
        edit(31_000, 3, "d"),
        {"type": "tabs", "t": 32_000, "hashes": [h(end)]},
    ]

    report = check_session([(batch(0), first), (batch(2), third)], store(start, middle))

    assert (report.unverifiable, report.matched, report.mismatched) == (1, 1, 0)


def test_a_file_load_replaces_the_content_wholesale() -> None:
    start, loaded = "", "#include <stdio.h>\n"
    events = [
        {"type": "hello", "t": 0, "hashes": [h(start)]},
        {"type": "file_load", "t": 50, "tab": 0, "hash": h(loaded)},
        {"type": "submit", "t": 60, "tab": 0, "hash": h(loaded)},
    ]

    report = check_session([(batch(0), events)], store(start, loaded))

    assert report.complete and report.matched == 1


def test_multi_cursor_edits_replay_from_the_end() -> None:
    """複数カーソルの変更は、後ろの範囲から順に記録される（画面側で並べ替える）。"""
    start, end = "ab", "XaXb"
    events = [
        {"type": "hello", "t": 0, "hashes": [h(start)]},
        edit(10, 1, "X"),
        edit(10, 0, "X"),
        {"type": "tabs", "t": 20, "hashes": [h(end)]},
    ]

    assert check_session([(batch(0), events)], store(start)).matched == 1


def test_long_silences_are_listed() -> None:
    events = [
        {"type": "hello", "t": 0, "hashes": []},
        {"type": "heartbeat", "t": 30_000},
        {"type": "heartbeat", "t": 150_000},
    ]

    report = check_session([(batch(0), events)], store())

    assert [(s.start_ms, s.end_ms) for s in report.silences] == [(30_000, 150_000)]
    assert report.silences[0].seconds == 120


def test_the_pc_clock_offset_is_estimated() -> None:
    report = check_session(
        [(batch(0, skew=3.0), []), (batch(1, skew=5.0), []), (batch(2, skew=4.0), [])],
        store(),
    )
    assert report.clock_offset_seconds == 4.0
