"""問題セット（日程・答え方・学内限定・画面の静止画・受付・確定・再採点の一括）（段階 4-7）。

**日程と出し方は問題セットで揃える** ── 課題ごとに違う締切を持てると、同じセットの中で
締切がずれ、「この回はいつまでか」が言えなくなる。一括の更新は `_update_unit` を通り、
セットの全課題に同じ値が入る。`manage/__init__.py` の `register()` が、元の問題セットの
ルートがあった位置でここの `register` を呼ぶ（セット単位の確定 `finalize_unit` も
同じ位置に寄せた）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote, unquote

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from aijudge_audit import AuditAction
from aijudge_authoring.drafting import Difficulty
from aijudge_core import (
    DEFAULT_UPLOAD_SUFFIXES,
    HUMAN_SCORED,
    MIN_JUSTIFICATION_LENGTH,
    SUFFIX_GROUPS,
    AnswerMode,
    EvaluatorKind,
    GradeWindow,
    ReviewState,
    Task,
    TaskVersion,
    max_scores_by_version,
    parse_cidrs,
)
from aijudge_core.ids import CourseId
from aijudge_course_admin import groups as audience
from aijudge_course_admin import rubric
from aijudge_course_admin.answer_mode import editor_blockers, file_upload_required
from aijudge_course_admin.errors import AdminError
from aijudge_course_admin.finalization import finalize_tasks, pending_breakdown
from aijudge_course_admin.late_penalty import split_for_form, steps_from_form
from aijudge_course_admin.late_penalty import summarize as summarize_penalty
from aijudge_course_admin.tasks import clear_unit
from aijudge_grading import EvaluatorRegistry

from .. import notices
from ..audit_context import recorder_for, source_ip_of
from ..overview import empty_unit, find_unit, load_units, unit_key
from ..urls import RedirectResponse
from .common import (
    _can_edit,
    _console,
    _course_kcs,
    _first_error,
    _kc_keys_of,
    _normalized_unit,
    _parse_minutes,
    _parse_when,
    _plain,
    _require_instructor,
    _require_reader,
)
from .grading_views import _default_rubric_criteria, _evaluator_rows
from .messages import SAVED_MESSAGES


@dataclass(frozen=True)
class _Merged:
    """複数課題の確定結果を 1 つに畳んだもの。画面が読む形は 1 件と同じ。"""

    task: object
    finalized: int
    contested: int
    awaiting_human: int = 0
    ai_pending: int = 0


def _answer_mode_update(course, group, *, by_file: bool, by_editor: bool) -> dict:
    """答え方の 2 つのチェックを、課題に入れる値にする。**入れられない組は断る。**

    個別の保存（`set_unit_answer_mode`）とまとめて保存（`save_unit_settings`）が
    同じ関数を通る ── 断る条件を経路ごとに書くと、片方だけが緩む。
    `group` は検査の要るとき（エディタを入れる・ファイルを止める）だけ渡せばよい。
    """
    if not (by_file or by_editor):
        raise HTTPException(
            status_code=400,
            detail="ファイルとエディタの少なくとも一方を選んでください（どちらも無いと提出できません）",
        )
    if group is not None:
        blockers = editor_blockers(group.tasks, course) if by_editor else ()
        if blockers:
            raise HTTPException(
                status_code=409,
                detail="エディタにできません: " + "／".join(blockers),
            )
        # **動画を受ける課題があれば、ファイル選択は止められない**（2026-09-25）。
        # 動画はエディタの画面から出せないので、止めると出す道が無くなる。
        required = () if by_file else file_upload_required(group.tasks, course)
        if required:
            raise HTTPException(
                status_code=409,
                detail="ファイル選択での提出を止められません: " + "／".join(required),
            )
    mode = AnswerMode.EDITOR if by_editor else AnswerMode.UPLOAD
    return {"answer_mode": mode, "file_upload": by_file}


# 減点の入力欄に、いまの段のほかに足しておく空の行の数（コースの設定画面と同じ）。
PENALTY_BLANK_ROWS = 2


def _penalty_from_form(form, mode: str, first_percent: str):
    """画面の入力から、この回の減点の段を決める。

    `course` は「コースの設定に従う」（None）。`unit` は「この回だけ決める」で、
    **すべて空欄なら「減点しない」**（空のタプル）── コースに段があっても外せる。
    """
    if mode != "unit":
        return None
    try:
        return steps_from_form(
            first_percent,
            list(
                zip(
                    [str(v) for v in form.getlist("penalty_hours")],
                    [str(v) for v in form.getlist("penalty_percent")],
                    strict=False,
                )
            ),
        )
    except AdminError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _apply_unit_update(
    uow, request: Request, me, course_id: str, key: str, *, update: dict, saved: str
) -> None:
    """`_update_unit` の中身。**書くだけで確定しない**（`commit` は呼ぶ側）。

    まとめて保存（`save_unit_settings`）が出題先と同じ作業単位で書けるように
    分けてある ── どちらかが断られたら、もう一方も書かれない。

    **`model_copy` を使わない。** あれは検証を走らせないので、締切が公開より
    前の課題がそのまま保存され、次に読むときに初めて落ちる（実際にそうなった）。
    作り直して検証を通す。
    """
    tasks = [
        task for task in uow.tasks.list_for_course(CourseId(course_id)) if unit_key(task) == key
    ]
    if not tasks:
        raise HTTPException(status_code=404, detail="この問題セットには課題がありません")
    before: dict[str, object] = {}
    for task in tasks:
        try:
            updated = Task.model_validate(task.model_dump() | update)
        except ValidationError as exc:
            # 日程の前後関係は模型が見ている（`Task._check_schedule`）。
            raise HTTPException(status_code=400, detail=_first_error(exc)) from None
        before |= {key: getattr(task, key, None) for key in update}
        uow.tasks.save_task(updated)
    # **締切と自動確定の猶予はここを通る。** どちらも成績がいつ閉じるかを
    # 決める値で（ADR 0013・ADR 0014）、後から「誰がいつ動かしたか」を
    # 言えないと、締切を巡る問い合わせに答えられない。
    recorder_for(uow, request, me).record(
        AuditAction.TASK_UPDATED,
        target_type="unit",
        # **記録が名指すのは問題セットそのもので、URL での姿ではない。**
        # `key` は経路に載せるために percent-encode してあり、日本語の
        # 名前だと 1 文字が 9 字に膨らむ ── 「第3回 配列とポインタ入門」で
        # 103 字になり、コースの id と合わせて列（128 字）を超える。
        # 復号すれば `tasks.unit` の幅（64 字）に収まり、記録としても
        # 読める（符号化された鍵は人にも機械にも引きにくい）。
        target_id=f"{course_id}/{unquote(key)}",
        summary=f"問題セットの設定を変えた（{saved}・課題 {len(tasks)} 件）",
        detail={
            "course_id": course_id,
            "field": saved,
            "changed": {
                name: {"before": _plain(before.get(name)), "after": _plain(value)}
                for name, value in update.items()
            },
        },
    )


def _campus_configured(console, me) -> bool:
    """テナントに学内の範囲が 1 件でも入っているか（#333）。"""
    return bool(_campus_ranges(console, me))


