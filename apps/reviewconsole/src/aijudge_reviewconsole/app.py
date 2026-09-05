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

`/manage` にコース・課題・受講の管理を載せている（`manage.py`）。学期中に
発生する作業（締切の設定、受講者の追加、課題の追加）を教員がターミナルで
行うのは現実的でないため。**科目プロファイルは表示だけで編集させない。**
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from aijudge_admin import allowed_namespaces, list_for_namespaces, pending_counts
from aijudge_authoring import images, render_statement
from aijudge_core import (
    HUMAN_SCORED,
    MIN_JUSTIFICATION_LENGTH,
    ArtifactKind,
    BlindMark,
    Course,
    Finalization,
    FinalizationSource,
    GradingPhase,
    GradingRun,
    HumanReview,
    Role,
    RubricCriterion,
    Submission,
    TaskVersion,
    blocks_finalization,
    content_type_for,
    new_id,
)
from aijudge_core.ids import (
    CourseId,
    CriterionId,
    FinalizationId,
    HumanReviewId,
    SubmissionId,
)
from aijudge_grading import load_profile, project_observations
from aijudge_identity import (
    AuthenticationFailed,
    AuthService,
    PermissionDenied,
    Principal,
    session_cookie_kwargs,
)
from aijudge_persistence import Database, ObservationFileStore
from aijudge_submission import (
    ArtifactStore,
    ImmutabilityViolation,
    StreamingArtifactStore,
    iter_file,
    parse_range,
)

from .manage import _role_counts
from .overview import digests_for, load_units
from .sampling import is_blind_sample
from .submissions import (
    STATE_LABELS,
    Filters,
    distribution_of,
    load_rows,
    newest_first,
    summarize,
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
# 「人が採点する」を表す評価器の名前。**画面に値を書き写さない** ── 書き写すと、
# 模型の側で変えたときに画面だけが古い値を送り続ける。
TEMPLATES.env.globals["HUMAN_SCORED"] = HUMAN_SCORED

# 画面に埋め込んでよい種別。それ以外はダウンロードさせる（#75）。
INLINE_KINDS = (ArtifactKind.IMAGE, ArtifactKind.PDF, ArtifactKind.VIDEO)


def _serve_video(
    console: Console, request: Request, artifact: object, artifact_id: str
) -> Response:
    """動画を Range 対応でストリーム配信する（教員が視聴して採点する）。"""
    store = console.video_store
    if store is None:
        raise HTTPException(status_code=404, detail="提出物が見つかりません")
    key = artifact.storage_key  # type: ignore[attr-defined]
    filename = artifact.filename or artifact_id  # type: ignore[attr-defined]
    try:
        size = store.size(key)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="提出物が見つかりません") from exc
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'inline; filename="{filename}"',
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=300",
    }
    media_type = content_type_for(artifact.filename)  # type: ignore[attr-defined]
    span = parse_range(request.headers.get("range"), size)
    if span is None:
        return StreamingResponse(
            iter_file(store.open_read(key)),
            media_type=media_type,
            headers={**headers, "Content-Length": str(size)},
        )
    start, end = span
    return StreamingResponse(
        iter_file(store.open_read(key), start=start, length=end - start + 1),
        status_code=206,
        media_type=media_type,
        headers={
            **headers,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(end - start + 1),
        },
    )


# **学習者アプリと同じ名前**（#103）。セッションは前から同じ表を
# `AuthService` 経由で共有していて、違うのは Cookie の名前だけだった ──
# そのために同じ人が同じ機械で 2 回ログインしていた。
#
# 入れ替えの日、教員コンソールに入っていた人は 1 度だけログインし直す
# （古い名前の Cookie は誰も読まない）。セッション自体は残っている。
# 受け付ける `Host`（#116）。コンマ区切り。既定は素通し（`*`）。
ENV_ALLOWED_HOSTS = "AIJUDGE_ALLOWED_HOSTS"

