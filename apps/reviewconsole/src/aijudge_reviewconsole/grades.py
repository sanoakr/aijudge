"""コースの成績の一覧（2026-10-26）── 学習者 × 課題の表と、その CSV。

**採用の規則は持たない。** 学習者・課題ごとにどの提出を成績に採るかは
`submissions.adopted_ids` の 1 か所で決まり、学習者の画面（`aijudge_studentweb.progress`）
と同じである。ここで別に「最高点」を数え直すと、成績の表と学習者の画面が
食い違ったときに、どちらが正しいかを誰も言えなくなる。

**点は画面に出ている点**（`ScoredRow.final_ratio`・遅延の減点を含む）に、その提出が
指す版の配点（`effective_max_score`）を掛けたもの。

**配点の無い課題（どの版も `points_declared` でない）は 1 点満点で足す**（2026-10-26
決定）。既定の 100 をそのまま足すと、network では「割合だけの課題 1 つ = 20 点の課題
5 つ」になり、誰も決めていない重みが合計に入る。割合（0.0〜1.0）をそのまま点とする。

**合計の分母は「出された課題」だけ**。出題先に入っていない課題（追試の名簿）と、
まだ公開していない課題は分母に入れない ── 入れると、対象でない学生が未提出で
減点されたように見える。未提出は 0 点として分母に入る。

**動作確認の提出（`is_trial`）は数えない**（#108）。表の行は受講登録の学習者だけ。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

import aijudge_webui as webui
from aijudge_audit import AuditAction
from aijudge_core import Course, ReviewState, Role, Task, in_audience, max_scores_by_version
from aijudge_core.ids import CourseGroupId, CourseId, SubmissionId, TaskVersionId, UserId
from aijudge_identity import INSTRUCTOR_ROLES
from aijudge_submission.protocols import ScoredRow

from .audit_context import recorder_for
from .overview import load_units
from .submissions import adopted_ids, version_max_scores

#: 配点の無い課題の満点。割合をそのまま点として足す（2026-10-26）。
UNPOINTED_MAX = 1.0
#: 表と CSV に出す点の小数の桁。配点の無い課題は割合なので 2 桁要る（0.85）。
POINT_DIGITS = 2

#: 欄の状態。**表示の語はここ 1 か所**（画面と CSV で同じ語を使う）。
STATE_LABELS: dict[str, str] = {
    "finalized": "確定",
    "open": "未確定",
    "contested": "再確認の依頼あり",
    "withheld": "保留",
    "grading": "採点中",
    "missing": "未提出",
    "excluded": "対象外",
    "upcoming": "公開前",
}
#: 点の付いた状態。**合計に入るのはこれと未提出（0 点）**。
SCORED_STATES = frozenset({"finalized", "open", "contested"})
#: 分母に入れない状態。
UNCOUNTED_STATES = frozenset({"excluded", "upcoming"})

#: 絞り込みの「何かが残っている人」。値は URL に載る。
FLAG_LABELS: dict[str, str] = {
    "missing": "未提出がある",
    "unfinalized": "未確定がある",
    "withheld": "保留・採点中がある",
}
SORT_LABELS: dict[str, str] = {
    "login": "ログイン名順",
    "total_desc": "合計の高い順",
    "total_asc": "合計の低い順",
}

#: CSV の値の頭にあると、表計算ソフトが式として読む字（CSV インジェクション）。
#: 表示名は学生が決められるので、頭に `'` を付けて文字列に留める。
_FORMULA_HEADS = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class Column:
    """課題 1 つ。`max_points` はいまの版の配点（配点の無い課題は 1）。"""

    task: Task
    unit_key: str
    unit_label: str
    max_points: float
    #: どれかの版に配点が書いてあるか。偽なら割合を 1 点満点で足す。
    pointed: bool

    @property
    def header(self) -> str:
        if not self.pointed:
            return f"{self.task.title}（割合）"
        return f"{self.task.title}（{format_points(self.max_points)} 点）"


@dataclass(frozen=True)
class Cell:
    state: str
    #: 点（割合 × 配点）。点の無い状態は None。
    points: float | None = None
    #: この欄の満点。採用された提出があれば、**その提出が指す版**の配点。
    max_points: float = 0.0
    ratio: float | None = None
    submission_id: SubmissionId | None = None
    submitted_at: datetime | None = None

    @property
    def label(self) -> str:
        return STATE_LABELS[self.state]

    @property
    def counted(self) -> bool:
        return self.state not in UNCOUNTED_STATES


@dataclass(frozen=True)
class Subtotal:
    earned: float = 0.0
    possible: float = 0.0

    @property
    def ratio(self) -> float | None:
        return self.earned / self.possible if self.possible else None


@dataclass(frozen=True)
class LearnerRow:
    user_id: UserId
    login: str
    name: str
    cells: tuple[Cell, ...]
    #: 問題セットの鍵 → 小計（列の順）。
    units: tuple[tuple[str, Subtotal], ...]
    total: Subtotal

    def count(self, *states: str) -> int:
        return sum(1 for cell in self.cells if cell.state in states)


@dataclass(frozen=True)
class GradeFilters:
    """絞り込み。**URL の問い合わせ文字列がそのまま入る**（提出一覧と同じ）。"""

    q: str = ""
    unit: str = ""
    group: str = ""
    flag: str = ""
    sort: str = "login"

    @property
    def active(self) -> bool:
        return bool(self.q or self.unit or self.group or self.flag)

    def query(self) -> dict[str, str]:
        """CSV のリンクに持ち歩く条件（空は落とす）。"""
        values = {
            "q": self.q,
            "unit": self.unit,
            "group": self.group,
            "flag": self.flag,
            "sort": "" if self.sort == "login" else self.sort,
        }
        return {key: value for key, value in values.items() if value}


@dataclass(frozen=True)
class UnitSpan:
    key: str
    label: str
    span: int


@dataclass(frozen=True)
class Gradebook:
    columns: tuple[Column, ...]
    rows: tuple[LearnerRow, ...]
    #: 絞り込む前の学習者の数。
    learners: int
    unpointed: tuple[Column, ...] = ()
    units: tuple[UnitSpan, ...] = field(default=())


@dataclass(frozen=True)
class Learner:
    user_id: UserId
    login: str
    name: str
    groups: frozenset[CourseGroupId] = frozenset()


def format_points(value: float | None) -> str:
    """`20`・`17.5`・`0.85`。末尾の 0 は書かない。"""
    if value is None:
        return ""
    text = f"{round(value, POINT_DIGITS):.{POINT_DIGITS}f}".rstrip("0").rstrip(".")
    return text or "0"


def build_gradebook(
    columns: Iterable[Column],
    learners: Iterable[Learner],
    scored: Iterable[ScoredRow],
    max_scores: Mapping[TaskVersionId, float],
    *,
    now: datetime,
    filters: GradeFilters | None = None,
) -> Gradebook:
    """表を組む。**I/O をしない** ── 読むのは `load_gradebook`。"""
    chosen = filters or GradeFilters()
    every = tuple(columns)
    shown = tuple(c for c in every if not chosen.unit or c.unit_key == chosen.unit)
    people = tuple(learners)
    enrolled = {person.user_id for person in people}

    rows = tuple(row for row in scored if not row.is_trial and row.learner_id in enrolled)
    adopted = adopted_ids(rows, max_scores)
    by_cell: dict[tuple[str, str], list[ScoredRow]] = {}
    for row in rows:
        by_cell.setdefault((str(row.learner_id), str(row.task_id)), []).append(row)

    table: list[LearnerRow] = []
    for person in people:
        if not _matches_person(person, chosen):
            continue
        cells = tuple(
            _cell(
                column,
                person,
                by_cell.get((str(person.user_id), str(column.task.id)), []),
                adopted,
                max_scores,
                now,
            )
            for column in shown
        )
        learner_row = LearnerRow(
            user_id=person.user_id,
            login=person.login,
            name=person.name,
            cells=cells,
            units=_subtotals(shown, cells),
            total=_sum(cells),
        )
        if _matches_flag(learner_row, chosen.flag):
            table.append(learner_row)

    return Gradebook(
        columns=shown,
        rows=_sorted(table, chosen.sort),
        learners=len(people),
        unpointed=tuple(c for c in shown if not c.pointed),
        units=_spans(shown),
    )


def _cell(
    column: Column,
    person: Learner,
    rows: list[ScoredRow],
    adopted: set[SubmissionId],
    max_scores: Mapping[TaskVersionId, float],
    now: datetime,
) -> Cell:
    task = column.task
    if not in_audience(task, person.groups):
        return Cell(state="excluded")
    winner = next((row for row in rows if row.submission_id in adopted), None)
    if winner is not None and winner.final_ratio is not None:
        if column.pointed and winner.task_version_id is not None:
            maximum = max_scores.get(winner.task_version_id, column.max_points)
        else:
            maximum = column.max_points
        if winner.reviewed or winner.finalized:
            state = "finalized"
        else:
            state = "contested" if winner.contested else "open"
        return Cell(
            state=state,
            points=winner.final_ratio * maximum,
            max_points=maximum,
            ratio=winner.final_ratio,
            submission_id=winner.submission_id,
            submitted_at=winner.submitted_at,
        )
    if rows:
        # 出してはいるが点が無い。採点の済んだものがあれば保留（総合点を出さない
        # 規則・P2）、無ければ採点中。どちらも分母には入る（0 点として数える）。
        latest = max(rows, key=lambda row: (row.submitted_at, row.attempt))
        state = "withheld" if any(row.graded for row in rows) else "grading"
        return Cell(
            state=state,
            max_points=column.max_points,
            submission_id=latest.submission_id,
            submitted_at=latest.submitted_at,
        )
    # **公開前の課題は分母に入れない。** 学期の途中で開くと、まだ出していない回が
    # 全員の未提出として合計を下げる。
    if task.before_open_at(now):
        return Cell(state="upcoming")
    return Cell(state="missing", points=0.0, max_points=column.max_points)


def _sum(cells: Iterable[Cell]) -> Subtotal:
    earned = 0.0
    possible = 0.0
    for cell in cells:
        if not cell.counted:
            continue
        earned += cell.points or 0.0
        possible += cell.max_points
    return Subtotal(earned=earned, possible=possible)


def _subtotals(
    columns: tuple[Column, ...], cells: tuple[Cell, ...]
) -> tuple[tuple[str, Subtotal], ...]:
    keys: dict[str, list[Cell]] = {}
    for column, cell in zip(columns, cells, strict=True):
        keys.setdefault(column.unit_key, []).append(cell)
    return tuple((key, _sum(group)) for key, group in keys.items())


def _spans(columns: tuple[Column, ...]) -> tuple[UnitSpan, ...]:
    spans: list[UnitSpan] = []
    for column in columns:
        if spans and spans[-1].key == column.unit_key:
            last = spans[-1]
            spans[-1] = UnitSpan(last.key, last.label, last.span + 1)
        else:
            spans.append(UnitSpan(column.unit_key, column.unit_label, 1))
    return tuple(spans)


def _matches_person(person: Learner, filters: GradeFilters) -> bool:
    if filters.group and filters.group not in {str(g) for g in person.groups}:
        return False
    needle = filters.q.strip().lower()
    if not needle:
        return True
    # **部分一致。** 学籍番号の途中（学年の後ろ）でも、氏名の一部でも引ける。
    return needle in person.login.lower() or needle in person.name.lower()


def _matches_flag(row: LearnerRow, flag: str) -> bool:
    if flag == "missing":
        return row.count("missing") > 0
    if flag == "unfinalized":
        return row.count("open", "contested") > 0
    if flag == "withheld":
        return row.count("withheld", "grading") > 0
    return True


def _sorted(rows: list[LearnerRow], sort: str) -> tuple[LearnerRow, ...]:
    by_login = sorted(rows, key=lambda row: row.login)
    if sort not in ("total_desc", "total_asc"):
        return tuple(by_login)
    # 分母が 0（全部対象外・公開前）の人は、どちらの向きでも後ろに置く。
    scored = [row for row in by_login if row.total.ratio is not None]
    rest = [row for row in by_login if row.total.ratio is None]
    scored.sort(key=lambda row: row.total.ratio or 0.0, reverse=sort == "total_desc")
    return (*scored, *rest)


# -- CSV ---------------------------------------------------------------------


def _text(value: str) -> str:
    return f"'{value}" if value.startswith(_FORMULA_HEADS) else value


def _write(rows: Iterable[Iterable[object]]) -> str:
    out = io.StringIO()
    # **BOM を付ける。** 日本語の Windows の Excel は、付いていない UTF-8 を
    # Shift_JIS として読み、氏名と課題名が化ける。
    out.write("﻿")
    writer = csv.writer(out)
    for row in rows:
        writer.writerow(row)
    return out.getvalue()


def wide_csv(book: Gradebook) -> str:
    """1 人 1 行。課題ごとの点、問題セットの小計、合計。**点の無い欄は空。**

    空にするのは未提出・保留・対象外を数字で区別できないからで、どれだったかは
    縦長の CSV（`long_csv`）の「状態」に出る。合計にはどれも 0 点で入っている
    （対象外と公開前は分母からも外れている）。
    """
    header: list[object] = ["ログイン名", "氏名"]
    header += [_text(f"{c.unit_label} {c.header}") for c in book.columns]
    header += [_text(f"{u.label} 小計") for u in book.units]
    header += ["合計", "合計の満点", "合計（%）", "未提出", "未確定"]
    lines: list[list[object]] = [header]
    for row in book.rows:
        line: list[object] = [_text(row.login), _text(row.name)]
        line += [
            format_points(cell.points) if cell.state in SCORED_STATES else "" for cell in row.cells
        ]
        line += [format_points(sub.earned) for _key, sub in row.units]
        ratio = row.total.ratio
        line += [
            format_points(row.total.earned),
            format_points(row.total.possible),
            "" if ratio is None else format_points(ratio * 100),
            row.count("missing"),
            row.count("open", "contested"),
        ]
        lines.append(line)
    return _write(lines)


def long_csv(book: Gradebook) -> str:
    """学習者 × 課題で 1 行。表計算ソフトで絞り込む・集計する用途。時刻は表示の
    タイムゾーン（既定は日本）で書く。"""
    lines: list[list[object]] = [
        [
            "ログイン名",
            "氏名",
            "問題セット",
            "課題",
            "点",
            "満点",
            "割合",
            "状態",
            "提出時刻",
            "提出 ID",
        ]
    ]
    for row in book.rows:
        for column, cell in zip(book.columns, row.cells, strict=True):
            at = webui.to_local(cell.submitted_at)
            lines.append(
                [
                    _text(row.login),
                    _text(row.name),
                    _text(column.unit_label),
                    _text(column.task.title),
                    format_points(cell.points) if cell.state in SCORED_STATES else "",
                    format_points(cell.max_points) if cell.counted else "",
                    "" if cell.ratio is None else format_points(cell.ratio),
                    cell.label,
                    "" if at is None else at.strftime("%Y-%m-%d %H:%M"),
                    str(cell.submission_id or ""),
                ]
            )
    return _write(lines)


# -- 読み出し ------------------------------------------------------------------


def load_columns(uow: object, course: Course, *, viewer: Role, now: datetime) -> tuple[Column, ...]:
    """列。並びと見せる範囲は問題セットの一覧（`load_units`）と同じ。

    **学習者に出ていない課題は列にしない**（取り下げ・却下）── 出していない課題が
    全員の未提出として合計を下げる。TA に見せない課題（公開前の秘匿）は
    `load_units` が既に外している。
    """
    units = load_units(uow, course, now=now, viewer=viewer)
    tasks = [
        (group, task, version)
        for group in units
        for task, version in group.tasks
        if not task.withdrawn and version.provenance.review_state is not ReviewState.REJECTED
    ]
    versions = uow.tasks.versions_for_tasks([task.id for _g, task, _v in tasks])  # type: ignore[attr-defined]
    scale = max_scores_by_version(versions)
    pointed = {version.task_id for version in versions if version.points_declared}
    columns = []
    for group, task, version in tasks:
        has_points = task.id in pointed
        columns.append(
            Column(
                task=task,
                unit_key=group.key,
                unit_label=group.unit or group.label,
                max_points=scale.get(version.id, version.max_score)
                if has_points
                else UNPOINTED_MAX,
                pointed=has_points,
            )
        )
    return tuple(columns)


def load_learners(uow: object, course: Course) -> tuple[Learner, ...]:
    """受講登録の学習者。グループ（出題先の名簿）も付ける。"""
    identity = uow.identity  # type: ignore[attr-defined]
    ids = {e.user_id for e in identity.list_enrollments(course.id) if e.role is Role.LEARNER}
    users = {user.id: user for user in identity.list_all_users(course.tenant_id)}
    groups: dict[UserId, set[CourseGroupId]] = {}
    for group in identity.list_groups(course.id):
        for member in identity.group_members(group.id):
            groups.setdefault(member, set()).add(group.id)
    return tuple(
        Learner(
            user_id=user_id,
            login=users[user_id].login if user_id in users else str(user_id),
            name=users[user_id].display_name if user_id in users else "",
            groups=frozenset(groups.get(user_id, ())),
        )
        for user_id in ids
    )


def load_gradebook(
    uow: object, course: Course, filters: GradeFilters, *, viewer: Role, now: datetime
) -> Gradebook:
    columns = load_columns(uow, course, viewer=viewer, now=now)
    scored = uow.submissions.scored_for_course(course.id)  # type: ignore[attr-defined]
    return build_gradebook(
        columns,
        load_learners(uow, course),
        scored,
        version_max_scores(uow, scored),
        now=now,
        filters=filters,
    )


# -- 画面 --------------------------------------------------------------------


def register(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    def _filters(q: str, unit: str, group: str, flag: str, sort: str) -> GradeFilters:
        return GradeFilters(
            q=q.strip(),
            unit=unit,
            group=group,
            flag=flag if flag in FLAG_LABELS else "",
            sort=sort if sort in SORT_LABELS else "login",
        )

    @router.get("/courses/{course_id}/grades", response_class=HTMLResponse)
    def grades_page(
        request: Request,
        course_id: str,
        q: str = "",
        unit: str = "",
        group: str = "",
        flag: str = "",
        sort: str = "login",
    ) -> HTMLResponse:
        """成績の一覧。**TA も開ける**（採点している学生の到達を見られないと、
        質問に答えられない）。CSV の書き出しは教員だけ（`grades_csv`）。"""
        from .app import _grader_role, require_principal

        console = request.app.state.aijudge
        me = require_principal(request)
        viewer = _grader_role(console, me, CourseId(course_id))
        filters = _filters(q, unit, group, flag, sort)
        moment = datetime.now(UTC)
        with console.database.unit_of_work() as uow:
            course = uow.identity.get_course(CourseId(course_id))
            if course is None:
                raise HTTPException(status_code=404, detail="コースが見つかりません")
            book = load_gradebook(uow, course, filters, viewer=viewer, now=moment)
            unit_choices = load_columns(uow, course, viewer=viewer, now=moment)
            groups = uow.identity.list_groups(course.id)
        return templates.TemplateResponse(
            request,
            "grades.html",
            {
                "me": me,
                "course": course,
                "section": {"label": "成績", "href": f"/courses/{course.id}/grades"},
                "book": book,
                "filters": filters,
                "units": _spans(unit_choices),
                "groups": groups,
                "flags": FLAG_LABELS,
                "sorts": SORT_LABELS,
                "fmt": format_points,
                "can_export": viewer in INSTRUCTOR_ROLES,
                "csv_query": filters.query(),
            },
        )

    @router.get("/courses/{course_id}/grades.csv")
    def grades_csv(
        request: Request,
        course_id: str,
        q: str = "",
        unit: str = "",
        group: str = "",
        flag: str = "",
        sort: str = "login",
        shape: str = "wide",
    ) -> Response:
        """成績の CSV。**教員だけ**、書き出すたびに監査ログに残す（全員の成績の持ち出し）。

        絞り込みは画面と同じものを受ける ── 画面で絞った結果と CSV の中身がずれると、
        どちらを信じるかを教員が毎回確かめることになる。
        """
        from .app import require_principal
        from .manage.common import _require_instructor

        console = request.app.state.aijudge
        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        filters = _filters(q, unit, group, flag, sort)
        long = shape == "long"
        with console.database.unit_of_work() as uow:
            book = load_gradebook(
                uow, course, filters, viewer=Role.INSTRUCTOR, now=datetime.now(UTC)
            )
            recorder_for(uow, request, me, role="instructor").record(
                AuditAction.GRADES_EXPORTED,
                target_type="course",
                target_id=str(course.id),
                summary=f"成績の CSV を書き出した（{len(book.rows)} 名）",
                detail={
                    "course_id": str(course.id),
                    "shape": "long" if long else "wide",
                    "learners": len(book.rows),
                    "filters": filters.query(),
                },
            )
            uow.commit()
        body = long_csv(book) if long else wide_csv(book)
        stamp = webui.to_local(datetime.now(UTC)).strftime("%Y%m%d")  # type: ignore[union-attr]
        name = f"grades-{course.id}-{'long' if long else 'wide'}-{stamp}.csv"
        return Response(
            content=body.encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                # 個人の成績である。共有の端末やプロキシに残さない。
                "Cache-Control": "no-store",
            },
        )

    return router