def _campus_ranges(console, me) -> tuple:
    """テナント管理者が登録した範囲（注釈つき）。**読めるものだけ**（`parse_cidrs`）。"""
    with console.database.unit_of_work() as uow:
        settings = uow.identity.get_campus_networks(me.tenant_id)
    if settings is None:
        return ()
    return tuple(entry for entry in settings.ranges if parse_cidrs([entry.cidr]))


def _chosen_ranges(console, me, chosen: list[str]) -> tuple[str, ...]:
    """学内限定にするとき選んだ範囲を検査する。**1 つ以上・登録済みのものだけ。**

    選ばせるのは、学内限定を入れたのに「どこから受け付けるのか」が決まって
    いない設定を作らないため。登録の無い範囲を通すと、あとで管理者が書き直した
    ときに黙って効かなくなる。
    """
    known = {entry.cidr for entry in _campus_ranges(console, me)}
    picked = tuple(sorted(set(chosen)))
    if not picked:
        raise HTTPException(
            status_code=400,
            detail="学内からだけ受け付けるときは、受け付ける範囲を 1 つ以上選んでください",
        )
    unknown = [c for c in picked if c not in known]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail="登録されていない範囲です: " + "、".join(unknown[:3]),
        )
    return picked


def _campus_choices(console, me, request: Request, group) -> dict:
    """受け付ける範囲の選択肢。いまの接続元が当たる範囲に印を付ける。"""
    import ipaddress

    ranges = _campus_ranges(console, me)
    source = source_ip_of(request)
    here: str | None = None
    if source:
        try:
            address = ipaddress.ip_address(source.strip())
        except ValueError:
            address = None
        for entry in ranges if address else ():
            network = ipaddress.ip_network(entry.cidr, strict=False)
            if address.version == network.version and address in network:
                here = entry.cidr
                break
    chosen = set(group.campus_ranges)
    known = {entry.cidr for entry in ranges}
    return {
        "ranges": ranges,
        "chosen": chosen,
        "source_ip": source,
        "here": here,
        # 選んであるのに登録から消えている範囲。**黙って落とさず見せる** ──
        # 全部消えていれば、この問題セットは誰も提出できない。
        "missing": sorted(chosen - known),
        # 範囲を選べるようになる前に学内限定にしたセット。全範囲として動いている。
        "legacy": group.campus_only and not chosen,
    }


def _clear_plan(console, course, group) -> dict[str, int]:
    """このセットを片付けると何がどうなるか。**変えずに数えるだけ。**"""
    if not group.tasks:
        return {"deleted": 0, "withdrawn": 0, "untouched": 0, "total": 0}
    report = clear_unit(console.database, course_id=course.id, unit=group.unit, dry_run=True)
    return {
        "deleted": len(report.deleted),
        "withdrawn": len(report.withdrawn),
        "untouched": len(report.untouched),
        "total": report.total,
    }


def _exam_state(console, course, group, now: datetime, *, user_id) -> dict[str, object]:
    """試験の問題セットの状態（#67）。

    **落ちたジョブを必ず出す。** 一括採点で 90 名分が一斉に流れて一部が
    落ちると、いまはログにしか出ない ── 気づかなければ、その提出だけ成績が
    欠けたまま確定する。
    """
    version_ids = {str(version.id) for _task, version in group.tasks}
    if not version_ids:
        return {"waiting_jobs": 0, "failed_jobs": (), "last_release": None}
    with console.database.unit_of_work() as uow:
        # **課題版で絞ってから引く**（#230）。コース全件を引いて Python で
        # 絞ると、絞り込みが `list_for_course` の上限の後ろに来て、押す前に
        # 出すこの件数から新しい提出が抜ける。
        ids = [s.id for s in uow.submissions.list_for_versions(sorted(version_ids))]
        failed = uow.jobs.failed_for(ids)
        # 待たせている件数は「流したら何件動くか」。押す前に出す。
        waiting = uow.jobs.waiting_count(ids, now)
    return {
        "waiting_jobs": waiting,
        "failed_jobs": failed,
        "last_release": console.notices.take(user_id, course.id, notices.JOBS_RELEASED),
    }