SESSION_COOKIE = "aijudge_session"
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
        video_store: StreamingArtifactStore | None = None,
        observations: ObservationFileStore | None = None,
        learner_url: str = "",
        learner_port: int = 8080,
    ) -> None:
        self.database = database
        self.store = artifact_store
        # 動画の置き場所（学習者アプリと同じディレクトリを指す）。
        self.video_store = video_store
        self.profiles_dir = profiles_dir
        self.observations = observations
        # 学習者アプリの場所（#103）。**空なら、開いているホスト名のまま
        # ポートだけ変えて渡す**（#114）── 決め打ちの名前へ渡すと、その名前で
        # 開いていない人の Cookie が付いていかない。
        self.learner_url = learner_url.rstrip("/")
        self.learner_port = learner_port
        self._rates: dict[str, float] = {}
        # 直近に足した課題。管理画面が「何が起きたか」を返すために持つ。
        # コースを添えるのは Console が全利用者で共有だから。
        self.last_task: tuple[str, object] | None = None
        # 直近のテストケース生成の失敗を (course_id, task_id, 理由) で持つ。
        # **理由を持つのは、決めつけないため**（#52）。
        self.last_test_case_error: tuple[str, str, str] | None = None
        # 直近の再採点の結果を (course_id, 件数) で持つ。
        self.last_regrade: tuple[str, int] | None = None
        # 直近の一括確定の結果を (course_id, outcome) で持つ。
        # **コースを添えるのは Console が全利用者で共有だから。** 添えないと、
        # 別のコースの教員に他コースの課題名が出る。
        self.last_finalize: tuple[str, object] | None = None
        # 直前に片付けた問題セットの内訳（#59）。**件数だけでは足りない** ──
        # 削除と取り下げが混ざるので、何がどちらになったかを画面に出す。
        self.last_clear: tuple[str, object] | None = None
        # 直前に上げた課題文の画像の貼り付け行（#64）。**URL を手で書かせない。**
        self.last_image: tuple[str, str] | None = None
        # 直前に採点へ回した件数（#67）。**0 件だったことも伝える** ──
        # 押したのに何も起きなかったのが正常なのか異常なのか分からない。
        self.last_release: tuple[str, int] | None = None

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
        """blind 採点の対象か。**抽出は提出 ID のハッシュで決める**（教員の
        選択ではない・ADR 0005）。

        教員・TA 自身の試行は母集団から外す（#108）。一致度は「AI の判定が
        学習者の提出に対してどれだけ人と合うか」であって、教員が動作確認で
        通した入力は測りたいものではない。
        """
        if submission.is_trial:
            return False
        return is_blind_sample(str(submission.id), self.blind_sample_rate(subject_profile))

    def source_of(self, submission: Submission) -> str:
        """提出物を本文として読む。**読めるものだけ**（#75）。

        以前は種別を見ずに `decode` していたので、画像や PDF の提出は化けた
        バイナリとして並んだ。**人が採点する画像・PDF 課題では、中身が
        見られないと採点そのものができない。**
        """
        for artifact in submission.gradable_artifacts:
            if artifact.kind in INLINE_KINDS or artifact.kind is ArtifactKind.DOCX:
                return ""
            try:
                return self.store.get(artifact.storage_key).decode("utf-8", "replace")
            except Exception:  # pragma: no cover - ストアが読めない状況
                return ""
        return ""

    def files_of(self, submission: Submission) -> tuple[dict[str, object], ...]:
        """提出物を画面に出すための行。種別が出し方を決める。"""
        return tuple(
            {
                "id": str(artifact.id),
                "filename": artifact.filename or "提出物",
                "is_image": artifact.kind is ArtifactKind.IMAGE,
                "is_pdf": artifact.kind is ArtifactKind.PDF,
                "is_video": artifact.kind is ArtifactKind.VIDEO,
            }
            for artifact in submission.gradable_artifacts
        )

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
        # 教員・TA 自身の試行は測定に入れない（#108）。観測は κ の証拠なので、
        # 学習者の提出でないものが混ざると、測った一致度がその分だけ嘘になる
        # （ADR 0005 が `Finalization` と `HumanReview` を分けたのと同じ理由）。
        if submission.is_trial:
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

    # **`Host` を 1 か所で検査する**（#116）。`Host` も `X-Forwarded-*` も
    # クライアントが決められるので、通してしまうと、それを読む全ての処理が
    # 同じ穴を持つ（相手側へのリンク・絶対 URL の生成）。
    #
    # 既定は素通し（`*`）。**間違った既定は運用を黙って壊す** ── 名前が
    # 分かるのは運用者だけなので、逆プロキシを前に立てるときに設定する
    # （`docs/RUNNING.md`）。
    allowed = [h.strip() for h in os.environ.get(ENV_ALLOWED_HOSTS, "*").split(",") if h.strip()]
    if allowed and allowed != ["*"]:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed)

    # 管理画面（コース・課題・受講）。再確認の依頼とは別の関心事だが、
    # 教員に 2 つの Web アプリを使わせないため同じアプリに載せる。
    from .manage import register as register_manage

    app.include_router(register_manage(TEMPLATES))

    # 課題を足す API。非対話の呼び出し元（移行のエージェント）のための入口で、
    # 認証は API トークン。**Cookie を見ない**ので CSRF の経路が無い。
    from .api import register as register_api

    app.include_router(register_api())

    # -- ログイン ----------------------------------------------------------

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, changed: str = "") -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request, "login.html", {"error": None, "changed": bool(changed)}
        )

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

    # -- 担当コースとコースのメニュー ----------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        """担当コースの一覧。**開かずに判断できるだけの情報を出す**（`overview.py`）。

        コード・コース名・学期の 3 列だけでは、どのコースに用があるのかを
        全部開くまで判断できない。
        """
        principal = current_principal(request)
        if principal is None:
            return RedirectResponse("/login", status_code=303)
        with console.database.unit_of_work() as uow:
            auth = AuthService(uow.identity)
            courses = []
            attending = []
            # テナント管理者かはコースの受講に頼らず利用者自身の属性で決まる
            # （#128）。`courses_for` も管理者には全コースを返すので、下の
            # ループは受講登録の無いコースも普通に回す。
            is_admin = principal.is_tenant_admin
            for course in auth.courses_for(principal.tenant_id, principal.user_id):
                enrollment = uow.identity.find_enrollment(course.id, principal.user_id)
                if _can_grade(auth, course.id, principal):
                    courses.append(course)
                else:
                    # **受講しているコースも出す**（#103）。役割はコースごとに
                    # 決まるので、同じ人が「A では学習者・B では教員」になる。
                    # 出さないと、その人は入口が 2 つあること自体に気づけない。
                    attending.append(
                        {"course": course, "role": None if enrollment is None else enrollment.role}
                    )
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "me": principal,
                "digests": digests_for(console.database, courses),
                # 採点しないコース（学習者として取っているもの）。
                "attending": attending,
                # 学習者アプリの場所。**設定されていなければ案内だけ出す** ──
                # 1 台で両方動かしている運用では相手の URL を機械は知らない。
                "learner_url": counterpart_url(
                    request, configured=console.learner_url, port=console.learner_port
                ),
                # コースを作れるのは管理者だけ。作る口をここに置くのは、
                # 入口が 2 つあること自体が分かりにくさの元だったから。
                "is_admin": is_admin,
                "profiles": sorted(path.stem for path in console.profiles_dir.glob("*.yaml")),
            },
        )

    @app.get("/courses/{course_id}", response_class=HTMLResponse)
    def course_menu(request: Request, course_id: str, me: Me) -> HTMLResponse:
        """コースの入口。**ここは分岐だけを持つ。**

        1 枚に全部（自動確定・課題・受講者・プロファイル）を積むと、教員は
        「第 3 回の締切を直す」ために縦に長い画面を目で探すことになる。
        コース全体の設定と、問題セットごとの設定と、再確認の依頼を、同じ階層に並べる。
        """
        course, rows, _marked = _queue_rows(console, me, CourseId(course_id))
        pending = pending_counts(console.database, course.id)
        _course, blind, blind_marked = _blind_rows(console, me, CourseId(course_id))
        unfinalized = sum(pending.values())
        with console.database.unit_of_work() as uow:
            submitted = summarize(load_rows(uow, course))
        with console.database.unit_of_work() as uow:
            units = load_units(uow, course, pending=pending)
            task_ids = {task.id for task in uow.tasks.list_for_course(course.id)}
            drafts = sum(
                1 for version in uow.tasks.list_versions_in_review() if version.task_id in task_ids
            )
            enrollment = uow.identity.find_enrollment(course.id, me.user_id)
            enrollments = uow.identity.list_enrollments(course.id)
        profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
        kcs = list_for_namespaces(console.database, allowed_namespaces(profile))
        return TEMPLATES.TemplateResponse(
            request,
            "course_menu.html",
            {
                "me": me,
                "course": course,
                "units": units,
                "contested": len(rows),
                "unfinalized": unfinalized,
                "submitted": submitted,
                "blind_pending": len(blind),
                "blind_marked": blind_marked,
                "min_sample_size": min_sample_size,
                "drafts": drafts,
                "kc_count": len(kcs),
                # 直前に片付けた問題セットの内訳（#59・#82）。**件数の合計では
                # 足りない** ── 1 回の操作で課題ごとに削除と取り下げに
                # 分かれるので、何がどちらになったのかが言えなくなる。
                # 片付けた直後の着地点がこの画面なので、ここに出す。
                "last_clear": (
                    console.last_clear[1]
                    if console.last_clear is not None and console.last_clear[0] == str(course.id)
                    else None
                ),
                # 受講者はここに出す。**「コース全体の設定」の中ではない** ──
                # 知識要素・未承認の課題と同じく自分のページを持つものなので
                # 同じ並びに置く。設定の中に埋めると、開くまで人数が見えない。
                "people_count": len(enrollments),
                "role_counts": _role_counts(enrollments),
                # TA にはコースの設定を開かせない（`manage.py` の権限と揃える）。
                # **テナント管理者は受講登録が無くても管理できる**（#128）。
                "can_manage": me.is_tenant_admin
                or (enrollment is not None and enrollment.role in (Role.INSTRUCTOR, Role.ADMIN)),
            },
        )

    # -- 待ち行列 ----------------------------------------------------------

    @app.get("/courses/{course_id}/queue", response_class=HTMLResponse)
    def queue(request: Request, course_id: str, me: Me) -> HTMLResponse:
        course, rows, marked_count = _queue_rows(console, me, CourseId(course_id))
        with console.database.unit_of_work() as uow:
            # 対応済みの依頼も出す（#102）。**待ち行列の下に置く** ── 上に
            # 混ぜると、手を動かす必要があるものが埋もれる（ADR 0009）。
            resolved = _resolved_rows(uow, course.id)
        return TEMPLATES.TemplateResponse(
            request,
            "queue.html",
            {
                "me": me,
                "course": course,
                "section": {"label": "再確認の依頼", "href": f"/courses/{course.id}/queue"},
                "rows": rows,
                "resolved": resolved,
                "marked_count": marked_count,
                "min_sample_size": min_sample_size,
            },
        )

    @app.get("/courses/{course_id}/submissions", response_class=HTMLResponse)
    def submissions_page(
        request: Request,
        course_id: str,
        me: Me,
        unit: str = "",
        task: str = "",
        learner: str = "",
        role: str = "",
        state: str = "",
        adopted: int = 0,
    ) -> HTMLResponse:
        """提出の一覧。**実際に何が出ているかを見る場所**（`submissions.py`）。

        待ち行列と確定処理は手を動かす必要があるものだけを出すので、全提出は
        そこに混ぜない。絞り込みは URL に載せる ── 絞った結果をそのまま TA や
        学生に渡せる。
        """
        course, _rows, _marked = _queue_rows(console, me, CourseId(course_id))
        with console.database.unit_of_work() as uow:
            rows = load_rows(uow, course)
            units = load_units(uow, course)
        # **問題セットを選んだら、問題の選択肢もそのセットに絞る。** 全課題を
        # 並べたままにすると、選んだセットに無い問題を選べてしまい、結果が
        # 常に空になる（教員には絞り込みが壊れたように見える）。
        #
        # 問題セットの選択肢そのものは絞らない ── 絞ると別のセットに移れなくなる。
        choices = tuple(group for group in units if not unit or group.key == unit)
        if unit:
            known = {str(task_obj.id) for group in choices for task_obj, _v in group.tasks}
            if task not in known:
                task = ""
        filters = Filters(
            unit=unit,
            task=task,
            learner=learner.strip(),
            role=role,
            state=state,
            adopted=bool(adopted),
        )
        shown = newest_first([row for row in rows if filters.matches(row)])
        return TEMPLATES.TemplateResponse(
            request,
            "submissions.html",
            {
                "me": me,
                "course": course,
                "section": {"label": "提出", "href": f"/courses/{course.id}/submissions"},
                "rows": shown,
                "total": len(rows),
                "filters": filters,
                # 問題セットの選択肢は全部、問題の選択肢は選んだセットの中だけ。
                "units": units,
                "task_choices": choices,
                "roles": [role.value for role in Role],
                "states": STATE_LABELS,
                "chart": distribution_of(shown),
            },
        )

    @app.get("/courses/{course_id}/finalize", response_class=HTMLResponse)
    def finalize_queue(request: Request, course_id: str, me: Me) -> HTMLResponse:
        """確定処理。**依頼が出なかった提出を閉じる導線**（ADR 0010）。

        再確認の依頼は学習者の申し出に答える仕事で、こちらは残りを閉じる
        仕事である。学期末に成績が閉じるには後者が要る（依頼はごく一部に
        しか出ない）。粒度は 3 つ ── 提出ごと・問題ごと・問題セットごと。
        """
        course, rows, _marked = _queue_rows(console, me, CourseId(course_id))
        pending = pending_counts(console.database, course.id)
        with console.database.unit_of_work() as uow:
            units = load_units(uow, course, pending=pending)
            enrollment = uow.identity.find_enrollment(course.id, me.user_id)
            open_rows = []
            for submission, run in uow.reviews.pending_for_course(course.id):
                # 教員・TA 自身の試行は成績ではない（#108）。閉じる対象に
                # 出すと、いつまでも減らない未確定として残り続ける。
                if submission.is_trial:
                    continue
                version = uow.tasks.get_version(submission.task_version_id)
                task = None if version is None else uow.tasks.get_task(version.task_id)
                request_row = uow.reviews.find_request_for_run(run.id)
                open_rows.append(
                    {
                        "submission": submission,
                        "run": run,
                        "task": task,
                        "learner": uow.identity.get_user(submission.learner_id),
                        # 未対応の異議申立があるものは一括では閉じない。
                        # **1 件ずつ読むもの**として印を付ける。
                        "contested": blocks_finalization(request_row),
                    }
                )
        return TEMPLATES.TemplateResponse(
            request,
            "finalize.html",
            {
                "me": me,
                "course": course,
                "section": {"label": "確定処理", "href": f"/courses/{course.id}/finalize"},
                "rows": open_rows,
                "units": [unit for unit in units if unit.unfinalized],
                "pending": pending,
                "contested": len(rows),
                # 一括確定は担当教員以上（`manage.py` の権限と揃える）。
                # **テナント管理者は受講登録が無くても管理できる**（#128）。
                "can_manage": me.is_tenant_admin
                or (enrollment is not None and enrollment.role in (Role.INSTRUCTOR, Role.ADMIN)),
                "min_reason": MIN_JUSTIFICATION_LENGTH,
                "last_finalize": (
                    console.last_finalize[1]
                    if console.last_finalize is not None
                    and console.last_finalize[0] == str(course.id)
                    else None
                ),
            },
        )

    @app.get("/courses/{course_id}/blind", response_class=HTMLResponse)
    def blind_queue(request: Request, course_id: str, me: Me) -> HTMLResponse:
        """blind 採点の待ち。**再確認の依頼とは別の画面**（ADR 0005 / 0009）。"""
        course, rows, marked_count = _blind_rows(console, me, CourseId(course_id))
        return TEMPLATES.TemplateResponse(
            request,
            "blind_queue.html",
            {
                "me": me,
                "course": course,
                "section": {"label": "blind 採点", "href": f"/courses/{course.id}/blind"},
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
                # 提出物そのもの（#75）。**人が採点する画像・PDF 課題では、
                # これが見えないと採点できない。**
                "files": console.files_of(context.submission),
                "criteria": context.task_version.criteria,
                "course": context.course,
                "section": {
                    "label": "再確認の依頼",
                    "href": f"/courses/{context.course.id}/queue",
                },
                "task_meta": context.task,
                "learner": context.learner,
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

    @app.get("/images/{course_id}/{name}")
    def statement_image(request: Request, course_id: str, name: str, me: Me) -> Response:
        """課題文に貼られた画像（#111）。

        **課題文が指す経路をそのまま持つ。** 貼り付ける 1 行は
        `![](/images/<course>/<name>)` で（`aijudge_authoring.images`）、
        絶対 URL を埋め込まないのは、課題文が学習者と教員の両方の画面に
        出るからである ── 相対パスなら、開いている側が自分で返す。

        **返す側がこれまで居なかった。** 経路は `/manage` 接頭辞の付いた
        ルータの中で宣言されていたので、実際には
        `/manage/courses/<id>/images/<name>` にしか無く、課題文が指す
        `/images/...` は 404 だった。プレビュー・採点画面・blind・TA の
        課題ページで、画像が全部欠けていた。

        **採点できる人に見せる。** 課題文の一部なので、TA が読む画面
        （#102）でも要る。
        """
        with console.database.unit_of_work() as uow:
            auth = AuthService(uow.identity)
            if not _can_grade(auth, CourseId(course_id), me):
                # 採点できないコースの画像は「無い」と答える（提出物と同じ）。
                raise HTTPException(status_code=404, detail="画像が見つかりません")
        try:
            payload = console.store.get(images.storage_key(course_id, name))
        except Exception as exc:
            raise HTTPException(status_code=404, detail="画像が見つかりません") from exc
        return Response(
            content=payload,
            media_type=images.content_type(name),
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @app.get("/review/{submission_id}/artifacts/{artifact_id}")
    def submitted_file(request: Request, submission_id: str, artifact_id: str, me: Me) -> Response:
        """提出されたファイルそのものを返す（#75）。

        **担当教員だけ。** `_load` が受講と役割を確かめている（他人のコースの
        提出は 404）ので、そこを通す。

        `Content-Type` は拡張子から引き、分からなければ
        `application/octet-stream` にする ── 学習者が出したファイルを返す
        経路なので、ブラウザに解釈させる余地を作らない。
        """
        context = _load(console, me, SubmissionId(submission_id))
        artifact = next((a for a in context.submission.artifacts if str(a.id) == artifact_id), None)
        if artifact is None:
            raise HTTPException(status_code=404, detail="提出物が見つかりません")
        if artifact.kind is ArtifactKind.VIDEO:
            return _serve_video(console, request, artifact, artifact_id)
        try:
            payload = console.store.get(artifact.storage_key)
        except Exception as exc:
            raise HTTPException(status_code=404, detail="提出物が見つかりません") from exc
        return Response(
            content=payload,
            media_type=content_type_for(artifact.filename),
            headers={
                "Cache-Control": "private, max-age=300",
                "Content-Disposition": ("inline" if artifact.kind in INLINE_KINDS else "attachment")
                + f'; filename="{artifact.filename or artifact_id}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

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
                "files": console.files_of(context.submission),
                "rows": _comparison_rows(context.task_version, context.run, context.mark),
                "highlights": _highlighted_lines(context.run),
                "review": context.review,
                "was_blind": context.mark is not None,
                "review_request": context.request,
                "finalization": context.finalization,
                "awaiting_ai": context.awaiting_ai,
                "learner": context.learner,
                "task_meta": context.task,
                # 出題文（#105）。**採点している人が問題を読めないのはおかしい。**
                # blind の画面には最初から出ていて、確定を決めるこの画面だけが
                # 持っていなかった。描画は学習者と同じ関数を通す ── 別の
                # 描画を当てると、数式や画像の食い違いがここでは見えない。
                "statement_html": render_statement(context.task_version.statement),
                "section": {
                    "label": "再確認の依頼",
                    "href": f"/courses/{context.course.id}/queue",
                },
                "min_reason": MIN_JUSTIFICATION_LENGTH,
            },
        )

    @app.post("/review/{submission_id}/finalize")
    async def finalize(request: Request, submission_id: str, me: Me) -> Response:
        form = await request.form()
        context = _load(console, me, SubmissionId(submission_id))
        if context.awaiting_ai:
            # **AI 評価の到着前に確定させない。** 確定すると、直後に届く
            # AI 段階の採点が確定済みの成績を追い越すことになる。
            raise HTTPException(
                status_code=409,
                detail="AI 評価がまだ届いていません。届いてから確定してください。",
            )
        final = _parse_levels(context.task_version.criteria, form)
        comment = str(form.get("comment", ""))
        # 遅延の減点の免除。**評価の修正とは別物**（ADR 0013）。減点の無い
        # 採点で送られてきても害は無いが、意味を持つのは減点があるときだけ。
        waived = context.run.penalty is not None and form.get("waive_penalty") is not None

        # 変更した観点だけを持つ。触っていない観点は AI に同意した意味。
        machine = {score.criterion_id: score.level for score in context.run.criterion_scores}
        adjusted = {
            criterion_id: level
            for criterion_id, level in final.items()
            if machine.get(criterion_id) != level
        }

        text = comment.strip()
        if len(text) < MIN_JUSTIFICATION_LENGTH:
            # **根拠説明を必須にする。** 学習者には AI の判定が既に示されて
            # おり、覆すなら理由が要る。覆さない場合も「確認した」だけでは
            # 学習者に何も返らない（設計原則 P4 を人間の判定にも適用する）。
            raise HTTPException(
                status_code=400,
                detail=(
                    f"確定の根拠を {MIN_JUSTIFICATION_LENGTH} 文字以上で書いてください"
                    "（学習者に表示されます）"
                ),
            )

        review_id = HumanReviewId(new_id("hrv"))
        now = datetime.now(UTC)
        with console.database.unit_of_work() as uow:
            request = uow.reviews.find_request_for_run(context.run.id)
            try:
                # 2 つの記録を書く。**別物である**（ADR 0010）。
                # HumanReview は「教員がこの 1 件を読んだ」── 一致度の測定が
                # 証拠に使える唯一の記録。Finalization は「成績が確定した」──
                # 一括確定や自動確定でも起きる事実。
                uow.reviews.save_review(
                    HumanReview(
                        id=review_id,
                        grading_run_id=context.run.id,
                        grader_id=me.user_id,
                        adjusted_levels=adjusted,
                        penalty_waived=waived,
                        comment=text,
                        request_id=None if request is None else request.id,
                        reviewed_at=now,
                    )
                )
                if context.finalization is None:
                    uow.reviews.save_finalization(
                        Finalization(
                            id=FinalizationId(new_id("fin")),
                            grading_run_id=context.run.id,
                            source=FinalizationSource.INSTRUCTOR_REVIEW,
                            actor_id=me.user_id,
                            review_id=review_id,
                            justification=text,
                            finalized_at=now,
                        )
                    )
                # 既に確定済みのことがある。**自動確定した成績に学習者が
                # 異議を申し立て、教員が読む経路。** 確定の記録は最初の
                # ものを残す（追記のみ、P8）。教員が読んだ事実は
                # `HumanReview` の側に付き、学習者にはそちらが出る。
                if request is not None:
                    uow.reviews.resolve_request(request.id, review_id)
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
    """1 件のレビューに必要なもの一式。

    文脈（どの回のどの課題か、誰の何回目の提出か）も含める。すべての画面に
    出すため、読み出しを 1 か所にまとめてある。
    """

    __slots__ = (
        "awaiting_ai",
        "course",
        "finalization",
        "learner",
        "mark",
        "needs_blind",
        "request",
        "review",
        "run",
        "submission",
        "task",
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
        task: object | None = None,
        learner: object | None = None,
        request: object | None = None,
        finalization: Finalization | None = None,
        awaiting_ai: bool = False,
    ) -> None:
        self.submission = submission
        self.run = run
        self.task_version = task_version
        self.course = course
        self.mark = mark
        self.review = review
        self.needs_blind = needs_blind
        self.task = task
        self.learner = learner
        self.request = request
        self.finalization = finalization
        self.awaiting_ai = awaiting_ai


# ホスト名として通す形（#116）。**ヘッダの中身を信用しない。**
_HOSTNAME = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")


def counterpart_url(request: Request, *, configured: str, port: int) -> str:
    """相手側アプリの場所（#114）。

    **ブラウザが今いるホスト名をそのまま使う。** セッション Cookie は
    ホスト単位（`Domain` を付けていない・ポートは無視される）なので、
    起動時に決め打ちした名前へ渡すと、その名前で開いていない人の Cookie は
    付いていかない ── 1 台が `localhost`・IP・短い名前・FQDN・tailnet 名の
    どれでも応じる以上、「どの名前で来たか」は起動時には決まらない。

    `configured` が入っていればそちらを優先する。逆プロキシの後ろや、
    本当に別のホストに置いてある運用では、名前を知っているのは運用者の
    ほうだから（その場合セッションは共有されない ── 別のホストなら Cookie は
    そもそも届かない）。

    **ヘッダは検査してから使う**（#116）。`Host` も `X-Forwarded-*` も
    クライアントが決められるので、素通しすると 2 つ通る:

    - `X-Forwarded-Proto: javascript` と `%0a` を含むホスト名で
      `javascript://x%0aalert(1)/…` が作れる（改行が `//` のコメントを終わらせる）
    - リンク先が攻撃者のホストになり、同じ見た目のログイン画面に渡せる

    いま被害者に踏ませるのは難しい（ブラウザは自分が開いた URL の `Host` しか
    送らない）。**難しいことと塞がっていることは別である** ── 共有キャッシュや、
    外部入力を `X-Forwarded-*` に写す逆プロキシがあれば成立し、逆プロキシは
    #103 の次の段でまさに前に立てるものである。
    """
    if configured:
        return configured
    scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
    if scheme not in ("http", "https"):
        # 知らないスキームは使わない（`javascript:` を href に置かせない）。
        scheme = "https" if request.url.scheme == "https" else "http"
    forwarded = request.headers.get("x-forwarded-host")
    host = (forwarded or request.url.hostname or "localhost").split(":")[0]
    if not _HOSTNAME.match(host):
        # 形の合わない名前は、そもそも自分のものではない。
        host = request.url.hostname or "localhost"
        if not _HOSTNAME.match(host):
            host = "localhost"
    return f"{scheme}://{host}:{port}"


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
        request = uow.reviews.find_request_for_run(run.id)
        finalization = uow.reviews.find_finalization_for_run(run.id)
        # AI 評価がまだ来ていないだけなのか、来たが判定できなかったのかを
        # 区別する。前者は待てばよく、後者は教員が観点を埋める。
        awaiting_ai = uow.jobs.awaiting(submission_id, GradingPhase.AI)
        learner = uow.identity.get_user(submission.learner_id)

    needs_blind = mark is None and console.needs_blind_mark(submission, course.subject_profile)
    return _Context(
        submission,
        run,
        task_version,
        course,
        mark,
        review,
        needs_blind,
        task=task,
        learner=learner,
        request=request,
        finalization=finalization,
        awaiting_ai=awaiting_ai,
    )


def _resolved_rows(uow, course_id: CourseId) -> tuple[dict, ...]:
    """対応済みの再確認の依頼。**答えた人とともに残す**（#102）。

    以前は対応した瞬間に画面から消えていた。学習者の申し出も、それに誰が
    どう答えたかも、コンソールからは辿れない ── 同じ学習者が「前も同じ
    ことを聞いた」と言ってきたとき、教員には確かめる手段が無かった。

    記録そのものは前からある（`ReviewRequest.resolved_by` → `HumanReview`）。
    足りていなかったのは出す場所だけである。
    """
    rows = []
    for submission, run, request in uow.reviews.requested_for_course(
        course_id, include_resolved=True
    ):
        if not request.resolved:
            continue
        review = uow.reviews.find_review_for_run(run.id)
        version = uow.tasks.get_version(submission.task_version_id)
        task = None if version is None else uow.tasks.get_task(version.task_id)
        rows.append(
            {
                "submission": submission,
                "run": run,
                "request": request,
                "task": task,
                "learner": uow.identity.get_user(submission.learner_id),
                # 答えた人。**居ないことがある** ── 依頼が解決済みなのに
                # レビューが引けないなら、そう出す（居ない人の名前を作らない）。
                "answered_by": (
                    None if review is None else uow.identity.get_user(review.grader_id)
                ),
                "review": review,
            }
        )
    rows.sort(
        key=lambda row: row["review"].reviewed_at if row["review"] else row["request"].requested_at,
        reverse=True,
    )
    return tuple(rows)


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

        # **待ち行列は学習者からの再確認の依頼**（ADR 0009）。
        # 全提出を並べると受講 91 名 × 課題数になり、何から見ればよいか
        # 分からない。AI の判定は採点直後に学習者へ示しているので、
        # 疑いが出たものだけが人間の判断を要する。
        requested = uow.reviews.requested_for_course(course_id)
        rows = []
        marked = 0
        for submission, run, request in requested:
            mark = uow.reviews.find_blind_mark(submission.id)
            if mark is not None:
                marked += 1
            version = uow.tasks.get_version(submission.task_version_id)
            task = None if version is None else uow.tasks.get_task(version.task_id)
            learner = uow.identity.get_user(submission.learner_id)
            rows.append(
                {
                    "submission": submission,
                    "run": run,
                    "request": request,
                    "task": task,
                    "learner": learner,
                    "marked": mark is not None,
                    "needs_blind": mark is None
                    and console.needs_blind_mark(submission, course.subject_profile),
                }
            )
    return course, tuple(rows), marked


def _blind_rows(
    console: Console, me: Principal, course_id: CourseId
) -> tuple[Course, tuple[dict, ...], int]:
    """blind 採点がまだ付いていない、抽出された提出。

    **再確認の依頼とは別の仕事である。** あちらは学習者からの申し出に
    答えるもので、こちらは一致度を測るための正解データ作り（ADR 0005）。
    混ぜて並べると、教員はどちらの理由でその行が出ているのか分からない。
    """
    with console.database.unit_of_work() as uow:
        auth = AuthService(uow.identity)
        try:
            auth.require_grader(course_id, me.user_id)
        except PermissionDenied as exc:
            raise HTTPException(status_code=404, detail="コースが見つかりません") from exc
        course = uow.identity.get_course(course_id)
        if course is None:
            raise HTTPException(status_code=404, detail="コースが見つかりません")

        rows = []
        marked = 0
        for submission, run in uow.reviews.pending_for_course(course_id, include_decided=True):
            if not console.needs_blind_mark(submission, course.subject_profile):
                continue
            if uow.reviews.find_blind_mark(submission.id) is not None:
                marked += 1
                continue
            version = uow.tasks.get_version(submission.task_version_id)
            task = None if version is None else uow.tasks.get_task(version.task_id)
            rows.append(
                {
                    "submission": submission,
                    "run": run,
                    "task": task,
                    "learner": uow.identity.get_user(submission.learner_id),
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
                # **人が採点する観点は「採点できず」ではない。** 評価器を
                # 割り当てていないので、機械の判定が無いのが正しい状態で、
                # 教員が段階を入れて初めて埋まる（Issue #7）。
                "awaiting_human": criterion.id in run.awaiting_human,
                # AND のゲートで打ち切った観点。**「採点できず」ではない** ──
                # 0% は確定した結果で、人が埋めるものは何も無い（ADR 0015）。
                "gated": criterion.id in run.skipped_criteria,
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
