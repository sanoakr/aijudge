"""課題定義の `exit_code`（2026-10-18）。

ケースごとに終了コードの期待（`zero`・`nonzero`・`any`）を書ける。既定の `zero` は
payload に**書かない** ── 書くと、終了コードを気にしない既存の課題まで中身が
変わり、取り込み直しで版が上がる。
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from aijudge_authoring import TaskSpec, build_task_version
from aijudge_core.ids import CourseId, UserId

AUTHOR = UserId("usr_" + "1" * 32)
COURSE = CourseId("crs_" + "1" * 32)


def _payloads(document: str) -> dict[str, dict[str, object]]:
    spec = TaskSpec.model_validate(yaml.safe_load(document))
    version = build_task_version(
        spec, course_id=COURSE, subject_profile="cs_lang_c_intro", authored_by=AUTHOR
    )
    return {case.name: dict(case.payload) for case in version.test_cases}


def test_the_expectation_reaches_the_payload_only_when_it_is_not_the_default() -> None:
    payloads = _payloads(
        "key: ex1/p1\n"
        "statement: 本文\n"
        "test_cases:\n"
        '  - {name: ok, input: "3\\n", expected: "9\\n"}\n'
        '  - {name: bad, input: "x\\n", expected: "", exit_code: nonzero}\n'
        '  - {name: either, input: "", expected: "", exit_code: any}\n'
    )
    assert payloads["ok"] == {"input": "3\n", "expected": "9\n"}
    assert payloads["bad"] == {"input": "x\n", "expected": "", "exit_code": "nonzero"}
    assert payloads["either"]["exit_code"] == "any"


def test_an_unknown_expectation_is_refused_at_load() -> None:
    """採点の段まで持ち込まない。評価器は知らない値を `zero` に倒すが、
    書いた人が気づけるのは読み込みのときだけである。"""
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(
            yaml.safe_load(
                "key: ex1/p1\nstatement: 本文\n"
                "test_cases:\n  - {name: a, input: '', expected: '', exit_code: 1}\n"
            )
        )


def test_exit_code_beside_a_payload_is_refused() -> None:
    """2 か所に書かせない。payload を持つ宣言は、その中に書く。"""
    with pytest.raises(ValidationError, match="payload"):
        TaskSpec.model_validate(
            yaml.safe_load(
                "key: ex1/p1\nstatement: 本文\n"
                "test_cases:\n"
                "  - {name: a, payload: {input: '', expected: ''}, exit_code: nonzero}\n"
            )
        )