def _graded_by(version: TaskVersion) -> tuple[str, ...]:
    """課題の観点を何が判定するか。一覧の印に使う（複数ありうる）。

    - テストで判定: テストケースを走らせる評価器（`*_test_runner`）
    - 規則で判定: そのほかの決定的な評価器（文字列の照合・提出物の検査など）
    - AI が判定: 評価器を指名していない観点（どの AI 評価器も対象）と `*_ai_judge`
    - 教員が採点: 機械に採点させない観点（`HUMAN_SCORED`）

    名前で分けるのは、評価器の登録（エントリポイント）をこの画面が読まないため。
    AI の評価器は `_ai_judge`、テストは `_test_runner` で終わる名前に揃えてある。
    """
    found: dict[str, None] = {}
    for criterion in version.criteria:
        name = criterion.evaluator_id
        if name == HUMAN_SCORED:
            found["教員が採点"] = None
        elif name is None or name.endswith("_ai_judge"):
            found["AI が判定"] = None
        elif name.endswith("_test_runner"):
            found["テストで判定"] = None
        else:
            found["規則で判定"] = None
    order = ("テストで判定", "規則で判定", "AI が判定", "教員が採点")
    return tuple(label for label in order if label in found)


def _groups_of(console, course) -> tuple:
    with console.database.unit_of_work() as uow:
        return audience.list_groups(uow, course)


def _merged(outcomes) -> _Merged:
    return _Merged(
        task=outcomes[0].task if outcomes else None,
        finalized=sum(outcome.finalized for outcome in outcomes),
        contested=sum(outcome.contested for outcome in outcomes),
        awaiting_human=sum(outcome.awaiting_human for outcome in outcomes),
        ai_pending=sum(outcome.ai_pending for outcome in outcomes),
    )


def _parse_clear_points(raw: str) -> float | None:
    """クリア点。空欄はクリアの条件なし。"""
    text = raw.strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        raise HTTPException(status_code=400, detail="クリア点は数で入れてください") from None
    if value <= 0:
        raise HTTPException(status_code=400, detail="クリア点は 0 より大きい数です")
    return value


def _same_minute(left: datetime | None, right: datetime | None) -> bool:
    """画面の日時は分までなので、分までで比べる。秒を持つ値（取り込み）を
    開いて保存し直しただけで「変わった」ことにしない。"""
    if left is None or right is None:
        return left is right
    return left.replace(second=0, microsecond=0) == right.replace(second=0, microsecond=0)


def _unit_group(console, course, unit: str):
    """URL の鍵から問題セットを引く。**画面と同じまとめ方**（`unit_key`）。"""
    key = _normalized_unit(unit)
    with console.database.unit_of_work() as uow:
        units = load_units(uow, course)
    group = find_unit(units, key)
    if group is None:
        raise HTTPException(status_code=404, detail="問題セットが見つかりません")
    return group


def _update_unit(
    request: Request, course_id: str, unit: str, *, update: dict, saved: str
) -> Response:
    """問題セット内の全課題に同じ更新を当てる。"""
    from ..app import require_principal

    me = require_principal(request)
    _require_instructor(request, me, CourseId(course_id))
    console = _console(request)

    key = _normalized_unit(unit)
    with console.database.unit_of_work() as uow:
        _apply_unit_update(uow, request, me, course_id, key, update=update, saved=saved)
        uow.commit()
    return RedirectResponse(
        f"/manage/courses/{course_id}/units/{key}?saved={saved}#saved", status_code=303
    )


