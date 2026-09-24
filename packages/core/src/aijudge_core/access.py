"""課題を誰に・いつ見せるか（`docs/design/task-visibility.md`）。

**判定をここの 1 か所に置く。** 学生画面とコンソールの両方がここを呼ぶ。
経路ごとに書くと、どれかが漏れる ── 動画の 1 発の送信は関門を写して持ち、
写すときに学内限定だけが落ちていた（#119 の教訓を踏み直した）。

| 役割 | 通常・公開前 | 通常・公開後 | 秘匿・公開前 | 秘匿・公開後 |
|---|---|---|---|---|
| 教員・管理者 | 見える | 見える | 見える | 見える |
| TA | 見える | 見える | **見えない** | 見える |
| 学習者（出題先の中） | 見えない | 見える | 見えない | 見える |
| 学習者（出題先の外） | 見えない | **見えない** | 見えない | **見えない** |

「見えない」は、一覧に出さず、URL を直に開いても「無い」と答えること。

出題先（`Task.audience_group_ids`）が空の課題では、学習者は全員「出題先の中」。
**出題先は学習者にだけ効く** ── 教員・TA は名簿に関係なく見える。

取り下げ（`withdrawn`）と承認済みの版があるか（#48）はここで見ない。
どちらも課題の**存在**の問題で、役割と時刻によらない。呼ぶ側が先に落とす。
"""

from __future__ import annotations

from datetime import datetime

from .ids import CourseGroupId
from .task import Task
from .tenancy import Role

# 公開前でも見える役割。**TA は秘匿の課題では外れる**（`may_see`）。
_STAFF = frozenset({Role.INSTRUCTOR, Role.ADMIN})


def may_see(
    task: Task,
    role: Role,
    *,
    now: datetime,
    groups: frozenset[CourseGroupId] = frozenset(),
) -> bool:
    """この役割の人に、いまこの課題を見せてよいか。

    `groups` は見る人がそのコースで入っている名簿。**学習者のときだけ読む。**
    名簿を引くのは呼ぶ側（ここは I/O をしない）。
    """
    if role in _STAFF:
        return True
    before_open = task.before_open_at(now)
    if role is Role.ASSISTANT:
        # 公開前の課題も読める（#102・#340）── **秘匿の課題を除いて。**
        return not (before_open and task.confidential_until_open)
    # 学習者には公開前の課題を出さない。一覧から外すだけでは、URL を知って
    # いれば問題文を開けた（課題ページが公開日時を見ていなかった）。
    if before_open:
        return False
    return in_audience(task, groups)


def in_audience(task: Task, groups: frozenset[CourseGroupId]) -> bool:
    """出題先に入っているか。**出題先が空なら全員が入っている**（従来どおり）。"""
    if not task.audience_group_ids:
        return True
    return not groups.isdisjoint(task.audience_group_ids)


def may_submit_before_open(task: Task, role: Role, *, now: datetime) -> bool:
    """提出開始前でも出せる相手か（#340）。**教員・TA だけ。**

    この 2 つの役割の提出は `Submission.is_trial` が真になり、成績にも
    統計にも入らない（#108）。公開前こそ実物で確かめたい。

    **秘匿の課題では TA を外す。** 見えない課題に出せるのは筋が通らないし、
    教員の試しの提出と同じく、TA の試しの提出も模範解答に近いものになる。
    判定は `may_see` に揃える ── 見えるのに出せない、出せるのに見えない、
    のどちらも作らない。

    テナント管理者（受講登録の無い `ADMIN`）は含めない ── 受講登録を
    持たない相手まで広げるかは、#340 とは別に決める（従来どおり）。
    """
    if role is Role.INSTRUCTOR:
        return True
    return role is Role.ASSISTANT and may_see(task, role, now=now)
