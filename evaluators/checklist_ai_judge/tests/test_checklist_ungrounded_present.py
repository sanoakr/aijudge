"""行を引けない「ある」は満たした項目として数えない（P4）。

モデルが「ある」と答えても、提出物に実在する行を示せなければ根拠にならない。
数えれば、根拠の無い満点が確信度 1.0 で付き、自動確定の経路に乗る。
"""

from __future__ import annotations

from test_checklist_ai_judge import _item_case, _judge, _request

ITEMS = ("目的", "方法", "結果")


def _all_present(line: int | None) -> dict:
    findings = []
    for name in ITEMS:
        finding: dict = {"item": name, "present": True, "quote": "x"}
        if line is not None:
            finding["line"] = line
        findings.append(finding)
    return {"findings": findings}


def _outcome(response: dict):
    request = _request(cases=tuple(_item_case(name) for name in ITEMS))
    return _judge(response, response, response).evaluate(request)


def test_present_with_line_zero_is_not_counted() -> None:
    score = _outcome(_all_present(0)).scores[0]
    assert score.level == 0
    assert score.score_ratio == 0.0


def test_present_without_a_line_is_not_counted() -> None:
    score = _outcome(_all_present(None)).scores[0]
    assert score.level == 0


def test_present_beyond_the_last_line_is_not_counted() -> None:
    score = _outcome(_all_present(9999)).scores[0]
    assert score.level == 0


def test_the_rationale_does_not_claim_the_items_are_there() -> None:
    """「すべてあります」と「見つからなかった」が同じ採点に並ばない。"""
    score = _outcome(_all_present(0)).scores[0]
    assert "すべてあります" not in score.rationale
    notes = " ".join((e.note or "") for e in score.evidence)
    assert score.level == 0 or "見つからなかった" not in notes