def register(router: APIRouter, templates: Jinja2Templates) -> None:
    """この領域のルートを `router` に登録する（登録の位置は呼ぶ側が決める）。"""

    @router.post("/courses/{course_id}/units")
    def open_unit(
        request: Request,
        course_id: str,
        unit: Annotated[str, Form()] = "",
    ) -> Response:
        """回のページを開く（無ければ空の回として開く）。

        新しい回を作る導線がこれである。**保存は伴わない** ── 回は課題が
        持つ属性であって、それ自体の記録は無い。最初の 1 問を足した時点で
        回が実在する。
        """
        from ..app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        key = quote(unit.strip() or "_", safe="")
        return RedirectResponse(f"/manage/courses/{course_id}/units/{key}", status_code=303)

    @router.get("/courses/{course_id}/units/{unit}", response_class=HTMLResponse)
    def unit_settings(request: Request, course_id: str, unit: str, saved: str = "") -> Response:
        """**1 回ぶん**の設定 ── その回の課題の締切、一括確定、課題の追加。

        まとまりの鍵は `unit`（`overview.unit_key`）。教員が用があるのは
        たいてい「いまの回」で、その単位で開けることが構成の分かりやすさに
        直結する。
        """
        from ..app import require_principal

        me = require_principal(request)
        course, role = _require_reader(request, me, CourseId(course_id))
        console = _console(request)

        # 課題ごとの未確定件数。**画面に出す。** 自動確定を設定したつもりで
        # cron を仕掛け忘れても、件数が減らないことで気づける。止まっている
        # 件数は分けて持つ ── 猶予中の待ちまで「要対応」と言わない（2026-10-01）。
        breakdown = pending_breakdown(console.database, course.id)
        pending, stalled = breakdown.total, breakdown.stalled
        now = datetime.now(UTC)
        with console.database.unit_of_work() as uow:
            # TA には公開前の秘匿の課題（試験）を出さない（`may_see`）。
            units = load_units(uow, course, pending=pending, stalled=stalled, now=now, viewer=role)
        # **知らない鍵でも 404 にしない。** 課題を 1 問も持たない回は
        # 「まだ何も無い回」であって存在しない回ではなく、ここが最初の
        # 1 問を足す場所になる。404 にすると新しい回を作る導線が無くなる。
        key = _normalized_unit(unit)
        group = find_unit(units, key) or empty_unit(key, course)

        # **同じ題名が 2 件あることを出す。** 以前は「テストケースを付けるには
        # 作り直してください」と書いてあり、そのとおりにすると別の課題が増えて
        # 同じ問題が 2 件並んだ（#43）。並んでいること自体を画面に出さないと、
        # 提出と採点がどちらに付いたのかを教員が追えない。
        titles: dict[str, int] = {}
        for task, _version in group.tasks:
            titles[task.title] = titles.get(task.title, 0) + 1

        # 課題ごとの知識要素（#292）。**付いていない課題の成績は習熟度に
        # 記録されない**ので、無いことも一覧で分かるようにする。
        with console.database.unit_of_work() as uow:
            kc_keys = {task.id: _kc_keys_of(uow, version) for task, version in group.tasks}
            # **学習者に出ているのはどれか**（#319）。一覧は `latest_version` を
            # 並べるので、承認待ちや却下済みの**新しい版**があると、その状態を
            # 課題そのものの状態として出していた ── 改訂を却下しただけで
            # 「却下済み — 出題されません」と出るが、**1 つ前の承認済みは出て
            # いる**。画面が学習者の見え方と食い違う。
            published = {
                task.id: uow.tasks.latest_published_version(task.id) for task, _ in group.tasks
            }
            # 配点（`effective_max_score`・2026-09-25）。**学習者に出ている版の値**を出す。
            versions = uow.tasks.versions_for_tasks([task.id for task, _ in group.tasks])
            max_scores = max_scores_by_version(versions)

        rows = []
        for task, version in group.tasks:
            rows.append(
                {
                    "task": task,
                    "version": version,
                    "kc_keys": kc_keys.get(task.id, ()),
                    "duplicate_title": titles.get(task.title, 0) > 1,
                    # 取り下げた課題。**消えてはいない**ので一覧には残す（#51）。
                    "withdrawn": task.withdrawn,
                    "test_cases": len(version.test_cases),
                    # 自動採点できるか。できない課題は AI 観点だけで、
                    # 教員の確定が前提になる（ADR 0008）。
                    "auto_graded": bool(version.test_cases),
                    # **採点のされ方**（2026-09-25）。以前は「自動テストなし」とだけ出し、
                    # AI が判定する課題（レポート・感想）まで自動採点されないように読めた。
                    "graded_by": _graded_by(version),
                    "evaluators": sorted(
                        {c.evaluator_id for c in version.criteria if c.evaluator_id}
                    ),
                    "unfinalized": pending.get(task.id, 0),
                    "stalled": stalled.get(task.id, 0),
                    # **承認済みと同じ見た目で並べない。** 一覧は
                    # `latest_version` をレビュー状態で絞らないので、生成した
                    # ままの課題もここに出る。印が無いと、教員は「この回は
                    # 5 問」と読むのに出題されるのは承認済みのぶんだけになる。
                    # 新しい版の状態。**課題そのものの状態ではない**（#319）。
                    "in_review": (version.provenance.review_state is ReviewState.IN_REVIEW),
                    "rejected": version.provenance.review_state is ReviewState.REJECTED,
                    # 学習者に出ている版（無ければ None）。ラベルはこれで決める
                    # ── 出ているかどうかは、承認済みの版があるかどうかである。
                    "published": published.get(task.id),
                    "points": max_scores.get(
                        (published.get(task.id) or version).id, version.max_score
                    ),
                    # 配点が入っているか。入っていなければ「—」（学習者にも割合だけが出る）。
                    "pointed": any(v.points_declared for v in versions if v.task_id == task.id),
                    # 訂正フォームの初期値。読みやすさの観点の重みは
                    # 版の中にあるので、そこから取り出す。
                    "readability_weight": next(
                        (c.weight for c in version.criteria if c.code == "readability"), 0.0
                    ),
                    # 訂正フォームで直せるように、いまの観点を行にして渡す。
                    "rubric_rows": rubric.to_rows(version.criteria),
                }
            )

        # **TA には読むだけの画面を出す**（#102）。編集用のテンプレートを
        # `{% if %}` で隠して回さない ── 隠し忘れが 1 か所あれば、そこから
        # 押せてしまう。出す形が違うなら、テンプレートも分ける。
        if not _can_edit(role):
            return templates.TemplateResponse(
                request,
                "unit_readonly.html",
                {
                    "me": me,
                    "course": course,
                    "section": {
                        "label": group.label,
                        "href": f"/manage/courses/{course.id}/units/{group.key}",
                    },
                    "unit": group,
                    "tasks": rows,
                    "course_grace": course.auto_finalize_after_minutes,
                },
            )

        return templates.TemplateResponse(
            request,
            "manage_unit.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": group.label,
                    "href": f"/manage/courses/{course.id}/units/{group.key}",
                },
                "unit": group,
                "tasks": rows,
                # 遅延の減点（この回の上書き）。**空の行を足して出す**（段を足せるように）。
                "unit_penalty_first": split_for_form(group.late_penalty_steps or ())[0],
                "unit_penalty_rows": split_for_form(group.late_penalty_steps or ())[1]
                + [{"hours": "", "percent": ""} for _ in range(PENALTY_BLANK_ROWS)],
                "course_penalty_text": summarize_penalty(course.late_penalty_steps),
                # いま実際に効いている減点（この回の設定、無ければコースの設定）。
                "effective_penalty_text": summarize_penalty(
                    course.late_penalty_steps
                    if group.late_penalty_steps is None
                    else group.late_penalty_steps
                ),
                # 「丸ごと片付ける」を押す前に出す内訳（#59）。**押してから
                # でないと分からないのでは確認にならない** ── 1 回の操作で
                # 課題ごとに削除か取り下げかが変わる。
                "clear_plan": _clear_plan(console, course, group),
                # 学内限定の表示に要る（#333）。**範囲が未設定なら効いて
                # いない**ので、そう書く ── 切り替えただけで守られていると
                # 読まれるのが、いちばん高くつく誤解である。
                "campus_configured": _campus_configured(console, me),
                # 選べる範囲（テナント管理者が注釈つきで登録したもの）と、いまの接続元。
                # **注釈を読んで選ぶ**ための値で、教員が自分の端末から開けば、いま
                # 居る教室がどの範囲かもその場で分かる。
                "campus_choices": _campus_choices(console, me, request, group),
                # 出題先の名簿（追試など）。**名簿そのものは別の画面で作る**
                # （`/manage/courses/{id}/groups`）── ここは選ぶだけ。
                "groups": _groups_of(console, course),
                # エディタで解けない理由（ADR 0026）。**押す前に見せる** ──
                # 押してから断られるのでは、どの課題が原因か分からない。
                "editor_blockers": editor_blockers(group.tasks, course),
                # ファイル選択を止められない理由（動画を受ける課題）。**押す前に見せる。**
                "file_upload_required": file_upload_required(group.tasks, course),
                # 問題セットの満点（学習者に出ている課題の配点の和）。クリア点の目安に出す。
                "points_full": sum(
                    row["points"]
                    for row in rows
                    if not row["withdrawn"] and (row["published"] is not None)
                ),
                # 試験の一括採点（#67）。待機中の件数と、落ちたジョブ。
                **_exam_state(console, course, group, now, user_id=me.user_id),
                "min_reason": MIN_JUSTIFICATION_LENGTH,
                # コースの既定。問題セットで指定しなければこれが効く。
                "course_grace": course.auto_finalize_after_minutes,
                "saved": SAVED_MESSAGES.get(saved),
                "saved_key": saved,
                "suffix_groups": SUFFIX_GROUPS,
                "course_suffixes": course.upload_suffixes or DEFAULT_UPLOAD_SUFFIXES,
                # 共通ルーブリック。未設定なら組み込みの既定を出して、
                # **いま何が使われているか**を見えるようにする。
                "rubric_rows": rubric.to_rows(
                    rubric.from_stored(course.rubric)
                    if course.rubric
                    else _default_rubric_criteria()
                ),
                "rubric_is_default": not course.rubric,
                # **生成は登録済み KC からの選択だけ**（`aijudge_course_admin.kc` の
                # 規則 4）。引退したものは選ばせない。
                # **このコースが使う範囲だけ出す。** 同じ名前空間を複数の
                # コースが使うほど関係のない候補が増え、C の科目に
                # `cs.python.*` が並ぶ。見にくいだけでなく、誤った知識要素を
                # 課題に付けられるということでもある（設計原則 P6）。
                "kcs": _course_kcs(console, course),
                "difficulties": [d.value for d in Difficulty],
                # ルーブリックの編集で、観点に指名できる評価器を出す。
                "deterministic": _evaluator_rows(
                    EvaluatorRegistry().load_installed(), EvaluatorKind.DETERMINISTIC
                ),
                "PROVISIONAL": GradeWindow.PROVISIONAL,
                "last_task": console.notices.take(me.user_id, course.id, notices.TASK_SAVED),
                "last_finalize": console.notices.take(me.user_id, course.id, notices.FINALIZED),
            },
        )

    @router.post("/courses/{course_id}/units/{unit}/retry-failed")
    def retry_failed(request: Request, course_id: str, unit: str) -> Response:
        """上限まで落ちたジョブを流し直す（#80）。

        **一覧を出しておいて手が無いのは中途半端である。** #67 で失敗した
        ジョブを問題セットのページに出したが、直したあとに戻す手立てが
        無かったので、SQL を叩くしかなかった。

        **教員が押したときだけ動く。** 自動で戻すなら上限を設けた意味が無い
        ── 直っていない原因で回し続けることになる。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        group = _unit_group(console, course, unit)

        version_ids = {str(version.id) for _task, version in group.tasks}
        now = datetime.now(UTC)
        with console.database.unit_of_work() as uow:
            # **上限の内側だけを再試行しない**（#230）。
            submissions = uow.submissions.list_for_versions(sorted(version_ids))
            failed = uow.jobs.failed_for([s.id for s in submissions])
            for job in failed:
                uow.jobs.update(job.retried(now))
            uow.commit()

        console.notices.put(me.user_id, course.id, notices.JOBS_RELEASED, len(failed))
        return RedirectResponse(
            f"/manage/courses/{course_id}/units/{group.key}?saved=retried#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/units/{unit}/grade-now")
    def grade_now(request: Request, course_id: str, unit: str) -> Response:
        """いま溜まっている提出を採点する（#67）。**何度でも押せる。**

        試験モードを解除しない ── 押したあとに出された提出はまた採点開始時刻
        まで待つ。試験中に「ここまでの提出が採点を通るか」を確かめられ、
        延長しても勝手に始まらない、という両方が要るため。

        やっているのは**寝かせてあるジョブの時刻を早めることだけ**で、採点
        そのものはワーカーが走らせる（提出時と同じ経路）。ここで採点を
        起動すると、レビュー画面が採点器を呼ぶ構造に戻る（ADR 0007）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        group = _unit_group(console, course, unit)

        version_ids = {str(version.id) for _task, version in group.tasks}
        now = datetime.now(UTC)
        with console.database.unit_of_work() as uow:
            # **一括採点の対象を打ち切られた一覧から決めない**（#230）。
            # 古い順に切るので、落ちるのは試験直後の提出だった ── しかも
            # 押す前の件数も同じ一覧から出ていたので、画面の中では
            # 矛盾せず、取りこぼしはどこにも現れなかった。
            submissions = uow.submissions.list_for_versions(sorted(version_ids))
            released = uow.jobs.release_waiting([s.id for s in submissions], now)
            uow.commit()

        console.notices.put(me.user_id, course.id, notices.JOBS_RELEASED, released)
        return RedirectResponse(
            f"/manage/courses/{course_id}/units/{group.key}?saved=released#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/units/{unit}/clear")
    def clear_unit_route(request: Request, course_id: str, unit: str) -> Response:
        """問題セットを丸ごと片付ける。**課題ごとに削除か取り下げか**（#59）。

        規則は `aijudge_course_admin.tasks` に置いてある ── 画面と CLI の両方から
        使うので、どちらが正しいかを問わずに済むよう 1 か所にする。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        group = _unit_group(console, course, unit)

        try:
            report = clear_unit(console.database, course_id=course.id, unit=group.unit)
        except AdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # **何がどうなったかを持ち帰る。** 件数だけでは、消えたのか残ったのか
        # 教員に分からない。
        console.notices.put(me.user_id, course.id, notices.UNIT_CLEARED, report)
        if not report.withdrawn and not report.untouched:
            # 全部消えたのでセットのページはもう無い。**コースのトップへ戻す**
            # （#82）── 以前は `/manage/courses/{id}`、つまり共通ルーブリックや
            # 遅延減点を設定する画面に飛ばしていた。消した直後に見たいのは
            # 「このコースに何が残っているか」であって、設定ではない。
            return RedirectResponse(f"/courses/{course_id}", status_code=303)
        return RedirectResponse(
            f"/manage/courses/{course_id}/units/{group.key}?saved=unit_cleared#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/units/{unit}/campus-only")
    def set_unit_campus_only(
        request: Request,
        course_id: str,
        unit: str,
        campus_only: Annotated[str, Form()] = "",
        campus_ranges: Annotated[list[str] | None, Form()] = None,
    ) -> Response:
        """**問題セットを学内からだけ受け付けるかを切り替える**（#333）。

        日程と同じで、値はセット単位で決めてその中の全課題に入れる
        （`_update_unit`）── 課題ごとに違うと、同じ回の中で「出せる課題と
        出せない課題」が混ざり、学習者にはその理由が読めない。

        **何を学内と見なすかはここでは決めない。** 範囲はテナント管理者が
        設定する（`/manage/campus-networks`）── 機関の属性であって、
        コースごとに違うものではない。
        """
        from ..app import require_principal

        me = require_principal(request)
        on = bool(campus_only.strip())
        ranges = _chosen_ranges(_console(request), me, campus_ranges or []) if on else ()
        return _update_unit(
            request,
            course_id,
            unit,
            update={"campus_only": on, "campus_ranges": ranges},
            saved="campus_only",
        )

    @router.post("/courses/{course_id}/units/{unit}/screen-capture")
    def set_unit_screen_capture(
        request: Request,
        course_id: str,
        unit: str,
        screen_capture: Annotated[str, Form()] = "",
    ) -> Response:
        """**試験中に画面全体の静止画を撮るかを切り替える**（ADR 0027・#444）。

        学内限定と同じく、値はセット単位で決めてその中の全課題に入れる。
        """
        return _update_unit(
            request,
            course_id,
            unit,
            update={"screen_capture": bool(screen_capture.strip())},
            saved="screen_capture",
        )

    @router.post("/courses/{course_id}/units/{unit}/answer-mode")
    def set_unit_answer_mode(
        request: Request,
        course_id: str,
        unit: str,
        file_upload: Annotated[str, Form()] = "",
        editor: Annotated[str, Form()] = "",
    ) -> Response:
        """**問題セットの答え方を決める**（ADR 0026）── ファイルとエディタの 2 つのチェック。

        2 つは独立に入れられる（2026-09-24）。両方なら学習者はどちらでも出せ、
        エディタだけなら**ファイルの提出を断る**（試験。作業の記録を経ない提出を
        止める）。両方外すと誰も提出できないので断る。

        学内限定と同じで、値はセット単位で決めて全課題に入れる（`_update_unit`）。
        同じ回の中で答え方が混ざると、学習者は課題ごとに画面を行き来する。

        **エディタを入れられるのは提出形式に `.c`・`.py`・`.md` のどれかを含む課題だけで、
        ここで確かめる**（`aijudge_course_admin.answer_mode`）。画面は理由を先に見せて押せなくするが、
        それは表示の都合であって境界ではない（#146）。
        """
        from ..app import require_principal

        by_file = bool(file_upload.strip())
        by_editor = bool(editor.strip())
        if by_editor or not by_file:
            me = require_principal(request)
            course = _require_instructor(request, me, CourseId(course_id))
            group = _unit_group(_console(request), course, unit)
        else:
            course = group = None
        return _update_unit(
            request,
            course_id,
            unit,
            update=_answer_mode_update(course, group, by_file=by_file, by_editor=by_editor),
            saved="answer_mode",
        )

    @router.post("/courses/{course_id}/units/{unit}/clear-points")
    def set_unit_clear_points(
        request: Request,
        course_id: str,
        unit: str,
        clear_points: Annotated[str, Form()] = "",
    ) -> Response:
        """**問題セットのクリア点を決める**（2026-09-25）。空欄はクリアの条件なし。

        学内限定と同じく、値はセット単位で決めて全課題に入れる（`_update_unit`）。
        学習者のコース一覧で、合計点がこの値以上の問題セットに「クリア」が付く。
        採点は変えない。
        """
        return _update_unit(
            request,
            course_id,
            unit,
            update={"clear_points": _parse_clear_points(clear_points)},
            saved="clear_points",
        )

    @router.post("/courses/{course_id}/units/{unit}/completion")
    def set_unit_completion(
        request: Request,
        course_id: str,
        unit: str,
        completion: Annotated[str, Form()] = "",
    ) -> Response:
        """**エディタで補完を出すかを切り替える**（設計書 §5.3）。

        答え方とは独立した値で、学内限定と同じくセット単位で全課題に入れる。
        エディタで解かない問題セットでは何も起きない（保存はしておく ── 後で
        エディタに切り替えたときに、選んだ設定がそのまま効く）。
        """
        return _update_unit(
            request,
            course_id,
            unit,
            update={"editor_completion": bool(completion.strip())},
            saved="completion",
        )

    @router.post("/courses/{course_id}/units/{unit}/confidential")
    def set_unit_confidential(
        request: Request,
        course_id: str,
        unit: str,
        confidential: Annotated[str, Form()] = "",
    ) -> Response:
        """**問題セットを公開まで教員だけに見せるかを切り替える**（試験）。

        公開前の問題セットは TA にも見えている（#340・#102）── 課題なら
        TA が先に読んで備えられるので正しいが、試験では TA が内容を先に
        知ること自体が漏洩の経路になる。**公開後は TA にも見える**
        （`aijudge_core.access`）。

        学内限定と同じく、値はセット単位で決めて全課題に入れる（`_update_unit`）。
        監査ログに残る ── 誰に何が見えるかを変える操作である。
        """
        return _update_unit(
            request,
            course_id,
            unit,
            update={"confidential_until_open": bool(confidential.strip())},
            saved="confidential",
        )

    @router.post("/courses/{course_id}/units/{unit}/audience")
    def set_unit_audience(
        request: Request,
        course_id: str,
        unit: str,
        groups: Annotated[list[str] | None, Form()] = None,
    ) -> Response:
        """**問題セットの出題先を置き換える**（追試など）。何も選ばなければ受講者全員。

        API（`PUT /api/courses/{id}/units/{unit}/audience`）と**同じ関数**を通す
        （`aijudge_course_admin.groups.set_audience`）── 名簿の検証と監査の記録を経路
        ごとに書かない。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        key = _normalized_unit(unit)
        with console.database.unit_of_work() as uow:
            tasks = [t for t in uow.tasks.list_for_course(course.id) if unit_key(t) == key]
            try:
                audience.set_audience(
                    uow,
                    recorder_for(uow, request, me),
                    course=course,
                    tasks=tasks,
                    names=groups or [],
                    unit_label=unquote(key),
                )
            except audience.GroupError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/units/{key}?saved=audience#saved", status_code=303
        )

    @router.post("/courses/{course_id}/units/{unit}/schedule")
    def set_unit_schedule(
        request: Request,
        course_id: str,
        unit: str,
        opens_at: Annotated[str, Form()] = "",
        submissions_open_at: Annotated[str, Form()] = "",
        due_at: Annotated[str, Form()] = "",
        grading_starts_at: Annotated[str, Form()] = "",
        accepts_until: Annotated[str, Form()] = "",
    ) -> Response:
        """**問題セットの日程。その中の全課題に同じ値を入れる。**

        課題ごとに違う締切を持てると、同じセットの中で締切がずれ、学習者にも
        教員にも「この回はいつまでか」が言えなくなる。日程はセットの性質で
        あって課題の性質ではない。

        締切の判定は `Submission.submitted_at`（提出確定の時刻）で行う。
        ここで入れる値がその基準になる。
        """
        return _update_unit(
            request,
            course_id,
            unit,
            update={
                "opens_at": _parse_when(opens_at),
                "submissions_open_at": _parse_when(submissions_open_at),
                "due_at": _parse_when(due_at),
                # 試験の問題セット（#67）。空なら提出と同時に採点する。
                "grading_starts_at": _parse_when(grading_starts_at),
                # 受付終了（#73）。空なら締切後も無期限に受け付ける。
                "accepts_until": _parse_when(accepts_until),
            },
            saved="schedule",
        )

    @router.post("/courses/{course_id}/units/{unit}/auto-finalize")
    def set_unit_grace(
        request: Request,
        course_id: str,
        unit: str,
        after_minutes: Annotated[str, Form()] = "",
    ) -> Response:
        """この問題セットの自動確定までの猶予（分）。空ならコースの既定に戻す。

        **日程とは別のフォームにする。** 締切を直しに来たときに猶予まで
        書き換えてしまう（あるいはその逆）事故を、フォームの単位で防ぐ。
        """
        return _update_unit(
            request,
            course_id,
            unit,
            update={"auto_finalize_after_minutes": _parse_minutes(after_minutes)},
            saved="grace",
        )

    @router.post("/courses/{course_id}/units/{unit}/number")
    def set_unit_number(
        request: Request,
        course_id: str,
        unit: str,
        session: Annotated[str, Form()] = "",
    ) -> Response:
        """回番号。**並べ替えと表示のためだけの値**で、採点には効かない。

        日程と混ぜない ── 効き方が違うものを 1 つの保存ボタンにまとめると、
        何が変わったのか教員に分からない。
        """
        number = int(session) if session.strip() else None
        return _update_unit(request, course_id, unit, update={"session": number}, saved="number")

    @router.post("/courses/{course_id}/units/{unit}/settings")
    async def save_unit_settings(request: Request, course_id: str, unit: str) -> Response:
        """問題セットの設定を**まとめて**保存する（2026-09-26）。

        以前は節ごとに保存ボタンがあり（回番号・日程・場所・答え方・クリア条件・
        補完・公開前・出題先・自動確定の 9 つ）、1 つを押すと頁が読み直されて、
        他の節で書きかけていた値が黙って消えた。

        **書くのは画面で変えた項目だけ。** 送られてきた値をセットの代表値
        （`UnitGroup`）と比べ、違う項目だけを全課題に入れる ── 全部を書くと、
        課題ごとにばらついている値（取り込みの結果）を、触ってもいない節の保存で
        揃えてしまう。

        **どれか 1 つでも断られたら何も書かない。** 出題先も同じ作業単位で書く。
        検査は個別の経路と同じ関数を通す（`_answer_mode_update` など）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        key = _normalized_unit(unit)
        group = _unit_group(console, course, unit)
        form = await request.form()

        def text(name: str) -> str:
            value = form.get(name)
            return value if isinstance(value, str) else ""

        def flag(name: str) -> bool:
            return bool(text(name).strip())

        raw_session = text("session").strip()
        try:
            session = int(raw_session) if raw_session else None
        except ValueError:
            raise HTTPException(status_code=400, detail="回番号は整数で入れてください") from None

        update: dict[str, object] = {}
        if session != group.session:
            update["session"] = session
        for name in (
            "opens_at",
            "submissions_open_at",
            "due_at",
            "accepts_until",
            "grading_starts_at",
        ):
            when = _parse_when(text(name))
            if not _same_minute(when, getattr(group, name)):
                update[name] = when
        wants_campus = flag("campus_only")
        chosen_ranges = (
            _chosen_ranges(console, me, form.getlist("campus_ranges")) if (wants_campus) else ()
        )
        if wants_campus != group.campus_only or tuple(sorted(chosen_ranges)) != group.campus_ranges:
            update["campus_only"] = wants_campus
            update["campus_ranges"] = chosen_ranges
        for field, name, current in (
            ("editor_completion", "completion", group.completion),
            ("confidential_until_open", "confidential", group.confidential),
            ("screen_capture", "screen_capture", group.screen_capture),
        ):
            if flag(name) != current:
                update[field] = flag(name)
        clear_points = _parse_clear_points(text("clear_points"))
        if clear_points != group.clear_points:
            update["clear_points"] = clear_points
        grace = _parse_minutes(text("after_minutes"))
        if grace != (None if group.grace_from_course else group.grace):
            update["auto_finalize_after_minutes"] = grace
        # **欄が送られてきたときだけ読む。** 欄の無い古い画面から保存されると、空の入力が
        # 「この回は減点しない」に化ける。印（`penalty_present`）で見分ける。
        if flag("penalty_present"):
            wanted = _penalty_from_form(form, text("penalty_mode"), text("penalty_first_percent"))
            if wanted != group.late_penalty_steps:
                update["late_penalty_steps"] = wanted
        by_file, by_editor = flag("file_upload"), flag("editor")
        if (by_file, by_editor) != (group.file_upload, group.editor):
            update |= _answer_mode_update(course, group, by_file=by_file, by_editor=by_editor)

        # 出題先。**名簿があるときだけ欄が出る**ので、欄が送られてきたときだけ見る
        # （チェックが 0 個だと `groups` そのものが送られず、全員に戻すのと
        # 区別が付かない）。
        names: list[str] | None = None
        if flag("audience_shown"):
            chosen = sorted(v for v in form.getlist("groups") if isinstance(v, str))
            known = {str(row.group.id): row.group.name for row in _groups_of(console, course)}
            current_names = sorted(known[g] for g in group.audience if g in known)
            if chosen != current_names:
                names = chosen

        if not update and names is None:
            return RedirectResponse(
                f"/manage/courses/{course_id}/units/{key}?saved=unchanged#saved",
                status_code=303,
            )
        with console.database.unit_of_work() as uow:
            if update:
                _apply_unit_update(
                    uow, request, me, course_id, key, update=update, saved="settings"
                )
            if names is not None:
                tasks = [t for t in uow.tasks.list_for_course(course.id) if unit_key(t) == key]
                try:
                    audience.set_audience(
                        uow,
                        recorder_for(uow, request, me),
                        course=course,
                        tasks=tasks,
                        names=names,
                        unit_label=unquote(key),
                    )
                except audience.GroupError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/units/{key}?saved=settings#saved", status_code=303
        )

    @router.post("/courses/{course_id}/units/{unit}/finalize")
    def finalize_unit(
        request: Request,
        course_id: str,
        unit: str,
        justification: Annotated[str, Form()] = "",
    ) -> Response:
        """問題セットの未確定分を、その中の全課題についてまとめて確定する。

        **根拠説明を必須にする。** 学習者にそのまま表示される。個別に読んで
        いない成績を確定させる操作なので、何を根拠にそうしたのかが残らないと
        学習者は何も分からない（設計原則 P4 を一括操作にも適用する）。

        未対応の異議申立は確定しない。そこは 1 件ずつ読むべきものとして
        「再確認の依頼」に残す。
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

        key = _normalized_unit(unit)
        with console.database.unit_of_work() as uow:
            task_ids = [
                task.id
                for task in uow.tasks.list_for_course(CourseId(course_id))
                if unit_key(task) == key
            ]
        if not task_ids:
            raise HTTPException(status_code=404, detail="この問題セットには課題がありません")

        try:
            outcomes = finalize_tasks(
                console.database,
                task_ids=task_ids,
                actor_id=me.user_id,
                justification=text,
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # 表示のために保持する。**コースを添える**（Console は全利用者で共有で、
        # 添えないと別コースの教員に他コースの課題名が出る）。
        console.notices.put(me.user_id, course_id, notices.FINALIZED, _merged(outcomes))
        return RedirectResponse(f"/courses/{course_id}/finalize", status_code=303)
