"""課題の運用（段階 4-12）。

設定・日程・実行上限・並び・移動とコピー・取り下げ・削除・確定・再採点。

出題の設定と状態の操作で、**本文には触らない**（版を増やさない）。移動・名前の変更・
コピーの規則は `aijudge_course_admin.task_relocation`、取り下げと削除の規則は
`aijudge_course_admin.tasks` が持ち、ここは画面と繋ぐだけ。`manage/__init__.py` の
`register()` が、元のルートがあった位置でここの `register` を呼ぶ。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from aijudge_audit import AuditAction
from aijudge_core import MAX_TASK_CASE_TIMEOUT_SECONDS, MIN_JUSTIFICATION_LENGTH, Task
from aijudge_core.ids import CourseId, TaskId
from aijudge_course_admin.errors import AdminError
from aijudge_course_admin.finalization import finalize_task
from aijudge_course_admin.task_relocation import copy_task as copy_task_as
from aijudge_course_admin.task_relocation import move_task as relocate_task
from aijudge_course_admin.tasks import delete as delete_task
from aijudge_course_admin.tasks import withdraw as withdraw_task
from aijudge_submission import SubmissionService

from .. import notices
from ..audit_context import recorder_for
from ..overview import unit_key
from ..urls import RedirectResponse
from .common import _console, _first_error, _parse_when, _plain, _require_instructor
from .task_page import _chosen_suffixes, _failed_without_run, _task_of

#: 実行時間の上限（#491）が範囲外のときの知らせ。
_CASE_TIMEOUT_REFUSED = (
    f"実行時間の上限は 0 より大きく {MAX_TASK_CASE_TIMEOUT_SECONDS:g} 秒以下の数で入れてください"
)


def _unit_href(course_id: str, task) -> str:
    """その課題が属する回のページ。

    課題を触る操作（締切・一括確定・追加）は**その回のページから来る**ので、
    そこへ戻す。コースのトップに返すと、教員は毎回同じ回を開き直すことになる。
    """
    return f"/manage/courses/{course_id}/units/{unit_key(task)}"


def register(router: APIRouter, templates: Jinja2Templates) -> None:
    """この領域のルートを `router` に登録する（登録の位置は呼ぶ側が決める）。"""

    @router.post("/courses/{course_id}/tasks/{task_id}/finalize")
    def finalize_remaining(
        request: Request,
        course_id: str,
        task_id: str,
        justification: Annotated[str, Form()] = "",
    ) -> Response:
        """この課題の未確定分をまとめて確定する。

        **根拠説明を必須にする。** 学習者にそのまま表示される。個別に読んで
        いない成績を確定させる操作なので、何を根拠にそうしたのかが残らないと
        学習者は何も分からない（設計原則 P4 を一括操作にも適用する）。

        未対応の異議申立は確定しない。そこは 1 件ずつ読むべきものとして
        待ち行列に残す。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        text = justification.strip()
        if len(text) < MIN_JUSTIFICATION_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"確定の根拠を {MIN_JUSTIFICATION_LENGTH} 文字以上で書いてください"
                    "（学習者に表示されます）"
                ),
            )

        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            if task is None or task.course_id != CourseId(course_id):
                raise HTTPException(status_code=404, detail="課題が見つかりません")

        try:
            outcome = finalize_task(
                console.database,
                task_id=TaskId(task_id),
                actor_id=me.user_id,
                justification=text,
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # 表示のために保持する。**コースを添える**（Console は全利用者で共有で、
        # 添えないと別コースの教員に他コースの課題名が出る）。
        console.notices.put(me.user_id, course_id, notices.FINALIZED, outcome)
        return RedirectResponse(_unit_href(course_id, task), status_code=303)

    @router.post("/courses/{course_id}/tasks/{task_id}/regrade")
    def regrade_task(request: Request, course_id: str, task_id: str) -> Response:
        """いまの版で採点し直す。**教員が押したときだけ動く。**

        実施中に課題を訂正すると、訂正前に出した提出は古い版の基準で付いた
        成績のまま残る。直す道が無ければ、誤った問題文で付いた点がそのまま
        成績になる ── かといって訂正のたびに自動で積み直すと、**誰も押して
        いない再採点で成績が動く**（設計原則 P5）。だから明示的な操作にする。

        **これはレビューの経路ではない。** ADR 0007 が切り離したのは
        「blind 採点や開示が採点を起動する」ことで、測定用の入力が採点の前提に
        戻るのを防ぐためだった。ここは作問・運用の操作で、積むのはジョブだけ
        ── 採点そのものはワーカーが走らせる（提出時と同じ経路）。

        **確定済みの提出は動かさない**（`_regradable`）。過去の採点も消えない
        ── 新しい採点が終わった時点で旧採点に `superseded_by` が入る（P8）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            # **学習者に出ている版で採点し直す。** 承認待ちの版で採点すると、
            # 誰も見ていない基準の点が成績になる（#48）。
            version = uow.tasks.latest_published_version(TaskId(task_id))
        if task is None or task.course_id != CourseId(course_id):
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        if version is None:
            raise HTTPException(
                status_code=409, detail="承認済みの版がありません（先に承認してください）"
            )

        service = SubmissionService(console.database.unit_of_work, console.store)
        with console.database.unit_of_work() as uow:
            rows = uow.reviews.unfinalized_for_task(task.id)
            failed = _failed_without_run(uow, task)
        queued = 0
        targets = [
            submission
            for submission, run, _request in rows
            if run.context.task_version_id != version.id
        ] + list(failed)
        for submission in targets:
            service.request_regrade(
                tenant_id=course.tenant_id,
                submission_id=submission.id,
                # **再採点は、回す先の版の規則で走らせる**（#195・#264）。
                # コースの値を渡すと、混在コースで別の科目の規則で採点し直す
                # ことになる ── 再採点は「同じ課題を新しい版で見直す」操作で
                # あって、課題の種類を変える操作ではない。
                subject_profile=version.subject_profile,
                task_version_id=version.id,
            )
            queued += 1
        console.notices.put(me.user_id, course.id, notices.REGRADED, queued, scope=task_id)
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=regraded#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/withdraw")
    def withdraw_task_route(
        request: Request,
        course_id: str,
        task_id: str,
        restore: Annotated[str, Form()] = "",
    ) -> Response:
        """出題を取り下げる（`restore` で取り消す）。**消さない。**

        採点結果は課題版を指しているので（P8）、提出のある課題を消すと過去の
        成績の出所が失われる。知識要素で決めたのと同じ区別で
        （`aijudge_course_admin.kc`）、使われたものは取り下げ、一度も使われていない
        ものだけを消す。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        _task_of(console, course, task_id)

        try:
            withdraw_task(console.database, task_id=TaskId(task_id), restore=bool(restore.strip()))
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        saved = "restored" if restore.strip() else "withdrawn"
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved={saved}#saved", status_code=303
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/delete")
    def delete_task_route(request: Request, course_id: str, task_id: str) -> Response:
        """**提出が 1 件も無い課題だけを消す。** 判定は `aijudge_course_admin.tasks`。

        提出があれば断り、取り下げを案内する（規則の置き場所を 1 つにする）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)

        try:
            delete_task(console.database, task_id=TaskId(task_id))
        except AdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        unit = unit_key(task)
        return RedirectResponse(
            f"/manage/courses/{course_id}/units/{unit}?saved=task_deleted#saved", status_code=303
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/move")
    def move_task(
        request: Request,
        course_id: str,
        task_id: str,
        direction: Annotated[str, Form()] = "up",
    ) -> Response:
        """課題の並びを 1 つ入れ替える。

        **数字を打たせない。** 出題順は「この問題セットの中で何番目か」で
        あって、教員が意識するのは前後関係だけである。数字で持たせると、
        1 問差し込むたびに全部を打ち直すことになる。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        with console.database.unit_of_work() as uow:
            tasks = [
                task
                for task in uow.tasks.list_for_course(CourseId(course_id))
                if unit_key(task) == unit_key(uow.tasks.get_task(TaskId(task_id)))
            ]
            ordered = sorted(tasks, key=lambda item: item.sort_key)
            index = next((i for i, task in enumerate(ordered) if str(task.id) == task_id), None)
            if index is None:
                raise HTTPException(status_code=404, detail="課題が見つかりません")
            swap = index - 1 if direction == "up" else index + 1
            if 0 <= swap < len(ordered):
                first, second = ordered[index], ordered[swap]
                # 位置を入れ替える。番号が無い課題には並び順から与える。
                first_position = first.position or index + 1
                second_position = second.position or swap + 1
                uow.tasks.save_task(first.model_copy(update={"position": second_position}))
                uow.tasks.save_task(second.model_copy(update={"position": first_position}))
                uow.commit()
            key = unit_key(ordered[index])
        return RedirectResponse(
            f"/manage/courses/{course_id}/units/{key}?saved=order#saved", status_code=303
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/unit")
    def move_task_to_unit(
        request: Request,
        course_id: str,
        task_id: str,
        unit: Annotated[str, Form()] = "",
        name: Annotated[str, Form()] = "",
    ) -> Response:
        """課題を別の問題セットへ移す（`name` を変えれば名前の変更も）。

        **キーを付け替える**（`<問題セット>/<名前>`・2026-09-27）。頭は問題セットを
        表すので、付け替えないと test5 にある課題のキーが `test4/…` のままになる。
        課題 ID はキーから導かれるので、**学生の提出がある課題は移せない**
        ── 規則と付け替えは `aijudge_course_admin.task_relocation` が持つ。
        お試しの提出は妨げず、付け替えのときに消える。

        **日程は移った先に揃える。** セットの中で締切がずれると、学習者にも
        教員にも「この回はいつまでか」が言えなくなる（`set_unit_schedule` と
        同じ理由）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)

        try:
            moved = relocate_task(
                console.database,
                task_id=task.id,
                unit=unit,
                name=name,
                artifact_store=console.store,
            )
        except AdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # 同じセットの中で名前だけ変えたなら課題の画面へ、移したなら移動先へ。
        # **元の URL はもう無い**（課題 ID が変わった）。
        if moved.task.unit == task.unit:
            return RedirectResponse(
                f"/manage/courses/{course_id}/tasks/{moved.task.id}/edit?saved=renamed#saved",
                status_code=303,
            )
        return RedirectResponse(
            f"/manage/courses/{course_id}/units/{unit_key(moved.task)}?saved=moved#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/copy")
    def copy_task_to_unit(
        request: Request,
        course_id: str,
        task_id: str,
        unit: Annotated[str, Form()] = "",
        name: Annotated[str, Form()] = "",
    ) -> Response:
        """課題を名前を付けて別の問題セットへ**コピー**する。元は残る。

        学生の提出がある課題は移せないので、別のセットで使うにはこちらを通る
        （2026-09-27）。写すのは中身（全版）と設定で、提出と採点は写さない。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)

        try:
            copied = copy_task_as(console.database, task_id=task.id, unit=unit, name=name)
        except AdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{copied.task.id}/edit?saved=copied#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/schedule")
    def set_task_schedule(
        request: Request,
        course_id: str,
        task_id: str,
        opens_at: Annotated[str, Form()] = "",
        submissions_open_at: Annotated[str, Form()] = "",
        due_at: Annotated[str, Form()] = "",
        grading_starts_at: Annotated[str, Form()] = "",
        accepts_until: Annotated[str, Form()] = "",
    ) -> Response:
        """**この課題 1 件の日程**を直す。

        日程を決めるのは問題セットである（`set_unit_schedule`）── ここは
        **揃っていないものを直すための口**であって、課題ごとに違う締切を
        置くための機能ではない。画面もそう書いてある。

        それでも 1 件ずつ直せる必要があるのは、ばらつきが実際に起きるから
        である ── 取り込んだ課題は 1 件ずつ日程を持っていることがあり、
        あとから足した課題はセットの日程を持っていない。「セットの日程を
        保存し直して全部に当てる」は、**当てたくない課題まで動かす**
        （試験の回を含むセットで採点開始が揃っていない、など）。

        版は上がらない。**日程は課題の内容ではない**ので、直しても過去の
        採点基準は変わらない（P8 の対象外）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)

        update = {
            "opens_at": _parse_when(opens_at),
            "submissions_open_at": _parse_when(submissions_open_at),
            "due_at": _parse_when(due_at),
            "grading_starts_at": _parse_when(grading_starts_at),
            "accepts_until": _parse_when(accepts_until),
        }
        try:
            # **`model_copy` を使わない。** 検証を走らせないので、締切が公開より
            # 前の課題がそのまま保存される（`_update_unit` と同じ理由）。
            updated = Task.model_validate(task.model_dump() | update)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=_first_error(exc)) from None

        with console.database.unit_of_work() as uow:
            uow.tasks.save_task(updated)
            # **締切は誰がいつ動かしたかを言えないといけない**（ADR 0013）。
            # セット単位の記録（`_update_unit`）と同じ理由で、1 件の操作も残す。
            recorder_for(uow, request, me).record(
                AuditAction.TASK_UPDATED,
                target_type="task",
                target_id=str(task.id),
                summary="課題の日程を変えた",
                detail={
                    "course_id": course_id,
                    "field": "task_schedule",
                    "changed": {
                        name: {"before": _plain(getattr(task, name, None)), "after": _plain(value)}
                        for name, value in update.items()
                    },
                },
            )
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=task_schedule#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/settings")
    async def save_task_settings(request: Request, course_id: str, task_id: str) -> Response:
        """「この問題の設定」をまとめて保存する（日程・提出形式・実行時間の上限）。

        問題のページの操作のタブを詰めたとき（2026-09-27）、版を上げない 3 つの値を
        1 枚のフォームにした。**版は上がらない** ── どれも課題の内容ではない。

        **監査は変わったものごとに、今までと同じ文言で残す**（`set_task_schedule`・
        `set_task_case_timeout` と同じ `summary`・`field`）。締切は誰がいつ動かしたかを
        言えないといけない（ADR 0013）ので、まとめて保存しても日程の記録は日程の記録。
        変わっていないものは記録しない（触っていない値の行を積まない）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)
        form = await request.form()

        def text(name: str) -> str:
            return str(form.get(name) or "")

        schedule = {
            name: _parse_when(text(name))
            for name in (
                "opens_at",
                "submissions_open_at",
                "due_at",
                "grading_starts_at",
                "accepts_until",
            )
        }
        update: dict[str, object] = dict(schedule)
        # 提出形式。印（`formats`）付きで来たときだけ置き換え、空は断る（`_chosen_suffixes`）。
        if form.get("formats"):
            update["accepted_suffixes"] = _chosen_suffixes(
                [str(v) for v in form.getlist("suffix")], "1", course
            )
        # 実行時間の上限は**欄があるときだけ**（コードを走らせない課題には出さない）。
        if "case_timeout_seconds" in form:
            raw = text("case_timeout_seconds").strip()
            try:
                update["case_timeout_seconds"] = float(raw) if raw else None
            except ValueError:
                raise HTTPException(status_code=400, detail=_CASE_TIMEOUT_REFUSED) from None
        try:
            # **`model_copy` を使わない。** 検証を走らせないので、締切が公開より
            # 前の課題がそのまま保存される（`set_task_schedule` と同じ理由）。
            updated = Task.model_validate(task.model_dump() | update)
        except ValidationError as exc:
            if any(e["loc"] == ("case_timeout_seconds",) for e in exc.errors()):
                raise HTTPException(status_code=400, detail=_CASE_TIMEOUT_REFUSED) from None
            raise HTTPException(status_code=400, detail=_first_error(exc)) from None

        def changed(names) -> dict[str, dict[str, object]]:
            return {
                name: {
                    "before": _plain(getattr(task, name)),
                    "after": _plain(getattr(updated, name)),
                }
                for name in names
                if getattr(task, name) != getattr(updated, name)
            }

        records = [
            ("課題の日程を変えた", "task_schedule", changed(schedule)),
            ("課題の提出形式を変えた", "accepted_suffixes", changed(["accepted_suffixes"])),
            (
                "課題の実行時間の上限を変えた",
                "case_timeout_seconds",
                changed(["case_timeout_seconds"]),
            ),
        ]
        with console.database.unit_of_work() as uow:
            uow.tasks.save_task(updated)
            recorder = recorder_for(uow, request, me)
            for summary, field, diff in records:
                if not diff:
                    continue
                recorder.record(
                    AuditAction.TASK_UPDATED,
                    target_type="task",
                    target_id=str(task.id),
                    summary=summary,
                    detail={"course_id": course_id, "field": field, "changed": diff},
                )
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=task_settings#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/case-timeout")
    def set_task_case_timeout(
        request: Request,
        course_id: str,
        task_id: str,
        case_timeout_seconds: Annotated[str, Form()] = "",
    ) -> Response:
        """**この課題だけ**テストケース 1 件の実行時間の上限を変える（#491）。

        数値計算のように、正しい解でも時間のかかる問題のため。空欄は既定
        （科目・コースの値）に戻す。版は上がらない ── 上限はコースの採点設定でも
        版を作らずに変えられ、それを課題単位に細かくしたもの。適用した値は採点の
        記録（`model_params`）に残る。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)

        raw = case_timeout_seconds.strip()
        try:
            seconds = float(raw) if raw else None
            updated = Task.model_validate(task.model_dump() | {"case_timeout_seconds": seconds})
        except ValueError:
            raise HTTPException(status_code=400, detail=_CASE_TIMEOUT_REFUSED) from None

        with console.database.unit_of_work() as uow:
            uow.tasks.save_task(updated)
            recorder_for(uow, request, me).record(
                AuditAction.TASK_UPDATED,
                target_type="task",
                target_id=str(task.id),
                summary="課題の実行時間の上限を変えた",
                detail={
                    "course_id": course_id,
                    "field": "case_timeout_seconds",
                    "changed": {
                        "case_timeout_seconds": {
                            "before": task.case_timeout_seconds,
                            "after": seconds,
                        }
                    },
                },
            )
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=task_case_timeout#saved",
            status_code=303,
        )
