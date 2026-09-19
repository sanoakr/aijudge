"""動画の保存期間の計算を固定する（ADR 0020）。

起点は**課題の締切**で 6 ヶ月。ここで確かめるのは、その約束が月の単位で
守られること、締切の無い課題では期限が決まらないこと、そして「最も早い回
でもその学期の疑義より後になる」という採用の根拠そのものである。
"""

from __future__ import annotations

from datetime import UTC, datetime

from aijudge_core import (
    VIDEO_RETENTION_MONTHS,
    video_retention_expires_at,
    video_retention_has_expired,
)


def _at(year: int, month: int, day: int, hour: int = 23, minute: int = 59) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def test_six_months_after_the_deadline() -> None:
    assert VIDEO_RETENTION_MONTHS == 6
    assert video_retention_expires_at(_at(2026, 10, 1)) == _at(2027, 4, 1)


def test_the_year_rolls_over() -> None:
    assert video_retention_expires_at(_at(2026, 11, 20)) == _at(2027, 5, 20)


def test_a_month_end_deadline_lands_on_a_real_day() -> None:
    # 8 月 31 日の 6 ヶ月後は 2 月 31 日に存在しない。**月末へ詰める** ──
    # `timedelta(days=183)` で代用すると、月をまたいでずれる。
    assert video_retention_expires_at(_at(2026, 8, 31)) == _at(2027, 2, 28)
    # うるう年も同じ規則で決まる。
    assert video_retention_expires_at(_at(2027, 8, 31)) == _at(2028, 2, 29)


def test_a_task_without_a_deadline_never_expires() -> None:
    # 砂場・自習用の課題（`Task.due_at` が `None`）。起点が無いので期限も
    # 無い ── 消し過ぎは取り返せず、消し残しは次に消せる。
    assert video_retention_expires_at(None) is None
    assert video_retention_has_expired(None, now=_at(2099, 1, 1)) is False


def test_expiry_is_inclusive_of_the_moment_itself() -> None:
    due_at = _at(2026, 10, 1)
    expires_at = _at(2027, 4, 1)
    assert video_retention_has_expired(due_at, now=expires_at) is True
    assert video_retention_has_expired(due_at, now=_at(2027, 3, 31)) is False


def test_the_earliest_unit_of_a_term_expires_after_its_appeals_window() -> None:
    """採用の根拠（ADR 0020 の表）。**ここが通れば残りは自動的に通る。**

    最も早い回の締切がいちばん早く期限を迎える。後期の 10 月締切が 4 月、
    その学期の疑義は 2〜3 月 ── 起点をこれより短くすると、異議を申し立てる
    側から先に根拠が消える（ADR 0009）。
    """
    autumn_first_unit = _at(2026, 10, 1)
    appeals_end = _at(2027, 3, 31)
    expires_at = video_retention_expires_at(autumn_first_unit)
    assert expires_at is not None and expires_at > appeals_end

    spring_first_unit = _at(2026, 4, 15)
    spring_appeals_end = _at(2026, 9, 30)
    spring_expires_at = video_retention_expires_at(spring_first_unit)
    assert spring_expires_at is not None and spring_expires_at > spring_appeals_end
