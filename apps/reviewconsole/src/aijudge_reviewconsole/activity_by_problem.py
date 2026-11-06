"""作業の記録を、問題セットごと・問題ごとに見る画面（2026-11-06）。

学習者の画面（`activity.py`）が答えるのは「この人はどう書いたか」で、ここが答える
のは「この回を・この問題を、受講者はどう書いたか」である。試験のあとに見たいのは
たいてい後者で、学習者の画面を 150 回開いて比べるわけにはいかない。

出すものは学習者の画面と同じ**事実だけ**で、**判定はしない**（設計書 §7）。並べ替えは
読む順番を変えるだけで、順位に意味を持たせない。**見られるのは担当教員だけ**で、
**見たことを監査ログに残す**（ADR 0023 §5）。全員分を 1 画面で見せるので、残す
必要は学習者の画面より大きい。

画面は 3 つある。

- 問題セットの一覧（`/courses/{id}/activity/units`）: **索引だけで作る**
- 問題セット（`/courses/{id}/activity/units/{unit}`）: 問題ごとの人数と、学習者ごとの合計
- 問題（`/courses/{id}/activity/tasks/{task}`）: 学習者ごとの作業・提出・再生への入口

**問題セットと問題の画面は、そのセットの全員の本体を読む。** 問題ごとの数字は索引に
無い（タブの切り替えは本体の中にしかない）。そこで**読み取った結果を覚えておく**
（`_digests`）。閉じたセッションの本体は変わらないので、2 度目からは読まなくて済む。

**1 人の記録の形が悪くても、画面全体を落とさない。** 記録はクライアントが送って
くるもので、受け口は形の一部しか確かめない（ADR 0023「やらないこと」）。学習者の
画面なら落ちるのはその 1 人の分だが、ここが落ちると、受講者 1 人の細工で全員分が
見られなくなる。読めないセッションは「読めない記録」として数えて出す。

**どのセッションを読むかは `IdeSession.unit` で決める。** 問題を別のセットへ移すと、
移す前に開いた回は移す前のセットの画面に数える（問題の画面は本体を読むまで、その
回に問題が含まれるか分からない ── 全セットを読むのは割に合わない）。その分は学習者の
画面からは辿れる。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from aijudge_core import AnswerMode, Course, ReviewState, Task
from aijudge_core.ids import SubmissionId, TaskId, TaskVersionId
from aijudge_ide import (
    ActivityFiles,
    ActivitySummary,
    EventBatch,
    IdeSession,
    IdeSessionId,
    summarize_by_tab,
)
from aijudge_ide.flags import Flag, flag_events, shared_paste_flags, submission_mismatches

from .activity import (
    UNKNOWN_TASK,
    _add,
    _audit,
    _instructor,
    _missing,
    _read,
    _submissions,
    _worked,
    activity_root,
)
from .grades import Learner, load_learners
from .overview import UnitGroup, empty_unit, find_unit, load_units

#: 問題が分からない記録の見出し（学習者の画面と同じ語）。
UNKNOWN_TITLE = "問題が分からない記録"

#: 並べ替え。値は URL に載る。**読む順番を変えるだけ**で、順位に意味は持たせない。
SORT_LABELS: dict[str, str] = {
    "login": "ログイン名順",
    "paste": "外からの貼り付けの字数が多い順",
    "away": "画面を離れた時間が長い順",
    "flags": "確かめる場所が多い順",
}

#: 読み取った結果を覚えておく数。1 件は数百バイトから数 KB で、150 名 × 3 回 ×
#: 15 回分を十分に覆う。教員コンソールは 1 プロセスで動く（`aijudge-review.service`）。
MAX_DIGESTS = 8192


@dataclass(frozen=True)
class SessionDigest:
    """1 回のセッションの本体から読み取った分。**DB を見ずに決まるものだけ**を持つ。

    ほかの学生との貼り付けの重なりと提出との突き合わせは、後から増えて変わるので
    覚えない（`_session_flags` が毎回 DB と突き合わせる）。
    """

    #: hello に載っていた課題版（タブの順）。無ければ空。
    version_ids: tuple[str, ...] = ()
    by_tab: dict[int, ActivitySummary] = field(default_factory=dict)
    flags: tuple[Flag, ...] = ()
    #: 外からの貼り付けと提出の出来事。あとで DB と突き合わせる分だけ取っておく。
    pastes: tuple[dict[str, Any], ...] = ()
    submits: tuple[dict[str, Any], ...] = ()
    #: 読めなかった束の数。形が悪くてセッション全体を読めなければ、全部の束を数える。
    unreadable: int = 0


def _slim(event: dict[str, Any], *keys: str) -> dict[str, Any]:
    """出来事から要る欄だけを取る。**本文を覚えない**（数字と目印だけを持つ）。"""
    return {key: event[key] for key in ("type", "t", "tab", *keys) if key in event}


def _parse(events: list[dict[str, Any]], unreadable: int) -> SessionDigest:
    hello = next((e for e in events if e.get("type") == "hello"), None)
    tasks = None if hello is None else hello.get("tasks")
    return SessionDigest(
        version_ids=tuple(str(v) for v in tasks) if isinstance(tasks, list) else (),
        by_tab=summarize_by_tab(events),
        flags=tuple(flag_events(events)),
        pastes=tuple(
            _slim(e, "hash", "len", "origin")
            for e in events
            if e.get("type") == "paste"
            and e.get("origin") != "internal"
            and isinstance(e.get("hash"), str)
        ),
        submits=tuple(
            _slim(e, "submission_id", "hash")
            for e in events
            if e.get("type") == "submit" and e.get("submission_id")
        ),
        unreadable=unreadable,
    )


def _read_digest(
    files: ActivityFiles, session: IdeSession, batches: tuple[EventBatch, ...]
) -> SessionDigest:
    loaded, unreadable = _read(files, session, batches)
    events = [event for _batch, batch_events in loaded for event in batch_events]
    try:
        return _parse(events, unreadable)
    except (TypeError, ValueError, AttributeError):
        # 形の悪い出来事（`len` が数でないなど）。冒頭の「画面全体を落とさない」。
        return SessionDigest(unreadable=len(batches))


#: 読み取りの結果を引く鍵: (セッション, 束の数, 最後の seq)。
DigestKey = tuple[str, int, int]


def _digest_key(session: IdeSession, size: tuple[int, int] | None) -> DigestKey:
    """**索引の状態を鍵に入れる。** 新しい束が届くと鍵が変わるので、開いている途中の
    セッションは読み直される。"""
    count, last_seq = size or (0, -1)
    return (str(session.id), count, last_seq)


class _DigestCache:
    """読み取った結果を覚えておく。消されたセッションの分は古い順に押し出される。"""

    def __init__(self, limit: int = MAX_DIGESTS) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._items: OrderedDict[DigestKey, SessionDigest] = OrderedDict()

    def get(self, key: DigestKey) -> SessionDigest | None:
        with self._lock:
            found = self._items.get(key)
            if found is not None:
                self._items.move_to_end(key)
            return found

    def put(self, key: DigestKey, digest: SessionDigest) -> None:
        with self._lock:
            self._items[key] = digest
            while len(self._items) > self._limit:
                self._items.popitem(last=False)


_digests = _DigestCache()


@dataclass
class WorkPart:
    """1 回のセッションのうち、1 つの問題の分。"""

    session: IdeSession
    tab: int
    summary: ActivitySummary


@dataclass
class Work:
    """1 人の学習者の作業。問題 1 つの分か、問題セット全体の合計。"""

    summary: ActivitySummary = field(default_factory=ActivitySummary)
    parts: list[WorkPart] = field(default_factory=list)
    flags: list[tuple[IdeSession, Flag]] = field(default_factory=list)

    def add(self, other: Work) -> None:
        self.summary = _add(self.summary, other.summary)
        self.parts.extend(other.parts)
        self.flags.extend(other.flags)


@dataclass(frozen=True)
class FlagGroup:
    """同じ種類の印をまとめたもの。リンクは最初の 1 つの再生へ。"""

    label: str
    count: int
    session: IdeSession
    flag: Flag


def flag_groups(flags: Iterable[tuple[IdeSession, Flag]]) -> list[FlagGroup]:
    """印を種類でまとめる。表の 1 行に全部並べると、行が印で埋まる。"""
    first: dict[str, tuple[IdeSession, Flag]] = {}
    counts: dict[str, int] = {}
    for session, flag in flags:
        first.setdefault(flag.label, (session, flag))
        counts[flag.label] = counts.get(flag.label, 0) + 1
    return [
        FlagGroup(label=label, count=counts[label], session=session, flag=flag)
        for label, (session, flag) in first.items()
    ]


@dataclass
class LearnerRow:
    """表の 1 行。問題セットの画面と問題の画面で同じ形を使う。"""

    learner: Learner
    work: Work
    #: 送信の欠け（索引から数える）。
    missing: int = 0
    unreadable: int = 0
    #: 提出した問題の数（問題セットの画面）。
    submitted_tasks: int = 0
    #: この問題への提出（問題の画面）。提出日時の順。
    submissions: list[dict[str, Any]] = field(default_factory=list)

    @property
    def flags(self) -> list[FlagGroup]:
        return flag_groups(self.work.flags)

    @property
    def adopted(self) -> dict[str, Any] | None:
        return next((s for s in self.submissions if s["adopted"]), None)

    @property
    def auto_closed(self) -> bool:
        return any(s["origin"] == "auto_close" for s in self.submissions)


@dataclass(frozen=True)
class ProblemRow:
    """問題セットの画面の、問題 1 つの行。"""

    task_id: str
    title: str
    #: この問題の画面へ開けるか。いま別のセットにある問題と、問題が分からない記録は開けない。
    openable: bool
    worked: int
    pasted: int
    flagged: int
    submitted: int


@dataclass(frozen=True)
class UnitRecord:
    """1 つの問題セットの記録を、問題 × 学習者に振り分けたもの。"""

    learners: tuple[Learner, ...]
    #: 課題 ID → 学習者 ID → 作業。
    work: dict[str, dict[str, Work]]
    #: hello から分かった課題の名前（いまセットに無い課題も含む）。
    titles: dict[str, str]
    sessions: dict[str, int]
    missing: dict[str, int]
    unreadable: dict[str, int]


def _submitted_hashes(uow: Any, digests: Iterable[SessionDigest]) -> dict[str, str]:
    """提出 ID → 実際の提出の指紋（`ide_submission_links`）。1 つの提出を 2 度引かない。"""
    wanted = {str(event["submission_id"]) for digest in digests for event in digest.submits}
    hashes: dict[str, str] = {}
    for submission_id in wanted:
        link = uow.ide_links.for_submission(SubmissionId(submission_id))
        if link is not None:
            hashes[submission_id] = link.content_hash
    return hashes


def _session_flags(
    digest: SessionDigest,
    session: IdeSession,
    sharing: dict[str, frozenset],
    submitted: dict[str, str],
) -> list[Flag]:
    """印を付ける。規則は学習者の画面（`activity._flags`）と同じで、DB を 1 回で済ませる。"""
    others = {
        event["hash"]: len(sharing[event["hash"]] - {session.learner_id})
        for event in digest.pastes
        if event["hash"] in sharing
    }
    flags = [
        *digest.flags,
        *submission_mismatches(digest.submits, submitted),
        *shared_paste_flags(digest.pastes, others),
    ]
    return sorted(flags, key=lambda flag: flag.t)


def _load_record(console: Any, course: Course, group: UnitGroup, root: Any) -> UnitRecord:
    """問題セットの全員の記録を読み、問題 × 学習者に振り分ける。"""
    with console.database.unit_of_work() as uow:
        learners = load_learners(uow, course)
        enrolled = {learner.user_id for learner in learners}
        # 受講していない人（教員の動作確認・履修を取り消した人）の記録は出さない。
        sessions = [
            session
            for session in uow.ide_activity.course_sessions(course.id)
            if session.unit == group.unit and session.learner_id in enrolled
        ]
        sizes = uow.ide_activity.batch_sizes([session.id for session in sessions])
        keys = {session.id: _digest_key(session, sizes.get(session.id)) for session in sessions}
        remembered = {session.id: _digests.get(keys[session.id]) for session in sessions}
        # **読み直す分の束だけを引く。** 2 度目に開くときに数万行を運ばない。
        unread = (
            {}
            if root is None
            else {
                session.id: uow.ide_activity.batches(session.id)
                for session in sessions
                if remembered[session.id] is None
            }
        )

    opened: dict[str, int] = {}
    missing: dict[str, int] = {}
    unreadable: dict[str, int] = {}
    for session in sessions:
        learner_id = str(session.learner_id)
        opened[learner_id] = opened.get(learner_id, 0) + 1
        missing[learner_id] = missing.get(learner_id, 0) + _missing(sizes.get(session.id))

    work: dict[str, dict[str, Work]] = {}
    titles: dict[str, str] = {}
    if root is None:
        return UnitRecord(learners, work, titles, opened, missing, unreadable)

    files = ActivityFiles(root)
    digests: dict[IdeSessionId, SessionDigest] = {}
    for session in sessions:
        digest = remembered[session.id]
        if digest is None:
            digest = _read_digest(files, session, unread[session.id])
            # **読めなかった束のある結果は覚えない。** 一時的な不調が消えなくなる。
            if not digest.unreadable:
                _digests.put(keys[session.id], digest)
        digests[session.id] = digest

    with console.database.unit_of_work() as uow:
        known: dict[str, tuple[str, str]] = {}
        for version_id in {v for digest in digests.values() for v in digest.version_ids}:
            version = uow.tasks.get_version(TaskVersionId(version_id))
            task = None if version is None else uow.tasks.get_task(version.task_id)
            # 消えた課題は ID のまま出す（記録は課題より長く残りうる）。
            known[version_id] = (
                (str(task.id), task.title) if task is not None else (version_id, version_id)
            )
        hashes = [event["hash"] for digest in digests.values() for event in digest.pastes]
        sharing = uow.ide_activity.learners_sharing(course.id, hashes) if hashes else {}
        submitted = _submitted_hashes(uow, digests.values())

    for session in sessions:
        digest = digests[session.id]
        learner_id = str(session.learner_id)
        if digest.unreadable:
            unreadable[learner_id] = unreadable.get(learner_id, 0) + digest.unreadable
        flags = _session_flags(digest, session, sharing, submitted)
        for tab, summary in digest.by_tab.items():
            # **手を動かしていない問題は数えない**（学習者の画面と同じ）。開いて
            # いただけでも、ハートビートは開いていたタブに振り分けられる。
            if not _worked(summary):
                continue
            if 0 <= tab < len(digest.version_ids):
                task_id, title = known[digest.version_ids[tab]]
            else:
                task_id, title = UNKNOWN_TASK, UNKNOWN_TITLE
            titles[task_id] = title
            work.setdefault(task_id, {}).setdefault(learner_id, Work()).add(
                Work(
                    summary=summary,
                    parts=[WorkPart(session, tab, summary)],
                    flags=[(session, flag) for flag in flags if flag.tab == tab],
                )
            )
    return UnitRecord(learners, work, titles, opened, missing, unreadable)


def _sorted_rows(rows: list[LearnerRow], sort: str) -> list[LearnerRow]:
    """並べ替え。同じ値はログイン名の順。"""
    keys = {
        "paste": lambda row: -row.work.summary.pasted_external_chars,
        "away": lambda row: -row.work.summary.away_seconds,
        "flags": lambda row: -len(row.work.flags),
    }
    by_login = sorted(rows, key=lambda row: row.learner.login)
    key = keys.get(sort)
    return by_login if key is None else sorted(by_login, key=key)


def _offered(group: UnitGroup) -> list[Task]:
    """学習者に出している問題。取り下げと却下は除く（成績の列 `load_columns` と同じ）。"""
    return [
        task
        for task, version in group.tasks
        if not task.withdrawn and version.provenance.review_state is not ReviewState.REJECTED
    ]


def _submitted_by_task(uow: Any, course: Course, task_ids: list[TaskId]) -> dict[str, set[str]]:
    """課題 ID → 提出した学習者。**ファイルの提出も入る**（記録が無くても提出はある）。"""
    if not task_ids:
        return {}
    found: dict[str, set[str]] = {}
    for row in uow.submissions.scored_for_course(course.id, task_ids=task_ids):
        # 動作確認の提出は数えない（#108）。
        if not row.is_trial:
            found.setdefault(str(row.task_id), set()).add(str(row.learner_id))
    return found


def unit_page_data(
    console: Any, course: Course, group: UnitGroup, root: Any, sort: str
) -> dict[str, Any]:
    """問題セットの画面に渡す値。"""
    record = _load_record(console, course, group, root)
    enrolled = {str(learner.user_id) for learner in record.learners}
    offered = [(str(task.id), task.title) for task in _offered(group)]
    offered_ids = {task_id for task_id, _title in offered}
    in_unit = {str(task.id) for task, _version in group.tasks}
    # 出していないのに記録がある問題（移した・取り下げた問題）も、黙って落とさない。
    others = [
        (task_id, title) for task_id, title in record.titles.items() if task_id not in offered_ids
    ]
    with console.database.unit_of_work() as uow:
        submitted = _submitted_by_task(uow, course, [TaskId(task_id) for task_id in offered_ids])
    submitted = {task_id: people & enrolled for task_id, people in submitted.items()}

    problems = []
    for task_id, title in [*offered, *others]:
        people = record.work.get(task_id, {})
        problems.append(
            ProblemRow(
                task_id=task_id,
                title=title,
                openable=task_id in in_unit,
                worked=len(people),
                pasted=sum(1 for w in people.values() if w.summary.pastes_external),
                flagged=sum(1 for w in people.values() if w.flags),
                submitted=len(submitted.get(task_id, ())),
            )
        )

    rows: list[LearnerRow] = []
    quiet: list[Learner] = []
    for learner in record.learners:
        learner_id = str(learner.user_id)
        total = Work()
        for people in record.work.values():
            if learner_id in people:
                total.add(people[learner_id])
        submitted_tasks = sum(1 for people in submitted.values() if learner_id in people)
        opened = record.sessions.get(learner_id, 0)
        if not opened and not submitted_tasks:
            quiet.append(learner)
            continue
        rows.append(
            LearnerRow(
                learner=learner,
                work=total,
                missing=record.missing.get(learner_id, 0),
                unreadable=record.unreadable.get(learner_id, 0),
                submitted_tasks=submitted_tasks,
            )
        )
    return {
        "problems": problems,
        "rows": _sorted_rows(rows, sort),
        "quiet": sorted(quiet, key=lambda learner: learner.login),
        "task_count": len(offered),
        "session_count": sum(record.sessions.values()),
    }


def task_page_data(
    console: Any, course: Course, group: UnitGroup, task_id: TaskId, root: Any, sort: str
) -> dict[str, Any]:
    """問題の画面に渡す値。"""
    record = _load_record(console, course, group, root)
    people = record.work.get(str(task_id), {})
    submissions: dict[str, list[dict[str, Any]]] = {}
    for row in _submissions(console, course, task_ids=[task_id]).values():
        submissions.setdefault(row["learner_id"], []).append(row)

    rows: list[LearnerRow] = []
    quiet: list[Learner] = []
    for learner in record.learners:
        learner_id = str(learner.user_id)
        work = people.get(learner_id)
        mine = submissions.get(learner_id, [])
        if work is None and not mine:
            quiet.append(learner)
            continue
        # 提出日時の順。回数は版ごとに数えるので、回数で並べると混ざる（学習者の画面と同じ）。
        mine.sort(key=lambda row: row["submitted_at"])
        rows.append(
            LearnerRow(
                learner=learner,
                work=work or Work(),
                missing=record.missing.get(learner_id, 0),
                unreadable=record.unreadable.get(learner_id, 0),
                submissions=mine,
            )
        )
    return {
        "rows": _sorted_rows(rows, sort),
        "quiet": sorted(quiet, key=lambda learner: learner.login),
        # 問題が分からない記録はこの問題の数字に入らない。**黙らせない。**
        "unknown_learners": len(record.work.get(UNKNOWN_TASK, {})),
        "session_count": sum(record.sessions.values()),
    }


@dataclass(frozen=True)
class UnitIndexRow:
    """問題セットの一覧の 1 行。**索引だけで作る。**"""

    key: str
    label: str
    unit: str | None
    tasks: tuple[tuple[str, str], ...]
    learners: int
    sessions: int
    last: datetime | None


def _unit_of(key: str, course: Course, units: tuple[UnitGroup, ...]) -> UnitGroup:
    """鍵から問題セットを引く。課題の無くなったセット（記録だけが残る）は空の形で返す。"""
    return find_unit(units, key) or empty_unit(key, course)


def unit_index(uow: Any, course: Course) -> tuple[list[UnitIndexRow], int]:
    """問題セットの一覧と、受講者の数。エディタを使わないセットは並べない。"""
    units = load_units(uow, course)
    enrolled = {learner.user_id for learner in load_learners(uow, course)}
    sessions = [s for s in uow.ide_activity.course_sessions(course.id) if s.learner_id in enrolled]
    by_unit: dict[str | None, list[IdeSession]] = {}
    for session in sessions:
        by_unit.setdefault(session.unit, []).append(session)

    def row(group: UnitGroup) -> UnitIndexRow:
        mine = by_unit.get(group.unit, [])
        return UnitIndexRow(
            key=group.key,
            label=group.label,
            unit=group.unit,
            tasks=tuple((str(task.id), task.title) for task in _offered(group)),
            learners=len({s.learner_id for s in mine}),
            sessions=len(mine),
            last=max((s.started_at for s in mine), default=None),
        )

    rows = [
        row(group)
        for group in units
        if group.unit in by_unit
        or any(task.answer_mode is AnswerMode.EDITOR for task, _version in group.tasks)
    ]
    # 課題が無くなったセットの記録も、ここから辿れるようにする（記録は課題より長く残る）。
    known = {group.unit for group in units}
    for unit in sorted(u for u in by_unit if u not in known and u is not None):
        rows.append(row(empty_unit(quote(unit, safe=""), course)))
    return rows, len(enrolled)


def register(templates: Jinja2Templates) -> APIRouter:
    router = APIRouter()

    def _section(course: Course) -> dict[str, str]:
        return {"label": "作業の記録", "href": f"/courses/{course.id}/activity"}

    @router.get("/courses/{course_id}/activity/units", response_class=HTMLResponse)
    def units_page(request: Request, course_id: str) -> HTMLResponse:
        """問題セットの一覧。**索引だけで作る。**"""
        me, course = _instructor(request, course_id)
        console = request.app.state.aijudge
        with console.database.unit_of_work() as uow:
            rows, learners = unit_index(uow, course)
        _audit(request, me, str(course.id), str(course.id), "問題セットの一覧", {})
        return templates.TemplateResponse(
            request,
            "activity_units.html",
            {
                "me": me,
                "course": course,
                "section": _section(course),
                "rows": rows,
                "learner_count": learners,
                "view": "units",
            },
        )

    @router.get("/courses/{course_id}/activity/units/{unit:path}", response_class=HTMLResponse)
    def unit_page(request: Request, course_id: str, unit: str, sort: str = "login") -> HTMLResponse:
        """1 つの問題セット。問題ごとの人数と、学習者ごとの合計。"""
        from .manage.common import _normalized_unit

        me, course = _instructor(request, course_id)
        console = request.app.state.aijudge
        chosen = sort if sort in SORT_LABELS else "login"
        key = _normalized_unit(unit)
        with console.database.unit_of_work() as uow:
            group = _unit_of(key, course, load_units(uow, course))
        root = activity_root()
        data = unit_page_data(console, course, group, root, chosen)
        # 課題も記録も無い鍵は、存在しない問題セットである。
        if not group.tasks and not data["session_count"]:
            raise HTTPException(status_code=404, detail="問題セットが見つかりません")
        _audit(
            request,
            me,
            str(course.id),
            str(course.id),
            "問題セット",
            {"unit": key, "sessions": data["session_count"]},
        )
        return templates.TemplateResponse(
            request,
            "activity_unit.html",
            {
                "me": me,
                "course": course,
                "section": _section(course),
                "trail": ({"label": group.label},),
                "group": group,
                "configured": root is not None,
                "sort": chosen,
                "sorts": SORT_LABELS,
                "unknown_task": UNKNOWN_TASK,
                **data,
            },
        )

    @router.get("/courses/{course_id}/activity/tasks/{task_id}", response_class=HTMLResponse)
    def task_page(
        request: Request, course_id: str, task_id: str, sort: str = "login"
    ) -> HTMLResponse:
        """1 つの問題。学習者ごとの作業と提出。再生はここから開く。"""
        me, course = _instructor(request, course_id)
        console = request.app.state.aijudge
        chosen = sort if sort in SORT_LABELS else "login"
        with console.database.unit_of_work() as uow:
            units = load_units(uow, course)
        found = next(
            (
                (group, task)
                for group in units
                for task, _version in group.tasks
                if str(task.id) == task_id
            ),
            None,
        )
        # ほかのコースの課題も、ここでは無いものとして扱う。
        if found is None:
            raise HTTPException(status_code=404, detail="問題が見つかりません")
        group, task = found
        root = activity_root()
        data = task_page_data(console, course, group, task.id, root, chosen)
        _audit(
            request,
            me,
            str(course.id),
            str(course.id),
            "問題",
            {"task_id": task_id, "sessions": data["session_count"]},
        )
        return templates.TemplateResponse(
            request,
            "activity_task.html",
            {
                "me": me,
                "course": course,
                "section": _section(course),
                "trail": (
                    {
                        "label": group.label,
                        "href": f"/courses/{course.id}/activity/units/{group.key}",
                    },
                    {"label": task.title},
                ),
                "group": group,
                "task": task,
                "siblings": _offered(group),
                "configured": root is not None,
                "sort": chosen,
                "sorts": SORT_LABELS,
                **data,
            },
        )

    return router
