"""出題先の名簿（`docs/design/task-visibility.md` §3.5）。

**画面・API・CLI はすべてここを通る。** 名簿の検証と監査の記録を経路ごとに
書くと、片方でしか効かない検査が必ず生まれる（#119 の教訓。動画の 1 発の
送信は関門を写して持ち、学内限定だけが落ちた）。

呼ぶ側が作業単位（`uow`）と監査の記録係（`recorder`）を渡し、確定（commit）も
呼ぶ側が行う。ここは 1 つの作業単位の中で、名簿と監査記録を一緒に書く ──
監査が書けなければ操作ごと失敗する（ADR 0016）。

名簿は**置き換え**でしか書かない。足し引きの操作を持たないので、同じ要求を
2 度流しても結果が同じになる（スクリプトの再実行が安全）。代わりに、何が
足されて何が外れたかを返す。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from aijudge_audit import AuditAction, AuditRecorder
from aijudge_core import Course, CourseGroup, Role, Task, new_id
from aijudge_core.ids import CourseGroupId

from .operations import AdminError

# 監査記録の `detail` に並べる login の上限。`detail` は 4000 字まで
# （`aijudge_audit.MAX_DETAIL_CHARS`）で、名簿を丸ごと写す場所ではない。
# 超えたぶんは件数だけ残す。
_AUDIT_LOGIN_LIMIT = 100


class GroupError(AdminError):
    """名簿の操作を続けられない。"""


class UnknownLogins(GroupError):
    """このコースの学習者ではない login が混じっている。

    **一部だけ登録しない。** 登録できた分だけ保存すると、漏れた学生は
    追試の問題セットが見えないまま試験を迎え、誰も気づかない。
    """

    def __init__(self, logins: Sequence[str]) -> None:
        self.logins = tuple(logins)
        shown = ", ".join(self.logins[:10])
        more = f" ほか {len(self.logins) - 10} 件" if len(self.logins) > 10 else ""
        super().__init__(f"このコースの学習者ではない login があります: {shown}{more}")


class GroupNotFound(GroupError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"グループ {name!r} はありません")


class GroupInUse(GroupError):
    """出題先として使われているグループは消せない。

    消すと、その課題の出題先が「存在しない名簿」になる ── 誰にも見えない
    課題が黙って残る。先に問題セットの出題先から外す。
    """

    def __init__(self, name: str, tasks: Sequence[Task]) -> None:
        self.name = name
        self.tasks = tuple(tasks)
        titles = "、".join(task.title for task in self.tasks[:5])
        super().__init__(
            f"グループ {name!r} は出題先として使われています（{titles}）。"
            "先に問題セットの出題先から外してください"
        )


@dataclass(frozen=True)
class GroupSummary:
    group: CourseGroup
    members: int
    # 出題先として使っている課題の数。0 なら消せる。
    used_by: int


@dataclass(frozen=True)
class MembersReplaced:
    """置き換えの結果。**何が起きたかを返す**（API の `created` と同じ理由）。"""

    group: CourseGroup
    created: bool
    added: tuple[str, ...]
    removed: tuple[str, ...]
    members: tuple[str, ...]


def list_groups(uow: object, course: Course) -> tuple[GroupSummary, ...]:
    tasks = uow.tasks.list_for_course(course.id)  # type: ignore[attr-defined]
    return tuple(
        GroupSummary(
            group=group,
            members=len(uow.identity.group_members(group.id)),  # type: ignore[attr-defined]
            used_by=sum(1 for task in tasks if group.id in task.audience_group_ids),
        )
        for group in uow.identity.list_groups(course.id)  # type: ignore[attr-defined]
    )


def members_of(uow: object, group: CourseGroup) -> tuple[str, ...]:
    """名簿を login の順で。"""
    ids = uow.identity.group_members(group.id)  # type: ignore[attr-defined]
    logins = []
    for user_id in ids:
        user = uow.identity.get_user(user_id)  # type: ignore[attr-defined]
        if user is not None:
            logins.append(user.login)
    return tuple(sorted(logins))


def replace_group_members(
    uow: object,
    recorder: AuditRecorder,
    *,
    course: Course,
    name: str,
    logins: Iterable[str],
) -> MembersReplaced:
    """グループを作る（無ければ）・名簿を丸ごと置き換える。

    名簿に入れられるのは**そのコースの学習者だけ**。TA や教員は出題先に
    関係なく見える（`may_see`）ので、入れる意味が無い ── 入れられると
    「TA を入れたから見えている」と読み違える。
    """
    wanted = _normalized(logins)
    group_name = CourseGroup(
        id=CourseGroupId(new_id("grp")), tenant_id=course.tenant_id, course_id=course.id, name=name
    ).name

    users = uow.identity.list_users(course.tenant_id, wanted)  # type: ignore[attr-defined]
    by_login = {user.login: user for user in users}
    unknown = [
        login
        for login in wanted
        if login not in by_login or not _is_learner(uow, course, by_login[login].id)
    ]
    if unknown:
        raise UnknownLogins(unknown)

    group = uow.identity.find_group(course.id, group_name)  # type: ignore[attr-defined]
    created = group is None
    if group is None:
        group = CourseGroup(
            id=CourseGroupId(new_id("grp")),
            tenant_id=course.tenant_id,
            course_id=course.id,
            name=group_name,
        )
        uow.identity.save_group(group)  # type: ignore[attr-defined]

    before = set(members_of(uow, group))
    after = set(wanted)
    uow.identity.set_group_members(  # type: ignore[attr-defined]
        group.id, frozenset(by_login[login].id for login in wanted)
    )
    added = tuple(sorted(after - before))
    removed = tuple(sorted(before - after))

    recorder.record(
        AuditAction.GROUP_UPDATED,
        target_type="course_group",
        target_id=str(group.id),
        summary=(
            f"出題先の名簿「{group.name}」を{'作った' if created else '置き換えた'}"
            f"（{len(after)} 名・追加 {len(added)}・削除 {len(removed)}）"
        ),
        detail={
            "course_id": str(course.id),
            "name": group.name,
            "created": created,
            "members": len(after),
            "added": list(added[:_AUDIT_LOGIN_LIMIT]),
            "removed": list(removed[:_AUDIT_LOGIN_LIMIT]),
            "truncated": len(added) > _AUDIT_LOGIN_LIMIT or len(removed) > _AUDIT_LOGIN_LIMIT,
        },
    )
    return MembersReplaced(
        group=group,
        created=created,
        added=added,
        removed=removed,
        members=tuple(sorted(after)),
    )


def delete_group(uow: object, recorder: AuditRecorder, *, course: Course, name: str) -> None:
    group = _find(uow, course, name)
    using = [
        task
        for task in uow.tasks.list_for_course(course.id)  # type: ignore[attr-defined]
        if group.id in task.audience_group_ids
    ]
    if using:
        raise GroupInUse(group.name, using)
    members = len(uow.identity.group_members(group.id))  # type: ignore[attr-defined]
    uow.identity.delete_group(group.id)  # type: ignore[attr-defined]
    recorder.record(
        AuditAction.GROUP_DELETED,
        target_type="course_group",
        target_id=str(group.id),
        summary=f"出題先の名簿「{group.name}」を消した（{members} 名）",
        detail={"course_id": str(course.id), "name": group.name, "members": members},
    )


def set_audience(
    uow: object,
    recorder: AuditRecorder,
    *,
    course: Course,
    tasks: Sequence[Task],
    names: Iterable[str],
    unit_label: str,
) -> tuple[Task, ...]:
    """問題セットの全課題の出題先を置き換える。**空は受講者全員。**

    どの課題が問題セットに入るかは呼ぶ側が決める（画面と API は URL の鍵で、
    CLI は `unit` で選ぶ）。出題先はセット単位で決める ── 課題ごとに違うと、
    同じ回の中で見える課題と見えない課題が混ざり、学習者には理由が読めない
    （`campus_only` と同じ扱い）。
    """
    if not tasks:
        raise GroupError("この問題セットには課題がありません")
    groups = [_find(uow, course, name) for name in dict.fromkeys(n.strip() for n in names) if name]
    ids = tuple(group.id for group in groups)

    saved = []
    before = {str(task.id): [str(g) for g in task.audience_group_ids] for task in tasks}
    for task in tasks:
        if task.course_id != course.id:
            raise GroupError("別のコースの課題が混じっています")
        # **作り直して検証を通す**（`model_copy` は検証を走らせない・`_update_unit`）。
        updated = Task.model_validate(task.model_dump() | {"audience_group_ids": ids})
        uow.tasks.save_task(updated)  # type: ignore[attr-defined]
        saved.append(updated)

    recorder.record(
        AuditAction.TASK_UPDATED,
        target_type="unit",
        target_id=f"{course.id}/{unit_label}",
        summary=(
            f"問題セットの出題先を変えた（{'、'.join(g.name for g in groups) or '受講者全員'}"
            f"・課題 {len(tasks)} 件）"
        ),
        detail={
            "course_id": str(course.id),
            "field": "audience",
            "changed": {
                "audience_group_ids": {
                    "before": sorted({g for ids_ in before.values() for g in ids_}),
                    "after": [str(g) for g in ids],
                },
            },
            "groups": [g.name for g in groups],
        },
    )
    return tuple(saved)


def _find(uow: object, course: Course, name: str) -> CourseGroup:
    group = uow.identity.find_group(course.id, name)  # type: ignore[attr-defined]
    if group is None:
        raise GroupNotFound(name)
    return group


def _is_learner(uow: object, course: Course, user_id) -> bool:
    enrollment = uow.identity.find_enrollment(course.id, user_id)  # type: ignore[attr-defined]
    return enrollment is not None and enrollment.role is Role.LEARNER


def _normalized(logins: Iterable[str]) -> tuple[str, ...]:
    """前後の空白と空行を落とし、重複を除く。**並びは保つ**（知らない login を
    出した順で返すため）。"""
    return tuple(dict.fromkeys(login.strip() for login in logins if login.strip()))
