"""教員コンソール（レビューと管理）。

**この画面は採点しない。** 採点は提出時にワーカーが走らせ（`aijudge-worker`）、
ここは届いた結果を読んで教員が確定させる場所である。レビューが採点を
起動していた頃は、測定用データの入力が採点の前提条件になっていた。これは
手段と目的が逆で、測定を必須機能にしない方針に反する（ADR 0007）。

通常の経路（大多数の提出）:

    待ち行列 → [AI の判定を見て確定] → 成績

blind 抽出に当たった提出だけ、教員の段階を先に取る:

    待ち行列 → [blind 採点] → [AI の判定を開示] → [確定]
                     ↓                              ↓
              測定用の正解データ                  成績

順序を逆にすると教員の採点は AI に引きずられる（アンカリング）。そうして
集めたデータで κ を測れば、実力より高い一致度が出る。だが全件に課すと
レビュー 1 件ごとに 2 段階の入力を強制するので、抽出に留める（`sampling.py`）。

blind 画面のレスポンスには AI の判定を一切含めない。CSS で隠すのでは
不十分（ページのソースを見れば分かる）。これはテストで固定してある。

`/manage` に科目・課題・受講の管理を載せている（`manage.py`）。学期中に
発生する作業（締切の設定、受講者の追加、課題の追加）を教員がターミナルで
行うのは現実的でないため。**科目プロファイルは表示だけで編集させない。**
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from aijudge_authoring import render_statement
from aijudge_core import (
    BlindMark,
    Course,
    GradingRun,
    HumanReview,
    RubricCriterion,
    Submission,
    TaskVersion,
    new_id,
)
from aijudge_core.ids import CourseId, CriterionId, HumanReviewId, SubmissionId
from aijudge_grading import load_profile, project_observations
from aijudge_identity import (
    AuthenticationFailed,
    AuthService,
    PermissionDenied,
    Principal,
    session_cookie_kwargs,
)
from aijudge_persistence import Database, ObservationFileStore
from aijudge_submission import ArtifactStore, ImmutabilityViolation

from .sampling import is_blind_sample

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

SESSION_COOKIE = "aijudge_review_session"
DEFAULT_TENANT = "ten_" + "0" * 32


class Console:
    """コンソールの状態。

    **採点機能を持たない。** 採点はワーカーの仕事で、ここからは呼べない。
    呼べるようにしておくと、いつかどこかの経路でレビューが採点を起動し、
    測定用データの入力が採点の前提条件に戻る（ADR 0007）。
    """

    def __init__(
        self,
        database: Database,
        artifact_store: ArtifactStore,
        *,
        profiles_dir: Path,
        observations: ObservationFileStore | None = None,
    ) -> None:
        self.database = database
        self.store = artifact_store
        self.profiles_dir = profiles_dir
        self.observations = observations
        self._rates: dict[str, float] = {}
        # 直近の課題取り込み結果。管理画面が表示に使う。
        self.last_import = None

    def blind_sample_rate(self, subject_profile: str) -> float:
        """科目プロファイルが宣言した blind 抽出率。

        読めない場合は 0 とする。**測定のためにレビューを止めない。**
        抽出されないだけで運用は続く。
        """
        if subject_profile not in self._rates:
            try:
                rate = load_profile(
                    self.profiles_dir / f"{subject_profile}.yaml"
                ).measurement.blind_sample_rate
            except Exception:
                rate = 0.0
            self._rates[subject_profile] = rate
        return self._rates[subject_profile]

    def needs_blind_mark(self, submission: Submission, subject_profile: str) -> bool:
        return is_blind_sample(str(submission.id), self.blind_sample_rate(subject_profile))

    def source_of(self, submission: Submission) -> str:
        for artifact in submission.gradable_artifacts:
            try:
                return self.store.get(artifact.storage_key).decode("utf-8", "replace")
            except Exception:  # pragma: no cover - ストアが読めない状況
                return ""
        return ""

    def refresh_observations(
        self,
        submission: Submission,
        run: GradingRun,
        task_version: TaskVersion,
        *,
        subject_profile: str,
        mark: BlindMark | None,
        review: HumanReview | None,
    ) -> None:
        """観測を書き直す。**失敗してもレビューは成立させる。**

        観測は投影であって記録の正本ではない（正本は DB 側）。測定の都合で
        レビューを落とさない（ADR 0007）。
        """
        if self.observations is None:
            return
        try:
            codes = {criterion.id: criterion.code for criterion in task_version.criteria}
            human_levels = (
                None
                if mark is None
                else {
                    codes[criterion_id]: level
                    for criterion_id, level in mark.levels.items()
                    if criterion_id in codes
                }
            )
            self.observations.save(
                project_observations(
                    run,
                    task_version,
                    subject_profile=subject_profile,
                    task_name=str(task_version.task_id),
                    submission=str(submission.id),
                    observed_at=datetime.now(UTC),
                    human_levels=human_levels,
                    blind=mark is not None,
                    marker=None if mark is None else str(mark.grader_id),
                    # 「教員が機械の判定を直したか」。blind からの移動ではない。
                    machine_corrected=(None if review is None else not review.agreed),
                )
            )
        except Exception:
            return


def _state(request: Request) -> Console:
    return request.app.state.aijudge  # type: ignore[no-any-return]


def current_principal(request: Request) -> Principal | None:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    with _state(request).database.unit_of_work() as uow:
        return AuthService(uow.identity).resolve(token)


def require_principal(request: Request) -> Principal:
    principal = current_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="ログインしてください")
    return principal


Me = Annotated[Principal, Depends(require_principal)]


def create_app(console: Console, *, min_sample_size: int = 30) -> FastAPI:
    app = FastAPI(title="aiJudge instructor console")
    app.state.aijudge = console

    # 管理画面（科目・課題・受講）。採点の待ち行列とは別の関心事だが、
    # 教員に 2 つの Web アプリを使わせないため同じアプリに載せる。
    from .manage import register as register_manage

    app.include_router(register_manage(TEMPLATES))

    # -- ログイン ----------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    def login(
        request: Request,
        login: Annotated[str, Form()],
        password: Annotated[str, Form()],
    ) -> Response:
        from aijudge_core.ids import TenantId

        with console.database.unit_of_work() as uow:
            try:
                _, token = AuthService(uow.identity).login(
                    tenant_id=TenantId(DEFAULT_TENANT), login=login, password=password
                )
            except AuthenticationFailed as exc:
                return TEMPLATES.TemplateResponse(
                    request, "login.html", {"error": str(exc)}, status_code=401
                )
            uow.commit()
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            **session_cookie_kwargs(forwarded_proto=request.headers.get("x-forwarded-proto")),
        )
        return response

    @app.post("/logout")
    def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE, "")
        if token:
            with console.database.unit_of_work() as uow:
                AuthService(uow.identity).logout(token)
                uow.commit()
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # -- 待ち行列 ----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        principal = current_principal(request)
        if principal is None:
            return RedirectResponse("/login", status_code=303)
        with console.database.unit_of_work() as uow:
            auth = AuthService(uow.identity)
            courses = [
                course
                for course in auth.courses_for(principal.tenant_id, principal.user_id)
                if _can_grade(auth, course.id, principal)
            ]
        return TEMPLATES.TemplateResponse(
            request, "index.html", {"me": principal, "courses": courses}
        )

    @app.get("/courses/{course_id}", response_class=HTMLResponse)
    def queue(request: Request, course_id: str, me: Me) -> HTMLResponse:
        course, rows, marked_count = _queue_rows(console, me, CourseId(course_id))
        return TEMPLATES.TemplateResponse(
            request,
            "queue.html",
            {
                "me": me,
                "course": course,
                "rows": rows,
                "marked_count": marked_count,
                "min_sample_size": min_sample_size,
            },
        )

    # -- blind 採点（抽出対象のみ）----------------------------------------

    @app.get("/review/{submission_id}/blind", response_class=HTMLResponse)
    def blind(request: Request, submission_id: str, me: Me) -> Response:
        context = _load(console, me, SubmissionId(submission_id))
        if not context.needs_blind:
            return RedirectResponse(f"/review/{submission_id}/reveal", status_code=303)
        return TEMPLATES.TemplateResponse(
            request,
            "blind.html",
            {
                "me": me,
                "submission": context.submission,
                "task": context.task_version,
                "lines": _numbered(console.source_of(context.submission)),
                "criteria": context.task_version.criteria,
                "statement_html": render_statement(context.task_version.statement),
            },
        )

    @app.post("/review/{submission_id}/blind")
    async def submit_blind(request: Request, submission_id: str, me: Me) -> Response:
        """blind 採点を保存する。**採点は起動しない。**

        フォーム全体を読む。段階の項目名が観点ごとに違うため
        （`level_<code>`）、宣言された引数では受け取れない。
        """
        form = await request.form()
        context = _load(console, me, SubmissionId(submission_id))
        parsed = _parse_levels(context.task_version.criteria, form)
        notes = str(form.get("notes", ""))

        with console.database.unit_of_work() as uow:
            try:
                uow.reviews.save_blind_mark(
                    BlindMark(
                        submission_id=context.submission.id,
                        grader_id=me.user_id,
                        levels=parsed,
                        marked_at=datetime.now(UTC),
                        notes=notes.strip() or None,
                    )
                )
            except ImmutabilityViolation as exc:
                # 二度目を受け付けると、AI を見たあとの段階で上書きできる。
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            uow.commit()

        fresh = _load(console, me, SubmissionId(submission_id))
        console.refresh_observations(
            fresh.submission,
            fresh.run,
            fresh.task_version,
            subject_profile=fresh.course.subject_profile,
            mark=fresh.mark,
            review=fresh.review,
        )
        return RedirectResponse(f"/review/{submission_id}/reveal", status_code=303)

    # -- 開示と確定 --------------------------------------------------------

    @app.get("/review/{submission_id}/reveal", response_class=HTMLResponse)
    def reveal(request: Request, submission_id: str, me: Me) -> Response:
        context = _load(console, me, SubmissionId(submission_id))
        if context.needs_blind:
            return RedirectResponse(f"/review/{submission_id}/blind", status_code=303)

        source = console.source_of(context.submission)
        return TEMPLATES.TemplateResponse(
            request,
            "reveal.html",
            {
                "me": me,
                "submission": context.submission,
                "course": context.course,
                "task": context.task_version,
                "run": context.run,
                "lines": _numbered(source),
                "rows": _comparison_rows(context.task_version, context.run, context.mark),
                "highlights": _highlighted_lines(context.run),
                "review": context.review,
                "was_blind": context.mark is not None,
            },
        )

    @app.post("/review/{submission_id}/finalize")
    async def finalize(request: Request, submission_id: str, me: Me) -> Response:
        form = await request.form()
        context = _load(console, me, SubmissionId(submission_id))
        final = _parse_levels(context.task_version.criteria, form)
        comment = str(form.get("comment", ""))

        # 変更した観点だけを持つ。触っていない観点は AI に同意した意味。
        machine = {score.criterion_id: score.level for score in context.run.criterion_scores}
        adjusted = {
            criterion_id: level
            for criterion_id, level in final.items()
            if machine.get(criterion_id) != level
        }

        with console.database.unit_of_work() as uow:
            try:
                uow.reviews.save_review(
                    HumanReview(
                        id=HumanReviewId(new_id("hrv")),
                        grading_run_id=context.run.id,
                        grader_id=me.user_id,
                        adjusted_levels=adjusted,
                        comment=comment.strip() or None,
                        reviewed_at=datetime.now(UTC),
                    )
                )
            except ImmutabilityViolation as exc:
                # 二度確定できると成績が二つ存在する。やり直しは再採点から。
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            uow.commit()

        fresh = _load(console, me, SubmissionId(submission_id))
        console.refresh_observations(
            fresh.submission,
            fresh.run,
            fresh.task_version,
            subject_profile=fresh.course.subject_profile,
            mark=fresh.mark,
            review=fresh.review,
        )
        return RedirectResponse(f"/courses/{context.course.id}", status_code=303)

    return app


# -- 権限つきの読み出し ------------------------------------------------------


class _Context:
    """1 件のレビューに必要なもの一式。"""

    __slots__ = (
        "course",
        "mark",
        "needs_blind",
        "review",
        "run",
        "submission",
        "task_version",
    )

    def __init__(
        self,
        submission: Submission,
        run: GradingRun,
        task_version: TaskVersion,
        course: Course,
        mark: BlindMark | None,
        review: HumanReview | None,
        needs_blind: bool,
    ) -> None:
        self.submission = submission
        self.run = run
        self.task_version = task_version
        self.course = course
        self.mark = mark
        self.review = review
        self.needs_blind = needs_blind


def _can_grade(auth: AuthService, course_id: CourseId, me: Principal) -> bool:
    try:
        auth.require_grader(course_id, me.user_id)
    except PermissionDenied:
        return False
    return True


def _load(console: Console, me: Principal, submission_id: SubmissionId) -> _Context:
    with console.database.unit_of_work() as uow:
        submission = uow.submissions.get(submission_id)
        if submission is None:
            raise HTTPException(status_code=404, detail="提出が見つかりません")
        task_version = uow.tasks.get_version(submission.task_version_id)
        task = None if task_version is None else uow.tasks.get_task(task_version.task_id)
        if task_version is None or task is None:
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        course = uow.identity.get_course(task.course_id)
        if course is None:
            raise HTTPException(status_code=404, detail="コースが見つかりません")

        auth = AuthService(uow.identity)
        try:
            auth.require_grader(task.course_id, me.user_id)
        except PermissionDenied as exc:
            # 採点できないコースの提出は「無い」と答える。存在を伝えると、
            # 提出 ID の存在自体が漏れる。
            raise HTTPException(status_code=404, detail="提出が見つかりません") from exc

        run = uow.runs.latest_for(submission_id)
        if run is None:
            # 採点が届いていない。**ここで採点しない**（ADR 0007）。
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{submission_id} はまだ採点されていません。"
                    "`uv run aijudge-worker --once` で採点してください。"
                ),
            )
        mark = uow.reviews.find_blind_mark(submission_id)
        review = uow.reviews.find_review_for_run(run.id)

    needs_blind = mark is None and console.needs_blind_mark(submission, course.subject_profile)
    return _Context(submission, run, task_version, course, mark, review, needs_blind)


def _queue_rows(
    console: Console, me: Principal, course_id: CourseId
) -> tuple[Course, tuple[dict, ...], int]:
    with console.database.unit_of_work() as uow:
        auth = AuthService(uow.identity)
        try:
            auth.require_grader(course_id, me.user_id)
        except PermissionDenied as exc:
            raise HTTPException(status_code=404, detail="コースが見つかりません") from exc
        course = uow.identity.get_course(course_id)
        if course is None:
            raise HTTPException(status_code=404, detail="コースが見つかりません")

        pending = uow.reviews.pending_for_course(course_id)
        rows = []
        marked = 0
        for submission, run in pending:
            mark = uow.reviews.find_blind_mark(submission.id)
            if mark is not None:
                marked += 1
            rows.append(
                {
                    "submission": submission,
                    "run": run,
                    "marked": mark is not None,
                    "needs_blind": mark is None
                    and console.needs_blind_mark(submission, course.subject_profile),
                }
            )
    return course, tuple(rows), marked


# -- 表示用のヘルパ ----------------------------------------------------------


LEVEL_FIELD_PREFIX = "level_"


def level_field(code: str) -> str:
    """観点 1 つ分のフォーム項目名。

    **観点ごとに変える。** 共有すると、ブラウザは全観点を 1 つのラジオ
    グループとして扱い、観点をまたいで 1 つしか選べなくなる。実際にそう
    なっていた（テストがフォームを経由せず直接 POST していたので、
    画面を見るまで分からなかった）。
    """
    return f"{LEVEL_FIELD_PREFIX}{code}"


def _parse_levels(
    criteria: tuple[RubricCriterion, ...], form: Mapping[str, object]
) -> dict[CriterionId, int]:
    """フォームから観点ごとの段階を読む。

    観点の取りこぼしは拒否する。欠けたまま確定すると、誰も見ていない観点が
    成績に入る。
    """
    parsed: dict[CriterionId, int] = {}
    missing: list[str] = []
    for criterion in criteria:
        raw = form.get(level_field(criterion.code))
        if raw is None or str(raw).strip() == "":
            missing.append(criterion.code)
            continue
        try:
            level = int(str(raw))
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"{criterion.code}: malformed level {raw!r}"
            ) from None
        allowed = {candidate.level for candidate in criterion.levels}
        if level not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"{criterion.code}: level {level} is not in {sorted(allowed)}",
            )
        parsed[criterion.id] = level

    if missing:
        raise HTTPException(status_code=400, detail=f"missing marks for: {sorted(missing)}")
    return parsed


def _comparison_rows(
    task: TaskVersion, run: GradingRun, mark: BlindMark | None
) -> list[dict[str, object]]:
    """観点ごとに「教員 vs AI」を並べる。"""
    by_id = {score.criterion_id: score for score in run.criterion_scores}
    marks = {} if mark is None else mark.levels
    rows: list[dict[str, object]] = []
    for criterion in task.criteria:
        score = by_id.get(criterion.id)
        human = marks.get(criterion.id)
        rows.append(
            {
                "criterion": criterion,
                "human_level": human,
                "ai_level": None if score is None else score.level,
                # blind 採点が無い提出では突き合わせるものが無い。
                # None は「不一致」ではなく「比べていない」を表す。
                "agrees": None if (human is None or score is None) else human == score.level,
                "score": score,
                "unscored": criterion.id in run.unscored_criteria,
            }
        )
    return rows


def _highlighted_lines(run: GradingRun) -> set[int]:
    lines: set[int] = set()
    for score in run.criterion_scores:
        for evidence in score.evidence:
            span = evidence.span
            if span.kind == "line":
                lines.update(range(span.start_line, span.end_line + 1))
    return lines


def _numbered(source: str) -> list[tuple[int, str]]:
    return list(enumerate(source.replace("\r\n", "\n").split("\n"), 1))
