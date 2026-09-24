"""行動記録の印（`aijudge_ide.flags`）。**判定ではなく、確かめる場所の目印。**

印が付くべき場面と、**付いてはいけない場面**の両方を固定する。ふつうの書き方で
印が並ぶと、教員は印を読まなくなる（誤検知の不利益）。
"""

from __future__ import annotations

from aijudge_ide import FlagKind, flag_events, submission_mismatches


def kinds(events) -> list[FlagKind]:
    return [flag.kind for flag in flag_events(events)]


def test_a_small_or_internal_paste_is_not_flagged() -> None:
    events = [
        {"type": "paste", "t": 1_000, "len": 40, "origin": "external"},
        {"type": "paste", "t": 2_000, "len": 900, "origin": "internal"},
    ]
    assert kinds(events) == []


def test_a_large_external_paste_is_flagged() -> None:
    flags = flag_events([{"type": "paste", "t": 5_000, "len": 450, "origin": "external"}])
    assert [f.kind for f in flags] == [FlagKind.LARGE_EXTERNAL_PASTE]
    assert flags[0].t == 5_000 and "450" in flags[0].detail


def test_a_large_paste_right_after_returning_is_told_apart() -> None:
    events = [
        {"type": "blur", "t": 1_000},
        {"type": "focus", "t": 60_000},
        {"type": "paste", "t": 65_000, "len": 300, "origin": "external"},
    ]
    flags = flag_events(events)
    assert [f.kind for f in flags] == [FlagKind.PASTE_AFTER_RETURN]
    assert "5 秒後" in flags[0].detail


def test_copying_the_statement_leaving_and_pasting_back_is_one_mark() -> None:
    events = [
        {"type": "copy", "t": 1_000, "from": "statement", "len": 300},
        {"type": "visibility", "t": 2_000, "state": "hidden"},
        {"type": "visibility", "t": 120_000, "state": "visible"},
        {"type": "paste", "t": 125_000, "len": 120, "origin": "external"},
    ]
    assert kinds(events) == [FlagKind.STATEMENT_ROUNDTRIP]


def test_copying_the_statement_without_leaving_is_not_a_roundtrip() -> None:
    events = [
        {"type": "copy", "t": 1_000, "from": "statement", "len": 30},
        {"type": "paste", "t": 3_000, "len": 120, "origin": "external"},
    ]
    assert FlagKind.STATEMENT_ROUNDTRIP not in kinds(events)


def test_a_bulk_insert_without_a_paste_is_flagged_but_a_paired_one_is_not() -> None:
    lone = [{"type": "edit", "t": 10_000, "tab": 0, "off": 0, "del": 0, "ins": "x" * 120}]
    paired = [
        {"type": "edit", "t": 10_000, "tab": 0, "off": 0, "del": 0, "ins": "x" * 120},
        {"type": "paste", "t": 10_400, "len": 120, "origin": "internal"},
    ]
    loaded = [
        {"type": "edit", "t": 10_000, "tab": 0, "off": 0, "del": 0, "ins": "x" * 120},
        {"type": "file_load", "t": 10_100, "tab": 0},
    ]
    assert kinds(lone) == [FlagKind.BULK_INSERT]
    assert kinds(paired) == []
    assert kinds(loaded) == []


def test_undo_does_not_count_as_a_bulk_insert() -> None:
    events = [
        {"type": "edit", "t": 1, "tab": 0, "off": 0, "del": 0, "ins": "x" * 200, "undo": True}
    ]
    assert kinds(events) == []


def test_sustained_inhuman_typing_is_flagged_once() -> None:
    # 20 ms おき（1 秒 50 字）に 400 字。
    events = [
        {"type": "edit", "t": i * 20, "tab": 0, "off": i, "del": 0, "ins": "a"} for i in range(400)
    ]
    flags = flag_events(events)
    assert [f.kind for f in flags].count(FlagKind.FAST_TYPING) == 1


def test_fast_but_human_typing_is_not_flagged() -> None:
    # 100 ms おき（1 秒 10 字）に 600 字 ── 速い人の打鍵。
    events = [
        {"type": "edit", "t": i * 100, "tab": 0, "off": i, "del": 0, "ins": "a"} for i in range(600)
    ]
    assert kinds(events) == []


def test_a_submission_that_differs_from_the_record_is_flagged() -> None:
    events = [
        {"type": "submit", "t": 9_000, "tab": 0, "submission_id": "sub_a", "hash": "1" * 64},
        {"type": "submit", "t": 9_500, "tab": 0, "submission_id": "sub_b", "hash": "2" * 64},
    ]
    flags = submission_mismatches(events, {"sub_a": "1" * 64, "sub_b": "3" * 64})
    assert [(f.kind, f.detail) for f in flags] == [(FlagKind.SUBMISSION_MISMATCH, "sub_b")]


def test_labels_state_facts_not_verdicts() -> None:
    """**「不正」「疑い」と書かない。**"""
    from aijudge_ide import FLAG_LABELS

    for label in FLAG_LABELS.values():
        assert "不正" not in label and "疑" not in label
