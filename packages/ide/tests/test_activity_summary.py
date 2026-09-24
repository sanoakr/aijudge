"""行動記録の要約（`aijudge_ide.summary`）。数え方だけを固定する（判定はしない）。"""

from __future__ import annotations

from aijudge_ide import summarize


def test_typing_pasting_and_leaving_are_counted_apart() -> None:
    events = [
        {"type": "hello", "t": 0},
        {"type": "edit", "t": 100, "ins": "int"},
        {"type": "edit", "t": 200, "ins": "x" * 120},
        {"type": "paste", "t": 210, "len": 120, "origin": "external"},
        {"type": "edit", "t": 300, "ins": "ab"},
        {"type": "paste", "t": 310, "len": 2, "origin": "internal"},
        {"type": "copy", "t": 400, "from": "statement"},
        {"type": "blur", "t": 1_000},
        {"type": "focus", "t": 31_000},
        {"type": "visibility", "t": 40_000, "state": "hidden"},
        {"type": "visibility", "t": 50_000, "state": "visible"},
        {"type": "run", "t": 60_000, "stage": "request"},
        {"type": "run", "t": 61_000, "stage": "result"},
        {"type": "submit", "t": 70_000},
    ]

    s = summarize(events)

    assert s.typed_chars == 3
    assert (s.pastes_external, s.pasted_external_chars, s.largest_external_paste) == (1, 120, 120)
    assert s.pastes_internal == 1
    assert s.copies_from_statement == 1
    assert (s.away_count, s.away_seconds) == (2, 40.0)
    assert (s.runs, s.submits) == (1, 1)
    assert s.duration_seconds == 70.0


def test_leaving_without_coming_back_counts_until_the_end() -> None:
    s = summarize(
        [
            {"type": "hello", "t": 0},
            {"type": "blur", "t": 5_000},
            {"type": "heartbeat", "t": 65_000},
        ]
    )
    assert (s.away_count, s.away_seconds) == (1, 60.0)
