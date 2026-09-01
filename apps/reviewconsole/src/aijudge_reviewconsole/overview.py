"""担当コースのダイジェストと、回（問題セット）ごとのまとめ。

管理画面の入口が「コード・コース名・学期」の 3 列だけだと、教員はどのコースに
用があるのかを開くまで判断できない。**開かずに分かるべきもの**をここで作る ──
課題が何問あり、受講者が何名いて、未確定が何件残り、異議申立と未承認の
課題（AI が生成し、まだ承認していないもの）が何件待っているか。

回ごとのまとめも同じ理由でここに置く。課題を平らに 20 行並べると、教員は
「第 3 回の締切を直したい」ときに目で探すことになる。1 回の授業で複数問
（`p1 p2 p3`）出るという課題の形（`Task.unit`）が、そのまま画面の構造になる。

**まとまりの鍵は `unit`。** `Task` の docstring が言うとおり `unit` が同一性の
鍵で、`session` は並べ替えのための数値である。URL に載せる鍵も `unit` から
作る ── `session` から作ると、回に対応しないまとまり（`exam08`）が同じ鍵に
潰れる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, unquote

from aijudge_admin import pending_counts
from aijudge_core import (
    Course,
    ReviewState,
    Role,
    Task,
    TaskVersion,
    grace_minutes,
)


def unit_key(task: Task) -> str:
    """まとまりを URL に載せる鍵。

    `unit` が無い課題（取り込み元にまとまりが無かったもの）は `session` で、
    それも無ければ「未分類」で 1 つにまとめる。`quote` するのは、`unit` が
    取り込み元のディレクトリ名そのままで、`/` を含みうるため。
    """
    raw = task.unit or (f"s{task.session}" if task.session is not None else "_")
    return quote(raw, safe="")


@dataclass(frozen=True)
class UnitGroup:
    """1 つの問題セット。

    **日程はセットで揃える。** 公開・提出開始・締切・自動確定の猶予は課題が
    持つが、値を決めるのはこの単位である。ばらついている（＝取り込みで
    課題ごとに違う値が入った）ときは、代表値を出しつつ `mixed` で告げる ──
    黙って 1 つに見せると、教員は揃っていると思ったまま学期を過ごす。
    """

    key: str
    label: str
    unit: str | None
    session: int | None
    tasks: tuple[tuple[Task, TaskVersion], ...]
    opens_at: datetime | None
    submissions_open_at: datetime | None
    due_at: datetime | None
    # 提出の受付を終える時刻（#73）。空なら締切後も無期限に受け付ける。
    accepts_until: datetime | None
    # 採点を始める時刻（試験・#67）。空なら提出と同時に採点する。
    grading_starts_at: datetime | None
    # 学習者に出ていない課題の数（取り下げ・却下）。**セットの一覧で要る**
    # ── 中を開かないと、そのセットがもう出ていないことに気づけない（#83）。
    hidden: int
    # このセットで実際に効く猶予（分）。課題の指定が無ければコースの既定。
    grace: int | None
    # セット内で日程がばらついているか。
    mixed: bool
    # この問題セットで未確定のまま残っている提出の件数。
    unfinalized: int
    # いまどの段階か。**期限経過なのに未確定が残っていることが見えるようにする。**
    # **この問題セットの締切を過ぎたか。** 提出ごとの確定の窓
    # （`grade_window`）とは別物である ── あちらは採点完了を起点に 1 件ずつ
    # 進み、こちらは「この回はもう終わっているか」を言う。教員が見たいのは
    # 後者で、締切を過ぎたのに未確定が残っていれば何かが止まっている。
    deadline_passed: bool

    @property
    def count(self) -> int:
        return len(self.tasks)

    @property
    def grace_from_course(self) -> bool:
        """猶予がコースの既定のままか（このセットで指定していない）。"""
        return all(task.auto_finalize_after_minutes is None for task, _ in self.tasks)

    @property
    def needs_attention(self) -> bool:
        """期限が過ぎたのに未確定が残っているか。

        自動確定が動いていないか、異議申立・要レビューが残っているかの
        どちらかで、どちらも教員が見るべき状態である。
        """
        return self.deadline_passed and self.unfinalized > 0


@dataclass(frozen=True)
class CourseDigest:
    """担当コース 1 件を、開かずに判断できるだけの情報にしたもの。"""

    course: Course
    # 問題セットの数。課題数だけだと、学期のどのあたりかが読み取れない。
    units: int
    tasks: int
    learners: int
    unfinalized: int
    contested: int
    drafts: int
    next_due: datetime | None

    @property
    def needs_attention(self) -> bool:
        return self.contested > 0 or self.drafts > 0


def load_units(
    uow: object,
    course: Course,
    *,
    pending: dict[object, int] | None = None,
    now: datetime | None = None,
) -> tuple[UnitGroup, ...]:
    """コースの課題を問題セットごとにまとめる。並びは `Task.sort_key` に従う。

    `pending` は課題ごとの未確定件数（`pending_counts`）。渡さなければ
    件数は 0 として組む ── 件数が要らない画面で課題数ぶんの問い合わせを
    させないため。
    """
    moment = now or datetime.now(UTC)
    counts = pending or {}
    rows: list[tuple[Task, TaskVersion]] = []
    for task in uow.tasks.list_for_course(course.id):  # type: ignore[attr-defined]
        version = uow.tasks.latest_version(task.id)  # type: ignore[attr-defined]
        if version is not None:
            rows.append((task, version))

    grouped: dict[str, list[tuple[Task, TaskVersion]]] = {}
    for task, version in sorted(rows, key=lambda row: row[0].sort_key):
        grouped.setdefault(unit_key(task), []).append((task, version))

    groups: list[UnitGroup] = []
    for key, items in grouped.items():
        head = items[0][0]
        tasks = [task for task, _ in items]
        opens = [task.opens_at for task in tasks if task.opens_at]
        starts = [task.submissions_open_at for task in tasks if task.submissions_open_at]
        dues = [task.due_at for task in tasks if task.due_at]
        grading = [task.grading_starts_at for task in tasks if task.grading_starts_at]
        accepts = [task.accepts_until for task in tasks if task.accepts_until]
        # 代表値は「最も早い公開・最も遅い締切」。揃っていれば同じ値になる。
        due_at = max(dues) if dues else None
        grace = grace_minutes(head.auto_finalize_after_minutes, course.auto_finalize_after_minutes)
        groups.append(
            UnitGroup(
                key=key,
                label=head.unit_label,
                unit=head.unit,
                session=head.session,
                tasks=tuple(items),
                opens_at=min(opens) if opens else None,
                submissions_open_at=min(starts) if starts else None,
                due_at=due_at,
                # 受付終了も最も遅いものを採る。早い側にすると、まだ出せる
                # 課題があるセットを「受付終了」と書くことになる。
                accepts_until=max(accepts) if accepts else None,
                # **最も遅い時刻を代表にする。** 揃っていれば同じ値で、
                # ばらついているときに「まだ採点しない課題がある」を
                # 隠さない側に倒す。
                grading_starts_at=max(grading) if grading else None,
                grace=grace,
                hidden=sum(
                    1
                    for task, version in items
                    if task.withdrawn or version.provenance.review_state is ReviewState.REJECTED
                ),
                mixed=_mixed(tasks),
                unfinalized=sum(counts.get(task.id, 0) for task, _ in items),
                deadline_passed=due_at is not None and moment >= due_at,
            )
        )
    return tuple(groups)


def _mixed(tasks: list[Task]) -> bool:
    """セット内で日程がばらついているか。

    取り込み済みの課題は 1 件ずつ締切を持っていることがあり、そこへ
    「セットの締切」だけを出すと、揃っていないことに気づけない。
    """
    shapes = {
        (
            task.opens_at,
            task.submissions_open_at,
            task.due_at,
            task.accepts_until,
            # 採点開始がばらついているのは試験として壊れている ── 一部だけ
            # 採点が始まると、その結果が試験中に返る（#67）。
            task.grading_starts_at,
            task.auto_finalize_after_minutes,
        )
        for task in tasks
    }
    return len(shapes) > 1


def find_unit(groups: tuple[UnitGroup, ...], key: str) -> UnitGroup | None:
    for group in groups:
        if group.key == key:
            return group
    return None


def empty_unit(key: str, course: Course, *, now: datetime | None = None) -> UnitGroup:
    """まだ課題を 1 問も持たない回。

    新しい回はここから始まる ── 教員が鍵（`ex04` など）を決めて開き、
    最初の 1 問を足す。存在しない回として断ると、回を作る導線が無くなる。
    """
    raw = unquote(key)
    return UnitGroup(
        key=key,
        label=raw if raw != "_" else "未分類",
        unit=None if raw == "_" else raw,
        session=None,
        tasks=(),
        opens_at=None,
        submissions_open_at=None,
        due_at=None,
        accepts_until=None,
        grading_starts_at=None,
        grace=course.auto_finalize_after_minutes,
        hidden=0,
        mixed=False,
        unfinalized=0,
        deadline_passed=False,
    )


def course_digest(database: object, course: Course, *, now: datetime | None = None) -> CourseDigest:
    """1 コースぶんのダイジェスト。

    未確定の件数は課題ごとに引くので、**コース数ぶん繰り返すと重い**。
    担当コースは教員 1 人あたり数件という前提でこの形にしている。
    """
    moment = now or datetime.now(UTC)
    pending = pending_counts(database, course.id)  # type: ignore[arg-type]
    with database.unit_of_work() as uow:  # type: ignore[attr-defined]
        tasks = uow.tasks.list_for_course(course.id)
        enrollments = uow.identity.list_enrollments(course.id)
        contested = len(uow.reviews.requested_for_course(course.id))
        task_ids = {task.id for task in tasks}
        drafts = sum(
            1 for version in uow.tasks.list_versions_in_review() if version.task_id in task_ids
        )
    upcoming = [task.due_at for task in tasks if task.due_at and task.due_at >= moment]
    return CourseDigest(
        course=course,
        units=len({unit_key(task) for task in tasks}),
        tasks=len(tasks),
        learners=sum(1 for row in enrollments if row.role is Role.LEARNER),
        unfinalized=sum(pending.values()),
        contested=contested,
        drafts=drafts,
        next_due=min(upcoming) if upcoming else None,
    )


def digests_for(
    database: object, courses: list[Course], *, now: datetime | None = None
) -> tuple[CourseDigest, ...]:
    return tuple(course_digest(database, course, now=now) for course in courses)


__all__ = [
    "CourseDigest",
    "UnitGroup",
    "course_digest",
    "digests_for",
    "empty_unit",
    "find_unit",
    "load_units",
    "unit_key",
]
