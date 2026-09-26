"""課題定義の日時はタイムゾーン付きで書く（#401）。

YAML の `due_at: 2026-09-25 23:59` は素の値になる。そのまま入れると
採点の段で TypeError になるので、`course apply` の時点で拒む。
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from aijudge_authoring import TaskSpec


@pytest.mark.parametrize(
    "written",
    [
        "2026-09-25 23:59",  # YAML では文字列、pydantic が素の日時に読む
        "2026-09-25 23:59:00",  # YAML の timestamp として素の datetime になる
        "2026-09-25T23:59:00",
    ],
)
@pytest.mark.parametrize(
    "field",
    ["opens_at", "due_at", "submissions_open_at", "grading_starts_at", "accepts_until"],
)
def test_a_naive_time_from_yaml_is_refused(written: str, field: str) -> None:
    document = yaml.safe_load(f"key: ex1/p1\nstatement: 本文\n{field}: {written}\n")
    with pytest.raises(ValidationError, match="timezone"):
        TaskSpec.model_validate(document)


def test_a_deadline_with_an_offset_is_accepted() -> None:
    document = yaml.safe_load("key: ex1/p1\nstatement: 本文\ndue_at: 2026-09-25T23:59:00+09:00\n")
    spec = TaskSpec.model_validate(document)
    assert spec.due_at is not None and spec.due_at.utcoffset() is not None
