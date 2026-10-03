"""1 コースの不整合でコース一覧全体が落ちない（2026-10-04、network の test5）。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, model_validator

from aijudge_core import Course
from aijudge_reviewconsole import overview


class _Strict(BaseModel):
    x: int = 0

    @model_validator(mode="after")
    def _fail(self) -> _Strict:
        raise ValueError("受付終了が締切より前になっています")


def _course(code: str) -> Course:
    return Course.model_construct(id=f"crs_{code}", code=code, title=code, term="t")  # type: ignore[arg-type]


def test_a_bad_course_is_reported_and_the_others_still_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = overview.CourseDigest(
        course=_course("ok"),
        units=1,
        tasks=2,
        learners=3,
        unfinalized=0,
        contested=0,
        drafts=0,
        next_due=None,
    )

    def fake(database: object, course: Course, *, now: object = None) -> overview.CourseDigest:
        if course.code == "bad":
            _Strict()
        return good

    monkeypatch.setattr(overview, "course_digest", fake)

    digests = overview.digests_for(object(), [_course("bad"), _course("ok")])

    assert [d.course.code for d in digests] == ["bad", "ok"]
    assert digests[0].error and "受付終了" in digests[0].error
    assert digests[1].error is None and digests[1].tasks == 2
