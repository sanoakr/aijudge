"""ブラウザ IDE の受け口（`docs/design/online-coding-test.md` §5・§8・§9）。

学生画面のうち、`answer_mode=editor` の課題にだけ効く部分をここに置く
（不変条件 I5 ── `upload` の課題には何も起きない）。

**タブは課題 1 つに対応し、選べる形式はその課題の提出形式に限る**
（`aijudge_ide.formats`）。`.c`・`.py` はプログラム、`.md` はテキストとして
書いて提出する。**実行できるのは、選んだ形式が採点の言語と一致するときだけ**。

**この web はコードを動かさない。** 実行の要求は `run_requests` に行を書く
だけで、動かすのは別プロセスの runner である（ADR 0024、`web-does-not-run-code`
契約）。提出は既存の `SubmissionService.accept()` を通し、採点側は IDE から
来たことを知らない（I2）。

**関門は `/submit` と同じ関数を通す**（I8）。実行・提出・自動保存の 3 つとも、
学内限定・受付期間・役割・`may_see` を `_submission_gate` で判定する ──
受付の外では実行もさせない（いつでも任意のコードを走らせられる窓口にしない）。
"""

# **`from __future__ import annotations` を使わない。** 経路の引数の型
# （`me: Me`）は `register_ide_routes` の中で作る別名で、注釈を文字列のまま
# 遅らせると FastAPI がモジュールの名前空間から引こうとして見つけられない。
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from aijudge_authoring import render_statement
from aijudge_core import AnswerMode, ArtifactKind, Course, Task, TaskVersion, allowed_suffixes
from aijudge_core.ids import CourseId
from aijudge_grading import OverrideError, SubjectProfile, effective_profile, load_profile
from aijudge_ide import (
    MAX_SOURCE_BYTES,
    MAX_STDIN_BYTES,
    BufferTooLarge,
    EditorFormat,
    RefusalReason,
    RunRefused,
    RunRequestId,
    content_hash,
    editor_formats,
    make_buffer,
    request_run,
    view_run,
)
from aijudge_submission import IncomingFile, SubmissionRejected
from aijudge_toolchain import UnknownLanguage, resolve_language

# テストを走らせる評価器（`aijudge_admin.answer_mode` と同じく名前で指す）。
# 評価器のパッケージを import すると sandbox まで引きずり、
# `web-does-not-run-code` 契約が落ちる。
CODE_TEST_RUNNER = "code_test_runner"

# 画面が結果を問い合わせる間隔（ミリ秒、設計書 §8.1）。実行を待っている人
# だけが問い合わせるので、150 名 × 2 回/秒にはならない（ADR 0024 §5）。
POLL_INTERVAL_MS = 500
# 自動保存の間隔（ミリ秒、設計書 §6.5）。変更があるときだけ送る。
AUTOSAVE_INTERVAL_MS = 10_000

TOO_LARGE = f"内容が大きすぎます（{MAX_SOURCE_BYTES // 1024} KiB まで）"


class RunBody(BaseModel):
    suffix: str = Field(max_length=8)
    source: str = Field(max_length=MAX_SOURCE_BYTES)
    stdin: str = Field(default="", max_length=MAX_STDIN_BYTES)
    sample_name: str | None = Field(default=None, max_length=200)
    ide_session_id: str | None = Field(default=None, max_length=64)


class SourceBody(BaseModel):
    suffix: str = Field(max_length=8)
    source: str = Field(max_length=MAX_SOURCE_BYTES)
    ide_session_id: str | None = Field(default=None, max_length=64)


