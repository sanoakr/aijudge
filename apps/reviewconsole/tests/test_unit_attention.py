"""問題セットの「期限経過で要対応」は、止まっている未確定だけで立つ（2026-10-01）。

以前は未確定の全件で立てていたので、自動確定が猶予の明けた順に閉じていく
正常な待ちまで「異議申立・要レビュー・採点失敗、または aijudge-finalize が
動いていない」と警告した（prog2 ex01 で 57 件、止まっていたのは 1 件）。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from aijudge_core import Course
from aijudge_core.ids import CourseId, TenantId
from aijudge_reviewconsole.overview import empty_unit

COURSE = Course(
    id=CourseId("crs_" + "1" * 32),
    tenant_id=TenantId("ten_" + "0" * 32),
    code="prog2",
    title="プログラミング演習 II",
    term="2026-後期",
    subject_profile="cs_lang_c_intro",
    auto_finalize_after_minutes=1440,
)
NOW = datetime(2026, 10, 1, 16, 0, tzinfo=UTC)


def _unit(*, unfinalized: int, stalled: int, deadline_passed: bool = True):
    base = empty_unit("ex01", COURSE, now=NOW)
    return replace(base, unfinalized=unfinalized, stalled=stalled, deadline_passed=deadline_passed)


def test_waiting_alone_does_not_need_attention() -> None:
    unit = _unit(unfinalized=57, stalled=0)
    assert not unit.needs_attention
    assert unit.waiting == 57


def test_a_stalled_submission_needs_attention() -> None:
    unit = _unit(unfinalized=57, stalled=1)
    assert unit.needs_attention
    assert unit.waiting == 56


def test_nothing_needs_attention_before_the_deadline() -> None:
    assert not _unit(unfinalized=3, stalled=3, deadline_passed=False).needs_attention
