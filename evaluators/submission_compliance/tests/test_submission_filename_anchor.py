"""ファイル名の規則は**先頭から**当てること（#703）。

`_filename` は `re.match` で当てる。既存の試験の規則はどれも `^...$` で両端を
固定しているので、`search` に変えても同じ結果になり、違いを見ていなかった
（#703 のレビューの記録）。教員が `^` を書き忘れた規則（`Y\\d{6}\\.pdf`）でも、
`x_Y230001.pdf` のように前に何か付いた名前は規則どおりと見なさない。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aijudge_eval_submission_compliance import _filename

from aijudge_core import Artifact, ArtifactKind, ArtifactRole
from aijudge_core.ids import ArtifactId, SubmissionId

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
# `^` を書いていない規則。
UNANCHORED = r"[YT]\d{6}\.pdf"


def _artifact(filename: str) -> Artifact:
    return Artifact(
        id=ArtifactId("art_" + "3" * 32),
        submission_id=SubmissionId("sub_" + "4" * 32),
        role=ArtifactRole.ORIGINAL,
        kind=ArtifactKind.PDF,
        storage_key="k",
        content_hash="sha256:x",
        byte_size=10,
        filename=filename,
        created_at=NOW,
    )


@pytest.mark.parametrize("filename", ["x_Y230001.pdf", "report Y230001.pdf"])
def test_a_name_with_something_before_the_rule_is_a_fault(filename: str) -> None:
    faults, reason = _filename((_artifact(filename),), {"filename_pattern": UNANCHORED})
    assert faults == 1
    assert "ファイル名が規則に合いません" in reason


def test_a_name_that_starts_with_the_rule_is_accepted() -> None:
    """上の試験と対にする。規則どおりの名前は不備にしない。"""
    faults, _ = _filename((_artifact("Y230001.pdf"),), {"filename_pattern": UNANCHORED})
    assert faults == 0