@dataclass
class IdeDeps:
    """`create_app` の中の関数のうち、IDE が使うもの。

    関門（`gate`）は `create_app` の中で閉じている ── ここで写さず、同じ
    関数を受け取る（I8）。
    """

    state: Any
    templates: Jinja2Templates
    gate: Callable[..., tuple[TaskVersion, Course, Task, Any]]
    course_and_tasks: Callable[..., tuple[Course, tuple]]
    load_progress: Callable[..., dict]
    build_context: Callable[..., dict]
    is_demo: Callable[[object], bool]
    now: Callable[[], datetime]
    profiles: dict[str, SubjectProfile] = field(default_factory=dict)

    def formats_for(self, task: Task, course: Course) -> tuple[EditorFormat, ...]:
        """この課題でエディタが選べる形式。**受付と同じ関数**で提出形式を決める。

        別の決め方をすると、画面が出した形式を提出で断ることになる。
        """
        return editor_formats(allowed_suffixes(task.accepted_suffixes, course.upload_suffixes))

    def runnable_format(
        self, task: Task, version: TaskVersion, course: Course
    ) -> EditorFormat | None:
        """試しに実行できる形式。無ければ None（テキストの課題・採点がテストを
        走らせない課題・採点の言語を課題が受け付けない場合）。

        採点の言語は**採点ワーカーと同じ重ね方**（雛形 + コースの上書き・
        ADR 0018）で決める。runner も同じ手順でもう一度確かめるので、ここが
        食い違っても違う言語で動くことはない。
        """
        name = version.subject_profile
        if name not in self.profiles:
            path = Path(self.state.profiles_dir) / f"{name}.yaml"
            if not path.is_file():
                return None
            try:
                self.profiles[name] = load_profile(path)
            except Exception:
                return None
        try:
            profile = effective_profile(self.profiles[name], course.grading_overrides)
        except OverrideError:
            return None
        if CODE_TEST_RUNNER not in profile.deterministic:
            return None
        try:
            language = resolve_language(dict(profile.evaluator_options.get(CODE_TEST_RUNNER, {})))
        except UnknownLanguage:
            return None
        return next(
            (f for f in self.formats_for(task, course) if f.filename == language.source_name),
            None,
        )


def _require_editor(task: Task) -> None:
    """エディタで解く課題か。**違えば無いものとして扱う**（I5）。"""
    if task.answer_mode is not AnswerMode.EDITOR:
        raise HTTPException(status_code=404, detail="この課題はエディタでは解けません")


def _chosen(deps: IdeDeps, task: Task, course: Course, suffix: str) -> EditorFormat:
    """送られてきた形式が、この課題で選べるものか。**画面の選択肢を信じない。**"""
    for candidate in deps.formats_for(task, course):
        if candidate.suffix == suffix.lower():
            return candidate
    raise HTTPException(status_code=400, detail="この課題では、その形式は使えません")


def _public_samples(version: TaskVersion) -> list[dict[str, str]]:
    """公開サンプル。**非公開のケースは出さない**（ADR 0024 §4）。"""
    return [
        {
            "name": case.name,
            "input": str(case.payload.get("input", "")),
            "expected": str(case.payload.get("expected", "")),
        }
        for case in version.test_cases
        if case.evaluator_id == CODE_TEST_RUNNER and not case.hidden
    ]


def _refusal_status(reason: RefusalReason) -> int:
    # 待てば通るものは 429、送り方が悪いものは 400。画面はこれで出し分ける。
    if reason in (RefusalReason.ALREADY_PENDING, RefusalReason.TOO_SOON):
        return 429
    return 400


def _last_submitted_hash(mark: Any) -> str | None:
    """最後の提出の中身の指紋。無ければ None。

    画面は「前回の提出から変えたか」をこれと自分の内容の指紋で比べる
    （設計書 §5.2 のタブの状態）。提出の Artifact の `content_hash` はバイト列の
    SHA-256 で、`aijudge_ide.content_hash` も UTF-8 のバイト列の SHA-256 である。
    """
    if mark is None or not mark.attempts:
        return None
    submission = getattr(mark.attempts[-1], "submission", None)
    if submission is None:
        return None
    for artifact in submission.artifacts:
        if artifact.kind in (ArtifactKind.CODE, ArtifactKind.MARKDOWN):
            return str(artifact.content_hash).removeprefix("sha256:")
    return None


def register_ide_routes(app: FastAPI, deps: IdeDeps, me_dependency: Any) -> None:
    """IDE の経路を足す。`me_dependency` は既存の `Me`（ログイン必須）。"""
    Me = me_dependency

    @app.get("/courses/{course_id}/ide", response_class=HTMLResponse)
    def ide_page(request: Request, course_id: str, me: Me, unit: str = "") -> HTMLResponse:
        """問題セットのうち、エディタで解く課題をタブにして開く（設計書 §5.1）。

        見える課題の絞り込みは既存のコースページと同じ関数（`may_see` を通す）。
        公開前の課題は学習者の一覧に無いので、ここにも出ない。
        """
        course_obj, rows = deps.course_and_tasks(deps.state, me, CourseId(course_id))
        wanted = unit or None
        picked = [
            (task, version)
            for task, version in sorted(rows, key=lambda row: row[0].sort_key)
            if task.unit == wanted
            and task.answer_mode is AnswerMode.EDITOR
            and deps.formats_for(task, course_obj)
        ]
        if not picked:
            raise HTTPException(status_code=404, detail="エディタで解く課題がありません")

        with deps.state.database.unit_of_work() as uow:
            progress = deps.load_progress(
                uow,
                tenant_id=me.tenant_id,
                learner_id=me.user_id,
                course=course_obj,
                rows=tuple(picked),
            )
            buffers = uow.ide_buffers.for_tasks(me.user_id, [task.id for task, _ in picked])

        moment = deps.now()
        tabs = []
        for task, version in picked:
            formats = deps.formats_for(task, course_obj)
            runnable = deps.runnable_format(task, version, course_obj)
            buffer = buffers.get(task.id)
            selected = (
                buffer.suffix
                if buffer is not None and any(f.suffix == buffer.suffix for f in formats)
                else formats[0].suffix
            )
            mark = progress.get(version.id)
            tabs.append(
                {
                    "task": task,
                    "version": version,
                    "statement_html": render_statement(version.statement),
                    "formats": [
                        {
                            "suffix": f.suffix,
                            "label": f.label,
                            "monaco": f.monaco_language,
                            "filename": f.filename,
                        }
                        for f in formats
                    ],
                    "selected": selected,
                    "runnable": None if runnable is None else runnable.suffix,
                    # サンプルは実行できるときだけ出す（押しても動かないボタンを出さない）。
                    "samples": _public_samples(version) if runnable is not None else [],
                    "source": buffer.source if buffer is not None else "",
                    "submitted": 0 if mark is None else mark.count,
                    "last_submitted_hash": _last_submitted_hash(mark),
                }
            )

        accepts = [task.accepts_until for task, _ in picked if task.accepts_until is not None]
        dues = [task.due_at for task, _ in picked if task.due_at is not None]
        accepts_until = max(accepts, default=None)
        first_task, first_version = picked[0]
        return deps.templates.TemplateResponse(
            request,
            "ide.html",
            {
                "me": me,
                "course": course_obj,
                "unit_label": first_task.unit_label,
                "tabs": tabs,
                "accepts_until": accepts_until,
                "due_at": max(dues, default=None),
                # **残り秒数はサーバが数える**（既存の締切表示と同じ・#73）。
                # 学習者の PC の時計がずれていても、表示はずれない。
                "remaining_seconds": (
                    None if accepts_until is None else int((accepts_until - moment).total_seconds())
                ),
                "poll_ms": POLL_INTERVAL_MS,
                "autosave_ms": AUTOSAVE_INTERVAL_MS,
                "max_source_bytes": MAX_SOURCE_BYTES,
                **deps.build_context(course_obj, first_task, first_version),
            },
        )

    @app.post("/ide/tasks/{task_version_id}/run")
    def ide_run(request: Request, task_version_id: str, body: RunBody, me: Me) -> JSONResponse:
        """試しの実行を積む（設計書 §8.1）。結果は `GET /ide/runs/{id}` で取る。"""
        version, course_obj, task_obj, _role = deps.gate(request, me, task_version_id)
        _require_editor(task_obj)
        chosen = _chosen(deps, task_obj, course_obj, body.suffix)
        runnable = deps.runnable_format(task_obj, version, course_obj)
        if runnable is None or runnable.suffix != chosen.suffix:
            return JSONResponse(
                {"detail": "この形式では実行できません（採点の言語と同じ形式だけ実行できます）"},
                status_code=409,
            )
        with deps.state.database.unit_of_work() as uow:
            try:
                run = request_run(
                    uow.run_requests,
                    tenant_id=me.tenant_id,
                    learner_id=me.user_id,
                    task_version_id=version.id,
                    source=body.source,
                    stdin=body.stdin,
                    sample_name=body.sample_name or None,
                    ide_session_id=body.ide_session_id,
                    suffix=chosen.suffix,
                    now=deps.now(),
                )
            except RunRefused as exc:
                headers = (
                    {"Retry-After": str(max(1, round(exc.retry_after)))}
                    if exc.retry_after is not None
                    else None
                )
                return JSONResponse(
                    {"detail": exc.message, "reason": exc.reason.value},
                    status_code=_refusal_status(exc.reason),
                    headers=headers,
                )
            uow.commit()
        return JSONResponse({"id": str(run.id), "state": run.state.value}, status_code=202)

    @app.get("/ide/runs/{run_id}")
    def ide_run_state(run_id: str, me: Me) -> JSONResponse:
        """実行の状態と結果。**本人の要求でなければ 404**（`view_run`）。"""
        with deps.state.database.unit_of_work() as uow:
            view = view_run(
                uow.run_requests, RunRequestId(run_id), learner_id=me.user_id, now=deps.now()
            )
            # 問い合わせのついでに古い要求を片付けているので、その分を残す。
            uow.commit()
        if view is None:
            raise HTTPException(status_code=404, detail="実行が見つかりません")
        run = view.request
        outcome = run.outcome
        return JSONResponse(
            {
                "id": str(run.id),
                "state": run.state.value,
                "ahead": view.ahead,
                "error": run.error,
                "outcome": None if outcome is None else outcome.model_dump(mode="json"),
            }
        )

    @app.post("/ide/tasks/{task_version_id}/submit")
    def ide_submit(
        request: Request, task_version_id: str, body: SourceBody, me: Me
    ) -> JSONResponse:
        """エディタの内容を提出する（設計書 §9）。**既存の `accept()` を通す**（I2）。

        ファイル名は形式の表の `filename`（`main.c`・`answer.md` など）で、
        プログラムは言語の表と同じ名前 ── `/submit` でファイルを出したときと
        同じ `Submission` になり、採点は区別できない。
        """
        version, course_obj, task_obj, role = deps.gate(request, me, task_version_id)
        _require_editor(task_obj)
        chosen = _chosen(deps, task_obj, course_obj, body.suffix)
        if not body.source.strip():
            raise HTTPException(status_code=400, detail="内容が空です")
        if len(body.source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise HTTPException(status_code=400, detail=TOO_LARGE)
        try:
            result = deps.state.submissions.accept(
                tenant_id=me.tenant_id,
                task_version_id=version.id,
                learner_id=me.user_id,
                subject_profile=version.subject_profile,
                files=[
                    IncomingFile(
                        filename=chosen.filename,
                        kind=chosen.kind,
                        payload=body.source.encode("utf-8"),
                    )
                ],
                # 試験の採点保留（#67）と役割の焼き付け（#108）は `/submit` と同じ。
                grading_starts_at=task_obj.grading_starts_at,
                submitted_as=role,
                is_demo=deps.is_demo(course_obj.id),
            )
        except SubmissionRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 提出した内容を自動保存にも入れる。**提出したのに、リロードすると前の
        # 書きかけに戻る**、を起こさない。
        with deps.state.database.unit_of_work() as uow:
            uow.ide_buffers.save(
                make_buffer(
                    tenant_id=me.tenant_id,
                    learner_id=me.user_id,
                    task_id=task_obj.id,
                    suffix=chosen.suffix,
                    source=body.source,
                    now=deps.now(),
                )
            )
            uow.commit()
        submission = result.submission
        return JSONResponse(
            {
                "submission_id": str(submission.id),
                "attempt": submission.attempt,
                "deduplicated": result.deduplicated,
                "content_hash": content_hash(body.source),
                "url": f"/submissions/{submission.id}",
            }
        )

    @app.put("/ide/tasks/{task_version_id}/buffer")
    def ide_save(request: Request, task_version_id: str, body: SourceBody, me: Me) -> JSONResponse:
        """自動保存（設計書 §6.5）。**受付を過ぎてから届いたものは採らない**（§9.1）。

        関門を通すので、受付の外では 409 になる。画面はそれを「保存できません」
        と出し、書いた内容はブラウザに残る。
        """
        _version, course_obj, task_obj, _role = deps.gate(request, me, task_version_id)
        _require_editor(task_obj)
        chosen = _chosen(deps, task_obj, course_obj, body.suffix)
        try:
            buffer = make_buffer(
                tenant_id=me.tenant_id,
                learner_id=me.user_id,
                task_id=task_obj.id,
                suffix=chosen.suffix,
                source=body.source,
                now=deps.now(),
            )
        except BufferTooLarge:
            raise HTTPException(status_code=400, detail=TOO_LARGE) from None
        with deps.state.database.unit_of_work() as uow:
            uow.ide_buffers.save(buffer)
            uow.commit()
        return JSONResponse(
            {"saved_at": buffer.updated_at.isoformat(), "content_hash": buffer.content_hash}
        )
