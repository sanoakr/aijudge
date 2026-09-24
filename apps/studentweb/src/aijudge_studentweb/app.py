"""学習者向け Web アプリ。

提出して、結果を見る。それだけ。

**見せる範囲は `visibility.py` が決める。** テンプレートの条件分岐に散らすと、
画面を 1 つ足したときに漏れる（漏れる方向は必ず「確定前の AI 判定が見える」
側で、それは設計原則 P5 を壊す）。

権限はコースの受講で決まる。**URL を推測されても他人の提出は見えない。**
UI で隠すのは表示の都合であって権限ではないので、リクエストごとに
受講と所有者を確かめる。
"""

from __future__ import annotations

import contextlib
import os
import re
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

import aijudge_webui as webui
from aijudge_authoring import images, render_statement
from aijudge_core import (
    MIN_JUSTIFICATION_LENGTH,
    PURGED_MESSAGE,
    STREAMED_SUFFIXES,
    ArtifactKind,
    CampusAccess,
    Course,
    GradingPhase,
    ReviewRequest,
    Role,
    Submission,
    SubmissionWindow,
    Task,
    TaskVersion,
    allowed_suffixes,
    campus_access,
    content_disposition,
    content_type_for,
    grace_minutes,
    kind_for,
    may_see,
    may_submit_before_open,
)
from aijudge_core.ids import (
    ArtifactId,
    CourseId,
    ReviewRequestId,
    SubmissionId,
    TaskVersionId,
    TenantId,
    UserId,
    new_id,
)
from aijudge_grading import load_profile
from aijudge_identity import (
    DEFAULT_LOGIN_LABEL,
    AuthenticationFailed,
    AuthService,
    GoogleOidcProvider,
    PermissionDenied,
    Principal,
    demo_course_from_env,
    session_cookie_kwargs,
)
from aijudge_persistence import Database
from aijudge_skill.portfolio import split_evidence
from aijudge_submission import (
    ArtifactStore,
    FilesystemUploadSessions,
    IncomingFile,
    OffsetMismatch,
    StreamingArtifactStore,
    SubmissionRejected,
    SubmissionService,
    TooLarge,
    UploadSessionError,
    artifact_storage_key,
    iter_file,
    parse_range,
)
from aijudge_telemetry import RequestContextMiddleware

from .audit_context import request_id_of, source_ip_of
from .progress import EMPTY, load_progress
from .visibility import ResultView, build_result_view


def _read_app_version() -> str:
    """release-tagging（ルート pyproject の version、`v<version>` タグ）を読む。

    デプロイは `git checkout --detach vX.Y.Z` した作業木からそのまま起動する
    ので、リポジトリルートの `pyproject.toml` がデプロイ済みタグを表す。
    `apps/studentweb/pyproject.toml` 自身にも `version` はあるが、
    こちらは `0.0.1` に固定されたプレースホルダで運用しない（`name` で見分ける）。
    フッターの表示を壊す理由にはならないので、読めなければ "unknown" とする。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "pyproject.toml"
        if not candidate.is_file():
            continue
        try:
            data = tomllib.loads(candidate.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        project = data.get("project")
        if isinstance(project, dict) and project.get("name") == "aijudge":
            version = project.get("version")
            if isinstance(version, str):
                return version
    return "unknown"


def _read_copyright_notice() -> str:
    """`LICENSE` の Copyright 行を読んで著作権表示を作る（#145）。

    表記を手で書き写すと `LICENSE` と footer がいずれずれる。
    開始年は `LICENSE` の記載のまま、終了年は表示時点の年（同じなら 1 年だけ
    出す）。読めなければ空文字を返す（フッターの他の表示を道連れにしない）。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "LICENSE"
        if not candidate.is_file():
            continue
        match = re.search(
            r"^\s*Copyright\s+(\d{4})\s+(.+?)\s*$", candidate.read_text(), re.MULTILINE
        )
        if match is None:
            return ""
        start_year, holder = match.group(1), match.group(2)
        current_year = str(datetime.now(UTC).year)
        years = start_year if start_year == current_year else f"{start_year}–{current_year}"
        return f"© {years} {holder}"
    return ""


APP_VERSION = _read_app_version()


# 見た目は `packages/webui` が 1 か所で持つ（#184）。テンプレートの探索先に
# 共有の断片（`_theme_boot.html` / `_theme_switch.html`）を足す ── 自分の
# `templates/` を先に見るので、同名を置けばアプリ側で上書きできる。
def _is_demo_course(course_id: object) -> bool:
    """このコースはデモか（#194）。

    **提出のたびに、描画のたびに環境から読む。** 値を起動時に固定すると、
    指名を変えたあとも再起動まで古い判定が残る ── 環境変数は配置の都合で
    変わりうるので、読むのは安いほうに合わせる。

    テンプレートからも呼ぶ（デモの帯を出すため）。環境変数を読むだけで
    DB は引かないので、`root_prefix()` と同じ扱いでよい ── ADR 0017 で
    「描画中に DB を引かない」と決めた線の内側である。
    """
    demo = demo_course_from_env()
    return demo is not None and str(demo.course_id) == str(course_id)


TEMPLATES = Jinja2Templates(
    directory=[str(Path(__file__).parent / "templates"), str(webui.TEMPLATES_DIR)]
)
TEMPLATES.env.globals["app_version"] = APP_VERSION
# 日時は UTC で保存し、表示だけ機関の時刻に直す（`aijudge_webui.local_filter`）。
# テンプレートで `strftime` を直に呼ばない ── 呼ぶと UTC のまま出る。
TEMPLATES.env.filters["local"] = webui.local_filter
TEMPLATES.env.globals["copyright_notice"] = _read_copyright_notice()
# デモコースの帯を出すのに使う（#194）。環境変数を読むだけの純関数。
TEMPLATES.env.globals["is_demo_course"] = _is_demo_course
# 消した動画の文面（ADR 0020）。**両アプリで同じ値を使う**ので、テンプレートに
# 文字列を直接書かない ── 片方だけ直る形にしない。
TEMPLATES.env.globals["purged_message"] = lambda: PURGED_MESSAGE
# 利用ガイド（#327）。**学生向けの頁へ直に送る** ── 索引に落とすと、学生は
# TA 向け・教員向けと並んだ一覧から自分の頁を選ぶことになる。
TEMPLATES.env.globals["guide_url"] = lambda: webui.guide_url("student")


def _static_url(name: str) -> str:
    """CSS への URL。**版を付ける** ── 付けないと配置し直したのにブラウザが
    前の CSS を使い続け、症状は「配置に失敗した」ように見える。"""
    return webui.asset_url(name, version=APP_VERSION)


TEMPLATES.env.globals["static_url"] = _static_url

# 受け付ける `Host`（#116）。コンマ区切り。既定は素通し（`*`）。
ENV_ALLOWED_HOSTS = "AIJUDGE_ALLOWED_HOSTS"
# アクセスログに残さない経路。画像の取り出しと、画面が数秒ごとに叩く
# 「まだ動いているか」の問い合わせ ── 締切前は 1 人あたり毎分 30 行になる。
# CSS も残さない ── 1 ページ 1 行増えるだけで、内容は毎回同じ。
QUIET_PATHS = ("/images/", "/static/")
QUIET_SUFFIXES = ("/state",)

SESSION_COOKIE = "aijudge_session"
# Google の認可コードフローの間だけ生きる短命 Cookie（#124・#125）。
# state・nonce の突き合わせにセッションを使わない ── まだ利用者が
# 誰かも決まっていない段階だから。
OIDC_STATE_COOKIE = "aijudge_oidc_state"


def _login_label(state) -> str:
    """ログインボタンの文言を引く（#209）。

    **失敗の画面でも同じ文言を出す。** ここで既定に落とすと、認証に失敗した
    ときだけボタンの名前が変わり、利用者には「別のログイン」に見える。
    """
    with state.database.unit_of_work() as uow:
        settings = uow.identity.get_oidc_settings(TenantId(DEFAULT_TENANT))
    return settings.login_label if settings else DEFAULT_LOGIN_LABEL


def _external_url(request: Request, path: str) -> str:
    """逆プロキシ越しでも正しい絶対 URL を作る（#125）。

    Google に渡す redirect_uri は、事前に登録した値と完全一致しなければ
    ならない。`X-Forwarded-*` はクライアントが決められる値だが、`Host` の
    検査（#116）と同じ前提 ── 学内などの届く先を絞った経路にこのアプリを
    置く運用なので、ここでも同じだけ信じる。
    """
    scheme = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    host = (
        (
            request.headers.get("x-forwarded-host")
            or request.headers.get("host")
            or request.url.netloc
        )
        .split(",")[0]
        .strip()
    )
    return f"{scheme}://{host}{path}"


# 提出できる拡張子は `aijudge_core.uploads` が持つ。**ここに表を作らない** ──
# 画面が受け付ける形式と教員が指定した形式がずれると、出せるのに採点が
# 種別を知らない提出が生まれる。
# 通常提出（コード・テキスト・PDF・画像）1 ファイルの上限。
# レポートの PDF/DOCX が数 MB になるので 20 MiB にしてある。これを超えるのは
# 事故か攻撃なので受け付ける前に止める。`AIJUDGE_MAX_UPLOAD_BYTES` で変えられる。
# **動画はこの経路では受けない**（`submit-video` + `MAX_VIDEO_BYTES`）。
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# 1 回の提出に入れられるファイル数と、合計の上限（1 ファイルの上限の何倍か）
# （#283）。認定証を何枚か出す課題のためで、束ねて出す形は想定しない。
MAX_FILES_PER_SUBMISSION = 10
MAX_TOTAL_UPLOAD_FACTOR = 4
# 動画 1 ファイルの上限（既定 5 GiB）。`AIJUDGE_MAX_VIDEO_BYTES` で変えられる。
MAX_VIDEO_BYTES = 5 * 1024 * 1024 * 1024
# **締切の無い課題だけは小さく絞る**（既定 256 MiB・ADR 0020）。
#
# あちらの動画は提出から 1 年残る（締切という共通の起点が無いので、回ごとに
# まとめて消せない）。砂場・自習用に数 GB を長く置く理由は無く、絞らないと
# 置き場所が先に尽きる ── 保存期間を延ばすのと引き換えに、受け付ける大きさを
# 下げて釣り合わせている。`AIJUDGE_MAX_VIDEO_BYTES_WITHOUT_DEADLINE` で変える。
MAX_VIDEO_BYTES_WITHOUT_DEADLINE = 256 * 1024 * 1024
# AI 評価 1 観点の目安秒数（RUNNING.md の実測 ≈ 17s を丸めた値）。待ち時間の
# 概算に使うだけ。約束はしない（画面側もレンジ表示にする）。
AVG_AI_SECONDS = 20
# web と同時に動かす AI ワーカー数のヒント（待ち時間の概算をワーカー数で割る）。
DEFAULT_AI_WORKERS = 1
# 1 プロセスで同時に受ける動画アップロードの上限。超過は即 429（クライアントが
# 順番待ち → 再試行）。**プロセス内ガード**で、`--workers` 間の全体制限は
# nginx `limit_conn` が持つ。SMR ディスクのスラッシング防止が目的。
DEFAULT_MAX_CONCURRENT_VIDEO = 4
# 429 のときに返す Retry-After（秒）。
VIDEO_RETRY_AFTER = 20
#: 画像を本文に書き起こす抽出器の名前（`input.transcription` に書かれる値）。
#: 提出画面が「画像は自動で文字に起こされる」と断るかどうかを、この名前の
#: 有無で決める。
IMAGE_TRANSCRIBER = "image_text"

#: 分割 1 つの大きさ（#119）。**サーバが決めてクライアントに渡す** ── 回線と
#: ディスクで妥当な値が違い、画面に書くと配備ごとに直せない。
#:
#: 8 MiB は「切れたときに捨てる量」と「往復の回数」の釣り合い。学内 Wi-Fi で
#: 3 GB なら 384 回の往復で、1 回失敗しても 8 MiB しか捨てない。
UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024
#: 受け取る分割の上限。**同時に何本も来たとき、載る量は本数ぶん積み上がる。**
UPLOAD_MAX_CHUNK_BYTES = 32 * 1024 * 1024


class StudentApp:
    """アプリの状態。DB とストアを持つ。"""

    def __init__(
        self,
        database: Database,
        artifact_store: ArtifactStore,
        *,
        profiles_dir: Path,
        video_store: StreamingArtifactStore | None = None,
        upload_sessions: FilesystemUploadSessions | None = None,
        max_upload_bytes: int = MAX_UPLOAD_BYTES,
        max_video_bytes: int = MAX_VIDEO_BYTES,
        max_video_bytes_without_deadline: int = MAX_VIDEO_BYTES_WITHOUT_DEADLINE,
        max_concurrent_video: int = DEFAULT_MAX_CONCURRENT_VIDEO,
        ai_workers: int = DEFAULT_AI_WORKERS,
        console_url: str = "",
        console_port: int = 8765,
    ) -> None:
        self.database = database
        self.store = artifact_store
        # 動画の置き場所。通常の提出物とは別ディスクに置ける（elite では
        # `/work/aijudge/video`）。未設定なら動画提出は 501 で断る。
        self.video_store = video_store
        # 中断から続けられるアップロードの受け皿（#119）。**動画ストアと同じ
        # 根の下**に置くこと ── 確定が `os.replace` で済む（別の場所だと、
        # いちばん混んでいる時間に 3 GB のコピーが増える）。
        self.upload_sessions = upload_sessions
        self.profiles_dir = profiles_dir
        # プロファイルごとの「画像を書き起こすか」。雛形はファイルなので
        # キャッシュしてよい（`GradingWorker._profile` と同じ判断）。
        self._transcribes_images: dict[str, bool] = {}
        self.max_upload_bytes = max_upload_bytes
        self.max_video_bytes = max_video_bytes
        self.max_video_bytes_without_deadline = max_video_bytes_without_deadline
        self.max_concurrent_video = max(1, max_concurrent_video)
        self.ai_workers = max(1, ai_workers)
        self.active_video_uploads = 0
        # 教員コンソールの場所（#103）。役割はコースごとに決まるので、同じ人が
        # 「A では学習者・B では TA」になる。**空なら、開いているホスト名の
        # ままポートだけ変えて渡す**（#114）── 決め打ちの名前へ渡すと、その
        # 名前で開いていない人の Cookie が付いていかない。
        self.console_url = console_url.rstrip("/")
        self.console_port = console_port
        self.submissions = SubmissionService(
            database.unit_of_work, artifact_store, stream_store=video_store
        )

    def transcribes_images(self, subject_profile: str) -> bool:
        """この科目は画像を本文に書き起こすか（`input.transcription`）。

        提出画面の断り書きに使うだけ。**読めなければ False** ── 断りが出ない
        だけで提出は止めない（採点側の誤りは採点側で出る）。コースの上書きは
        見ない: `input` は上書きできる項目に無い（`overrides.ALLOWED_KEYS`）。
        """
        if subject_profile not in self._transcribes_images:
            try:
                profile = load_profile(self.profiles_dir / f"{subject_profile}.yaml")
                answer = IMAGE_TRANSCRIBER in profile.input.transcription
            except Exception:
                answer = False
            self._transcribes_images[subject_profile] = answer
        return self._transcribes_images[subject_profile]

    def video_limit_for(self, task: Task) -> int:
        """この課題で受け付ける動画の大きさ（ADR 0020）。

        **締切の無い課題は小さい**。保存期間が 1 年と長く、しかも回ごとに
        まとめて消せないためで、上限と期間は一対で決まっている。
        """
        if task.due_at is None:
            return self.max_video_bytes_without_deadline
        return self.max_video_bytes

    @contextlib.contextmanager
    def video_slot(self) -> Iterator[None]:
        """同時アップロード数のプロセス内ガード。

        呼び出し側は先に `active_video_uploads >= max_concurrent_video` を
        見て 429 を返す（チェックと `+= 1` の間に `await` を挟まないので
        1 ループ内では競合しない）。ここは実際の増減だけ。
        """
        self.active_video_uploads += 1
        try:
            yield
        finally:
            self.active_video_uploads -= 1


def _state(request: Request) -> StudentApp:
    return request.app.state.aijudge  # type: ignore[no-any-return]


def current_principal(request: Request) -> Principal | None:
    """Cookie のセッションから主体を引く。無ければ None。"""
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    with _state(request).database.unit_of_work() as uow:
        return AuthService(uow.identity, audit=uow.audit).resolve(token)


def require_principal(request: Request) -> Principal:
    principal = current_principal(request)
    if principal is None:
        raise HTTPException(status_code=401, detail="ログインしてください")
    return principal


# 依存はモジュール階層に置く。`create_app` 内のローカル別名にすると、
# FastAPI が注釈を解決できず（`from __future__ import annotations` で
# 文字列になるため）、リクエストボディとして扱われて 422 になる。
Me = Annotated[Principal, Depends(require_principal)]


def create_app(app_state: StudentApp) -> FastAPI:
    app = FastAPI(title="aiJudge")
    app.state.aijudge = app_state

    # 共有の CSS（#184）。**セッションを要らない経路にする** ── ここの認証は
    # 経路ごと（`Depends(require_principal)`）なので何も免除は要らないが、
    # ログイン画面もフッタを持つ以上、誰も入っていない段階で CSS が必要になる。
    app.mount(webui.STATIC_MOUNT, StaticFiles(directory=webui.ASSETS_DIR), name="static")

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

    # アクセスログと相関 ID（ADR 0016）。**一番外側に置く** ── `add_middleware`
    # は後から足した方が外になるので、Host 検査より後に書く。弾かれた要求も
    # 記録に残す必要がある。
    #
    # これが無かったので、web の提出とワーカーの失敗を突き合わせられなかった
    # （#60 / #80、`docs/RUNNING.md`）。
    app.add_middleware(
        RequestContextMiddleware, quiet_paths=QUIET_PATHS, quiet_suffixes=QUIET_SUFFIXES
    )

    # -- ログイン ----------------------------------------------------------
    #
    # **既定の導線は Google（#121・#125）。** ローカルパスワードは
    # `/auth/local` の隠し経路にのみ残す（`admin` 専用、#127）。トップの
    # ログイン画面（このページ）からはリンクしない。

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, error: str = "") -> HTMLResponse:
        with app_state.database.unit_of_work() as uow:
            settings = uow.identity.get_oidc_settings(TenantId(DEFAULT_TENANT))
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {
                "error": error or None,
                "google_configured": settings is not None,
                # 機関ごとの呼び名（#209）。未設定なら既定が出る。
                "login_label": settings.login_label if settings else DEFAULT_LOGIN_LABEL,
            },
        )

    @app.get("/auth/login")
    def auth_login(request: Request) -> Response:
        """Google の認可エンドポイントへ渡す（#124・#125）。"""
        with app_state.database.unit_of_work() as uow:
            settings = uow.identity.get_oidc_settings(TenantId(DEFAULT_TENANT))
        if settings is None:
            # ボタンは未設定なら出していないはずだが、URL を直接叩かれても
            # 静かに戻すだけにする（エラー画面にするほどのことではない）。
            return RedirectResponse("/login", status_code=303)
        url, state, nonce = GoogleOidcProvider().authorization_url(
            settings, redirect_uri=_external_url(request, "/auth/callback")
        )
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(
            OIDC_STATE_COOKIE,
            f"{state}:{nonce}",
            max_age=600,
            **session_cookie_kwargs(forwarded_proto=request.headers.get("x-forwarded-proto")),
        )
        return response

    @app.get("/auth/callback", response_class=HTMLResponse)
    def auth_callback(
        request: Request, code: str = "", state: str = "", error: str = ""
    ) -> Response:
        # **文言はここで一度だけ引く**（#212）。`failed()` の中で引くと、
        # 呼び出し口の多くは既に unit_of_work を開いており、その内側で
        # もう 1 つ開くことになる ── SQLite は接続を 1 本しか持たない
        # （`StaticPool`）ので、内側を閉じた時点で外側の取引が終わる。
        # いまは失敗の経路に書き込みが無いので害は出ていないが、
        # 次に書き込みを足した人が踏む。
        label = _login_label(app_state)

        def failed(message: str) -> Response:
            response = TEMPLATES.TemplateResponse(
                request,
                "login.html",
                {
                    "error": message,
                    "google_configured": True,
                    "login_label": label,
                },
                status_code=401,
            )
            response.delete_cookie(OIDC_STATE_COOKIE, path="/")
            return response

        if error:
            return failed("Google 側でログインできませんでした。もう一度お試しください")

        expected_state, _, expected_nonce = request.cookies.get(OIDC_STATE_COOKIE, "").partition(
            ":"
        )
        if not code or not expected_state:
            return failed("ログインの状態を確認できませんでした")

        with app_state.database.unit_of_work() as uow:
            settings = uow.identity.get_oidc_settings(TenantId(DEFAULT_TENANT))
            if settings is None:
                return failed("Google ログインは設定されていません")
            try:
                identity = GoogleOidcProvider().exchange_code(
                    settings,
                    code=code,
                    redirect_uri=_external_url(request, "/auth/callback"),
                    expected_state=expected_state,
                    actual_state=state,
                    expected_nonce=expected_nonce,
                )
            except AuthenticationFailed as exc:
                return failed(str(exc))

            _, token = AuthService(
                uow.identity,
                audit=uow.audit,
                request_id=request_id_of(request),
                source_ip=source_ip_of(request),
            ).login_with_google(tenant_id=TenantId(DEFAULT_TENANT), identity=identity)
            uow.commit()

        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            **session_cookie_kwargs(forwarded_proto=request.headers.get("x-forwarded-proto")),
        )
        response.delete_cookie(OIDC_STATE_COOKIE, path="/")
        return response

    # -- ローカルパスワードログイン（隠し経路。#121・#125）-------------------
    #
    # `admin`（#127）専用の抜け道。URL を知っている人だけが辿り着く ──
    # トップのログイン画面（`/login`）からは意図的にリンクしない。

    @app.get("/auth/local", response_class=HTMLResponse)
    def local_login_form(request: Request) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, "login_local.html", {"error": None})

    @app.post("/auth/local")
    def local_login(
        request: Request,
        login: Annotated[str, Form()],
        password: Annotated[str, Form()],
        tenant: Annotated[str, Form()] = "",
    ) -> Response:
        with app_state.database.unit_of_work() as uow:
            service = AuthService(
                uow.identity,
                audit=uow.audit,
                request_id=request_id_of(request),
                source_ip=source_ip_of(request),
            )
            try:
                _, token = service.login(tenant_id=_tenant(tenant), login=login, password=password)
            except AuthenticationFailed as exc:
                # **失敗も commit する。** 監査行は操作と同じトランザクションに
                # 載っているので、ここで巻き戻すと失敗の記録ごと消える ──
                # そして失敗の記録こそ、総当たりに気づくための行である
                # （ADR 0016）。巻き戻すべき「操作」はここには無い。
                uow.commit()
                # 画面では理由を分けない。有効な ID の一覧を作れてしまう。
                return TEMPLATES.TemplateResponse(
                    request, "login_local.html", {"error": str(exc)}, status_code=401
                )
            uow.commit()

        response = RedirectResponse("/", status_code=303)
        # `Secure` を付けるかは配置で決まる（`aijudge_identity.cookies` 参照）。
        # リバースプロキシの `X-Forwarded-Proto` か
        # `AIJUDGE_SECURE_COOKIES=1` で決める。
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
            with app_state.database.unit_of_work() as uow:
                AuthService(
                    uow.identity,
                    audit=uow.audit,
                    request_id=request_id_of(request),
                    source_ip=source_ip_of(request),
                ).logout(token)
                uow.commit()
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    # -- コースと課題 ------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        principal = current_principal(request)
        if principal is None:
            return RedirectResponse("/login", status_code=303)
        with app_state.database.unit_of_work() as uow:
            auth = AuthService(uow.identity, audit=uow.audit)
            courses = auth.courses_for(principal.tenant_id, principal.user_id)
            # コースごとの役割（#103）。**学習者として取っているコースと、
            # 採点するコースを、同じ一覧の中で見分けられるようにする。**
            # `role_in` を使う（`find_enrollment` を直接見ない） ── テナント
            # 管理者はコース単位の Enrollment を持たないので、そちらでは
            # 常に None になり、一覧のどの行も「learner」に見えてしまう
            # （#128。管理者が採点画面へのリンクに気づけなくなる）。
            rows = [
                {"course": course, "role": auth.role_in(course.id, principal.user_id)}
                for course in courses
            ]
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {
                "me": principal,
                "courses": courses,
                "rows": rows,
                # コース一覧の上に教員コンソールへの入口を出すかどうか。
                # 行ごとの「採点の画面へ」（このコースの採点）とは別に、
                # コンソール自体の入口が要る ── 担当コースが多い教員ほど、
                # 一覧の途中の行まで探さないと入口に気づけなかった（#145 派生）。
                "has_grading_access": any(
                    row["role"] in (Role.ASSISTANT, Role.INSTRUCTOR, Role.ADMIN) for row in rows
                ),
                "console_url": counterpart_url(
                    request, configured=app_state.console_url, port=app_state.console_port
                ),
            },
        )

    @app.get("/courses/{course_id}/mastery", response_class=HTMLResponse)
    def my_mastery(request: Request, course_id: str, me: Me) -> HTMLResponse:
        """自分の知識要素ごとの習熟度（#335）。

        **教員が受講者 1 人を見る画面と同じものを描く**（`_mastery_rows.html`）
        ── 同じ問いに答える画面を 2 つ書くと、片方を直した日にもう片方だけが
        古くなる。違うのは「誰のものか」と、根拠に出せる範囲だけである。

        **これは推定値である。** 予測の妥当性（次の課題の正誤をどれだけ
        当てるか）はまだ測っていない ── 画面がそう言う（#327）。黙って数字を
        出すと、学習者は確定した評価として読む。
        """
        course_obj, _tasks = _course_and_tasks(app_state, me, CourseId(course_id))
        rows = my_mastery_rows(app_state, me, course_obj)
        return TEMPLATES.TemplateResponse(
            request,
            "mastery.html",
            {
                "me": me,
                "course": course_obj,
                "rows": rows,
                "evidence_of": True,
                **build_context(course_obj),
            },
        )

    @app.get("/courses/{course_id}", response_class=HTMLResponse)
    def course(request: Request, course_id: str, me: Me) -> HTMLResponse:
        course_obj, tasks = _course_and_tasks(app_state, me, CourseId(course_id))
        # 提出回数と採用される点を一覧に出す。出さないと、学習者は自分の
        # 到達点を知るのに課題を 1 つずつ開くことになる（`progress.py`）。
        with app_state.database.unit_of_work() as uow:
            progress = load_progress(
                uow,
                tenant_id=me.tenant_id,
                learner_id=me.user_id,
                course=course_obj,
                rows=tasks,
            )
        return TEMPLATES.TemplateResponse(
            request,
            "course.html",
            {
                "me": me,
                "course": course_obj,
                # 公開前の問題セットは学習者には出さない。**教員・TA には
                # 出す**（#340）── 出せるのに一覧に無いと、URL を直接
                # 叩くしかない。
                "sections": _group_by_unit(
                    tasks,
                    progress=progress,
                    preview=_previews_unopened_sets(_role_in(app_state, course_obj.id, me.user_id)),
                ),
                "progress": progress,
                "no_progress": EMPTY,
                **build_context(course_obj),
            },
        )

    @app.get("/tasks/{task_version_id}", response_class=HTMLResponse)
    def task(request: Request, task_version_id: str, me: Me) -> HTMLResponse:
        version, course_obj, task_obj = _task_and_course(
            app_state, me, TaskVersionId(task_version_id)
        )
        accepts = allowed_suffixes(task_obj.accepted_suffixes, course_obj.upload_suffixes)
        # 動画は取り込み経路が別（`submit-video`）。受付拡張子のうちストリーム
        # 対象のものだけ、専用のアップロード欄を出す。配備が動画に対応して
        # いなければ（`video_store` 未設定）出さない。
        video_accepts = (
            tuple(s for s in accepts if s in STREAMED_SUFFIXES)
            if app_state.video_store is not None
            else ()
        )
        plain_accepts = tuple(s for s in accepts if s not in STREAMED_SUFFIXES)
        # 画像・PDF の課題は複数ファイルを 1 つの提出にまとめられる（#283）。
        # コードを受ける課題は 1 ファイル（テスト実行は 1 つのソースを走らせる）。
        multi_file = bool(plain_accepts) and all(
            kind_for(suffix) is not ArtifactKind.CODE for suffix in plain_accepts
        )
        # 画像を受け、かつ科目が画像を書き起こすなら、そのことを断る。学習者
        # には「写真を出したのに文字で照合された」が見えないので、先に言う。
        image_transcribed = any(
            kind_for(suffix) is ArtifactKind.IMAGE for suffix in plain_accepts
        ) and app_state.transcribes_images(version.subject_profile)
        # 役割と受付の状態は**この 1 か所で求める**。提出欄を出すかどうかも、
        # 動作確認である旨の断りも、同じ 2 つの値から決まる（#108・#340）。
        role = _role_in(app_state, course_obj.id, me.user_id)
        window = task_obj.submission_window_at(now())
        with app_state.database.unit_of_work() as uow:
            # 一覧と個別画面で同じ規則の点・状態を出すため、
            # ここも `load_progress` を通す（`progress.py`）。
            progress = load_progress(
                uow,
                tenant_id=me.tenant_id,
                learner_id=me.user_id,
                course=course_obj,
                rows=((task_obj, version),),
            ).get(version.id, EMPTY)
        return TEMPLATES.TemplateResponse(
            request,
            "task.html",
            {
                "me": me,
                "task": version,
                "course": course_obj,
                "progress": progress,
                # 新しい提出を上に出す。直前に出したものを探させない。
                "attempts": tuple(reversed(progress.attempts)),
                "accepts": accepts,
                "plain_accepts": plain_accepts,
                "multi_file": multi_file,
                "image_transcribed": image_transcribed,
                "max_files": MAX_FILES_PER_SUBMISSION,
                "video_accepts": video_accepts,
                # **課題ごとの上限を出す**（ADR 0020）。締切の無い課題は小さい
                # ので、一律の値を書くと「上げられる」と読んだ学生が数 GB を
                # 送ってから 413 で断られることになる。
                "max_video_bytes": app_state.video_limit_for(task_obj),
                # 分割の大きさ（#119）。**再開のときはサーバに訊き直せない**
                # ので（受け皿を作り直さない）、画面が持っておく必要がある。
                "upload_chunk_bytes": UPLOAD_CHUNK_BYTES,
                # 提出開始を過ぎているか。**過ぎるまで受け付けない**
                # （`Task.accepts_submissions_at`）。ただし教員・TA は
                # 提出開始前でも出せる（#340）── 判定は受付と同じ述語で
                # 行う。画面と受付が違う答えを出すと、欄が出ているのに
                # 送ると断られる、が起きる。
                "open_for_submission": (
                    window is not SubmissionWindow.CLOSED
                    and (
                        window is not SubmissionWindow.NOT_OPEN
                        or may_submit_before_open(task_obj, role, now=now())
                    )
                ),
                # まだ学習者には出せない課題を開いているか（#340）。
                # **そう書く** ── 書かないと、教員は「もう公開されている」
                # と読む。
                "before_open": window is SubmissionWindow.NOT_OPEN,
                # 課題文は Markdown。生のまま出すと `##` や ``` が見える。
                "statement_html": render_statement(version.statement),
                # 教員・TA が自分のコースを開いているか（#108）。**出せる**が、
                # 出したものは成績にも統計にもならない。**先に言う** ── 言わずに
                # 除くと、教員は自分の提出が採点一覧に無いことを不具合として
                # 追いかけることになる。
                "submitting_as": role,
                # 学内限定の課題か、そしていまの接続元がそれを満たすか（#333）。
                # **先に言う。** 出そうとして断られてから知るのでは、試験の
                # 最中に場所を移すことになる。
                # この課題が問う知識要素（#327）。**事実だけ** ── 習熟度の
                # 推定値は出さない（妥当性が未測定・学習者は確定した評価と
                # して読む）。
                "knowledge_components": knowledge_components_of(app_state, version),
                "campus_only": task_obj.campus_only,
                "campus_access": (
                    campus_access_for(app_state, request, me.tenant_id)
                    if task_obj.campus_only
                    else None
                ),
                **build_context(course_obj, task_obj, version),
            },
        )

    # -- 提出 --------------------------------------------------------------

    @app.post("/tasks/{task_version_id}/submit")
    async def submit(
        request: Request,
        task_version_id: str,
        me: Me,
        upload: list[UploadFile],
    ) -> Response:
        """提出を受け付ける。**ファイルは複数でよい**（#283）。

        認定証を何枚も出す課題（修了した各レッスンの認定証）は、1 枚ずつ別の
        提出にすると 1 回しか採用されない。画像・PDF の課題は複数を 1 つの
        提出にまとめる。**コードの課題は 1 ファイル** ── テスト実行は 1 つの
        ソースを走らせるので、2 つ出されても何を走らせるか決められない。
        """
        version, course_obj, _task = _task_and_course(app_state, me, TaskVersionId(task_version_id))

        # **学内限定の課題は、学外から受け取らない**（#333）。画面にも出すが、
        # 断るのはここである ── 隠すのは表示の都合であって制限ではない。
        _require_campus(app_state, request, _task, me.tenant_id)

        # **受付の外では受け取らない。** 画面で隠すだけでは、URL を知って
        # いれば出せてしまう（隠すのは表示の都合であって制限ではない）。
        #
        # 断る理由を分ける（#73）。「まだ」と「もう」を同じ文言にすると、
        # 学習者は待てば出せるのか、間に合わなかったのかが分からない。
        #
        # **「まだ」の側は教員・TA に開ける**（#340・`may_submit_before_open`。
        # 秘匿の課題では TA を外す）。
        # 役割はここで 1 度だけ引き、下の `submitted_as` にも同じ値を渡す ──
        # 2 度引くと、許可した役割と記録する役割が食い違いうる。
        role = _role_in(app_state, course_obj.id, me.user_id)
        window = _task.submission_window_at(now())
        if window is SubmissionWindow.NOT_OPEN and not may_submit_before_open(
            _task, role, now=now()
        ):
            opens = _task.submissions_open_at or _task.opens_at
            raise HTTPException(
                status_code=409,
                detail=f"まだ提出できません（{webui.local_filter(opens)} から受け付けます）",
            )
        if window is SubmissionWindow.CLOSED:
            raise HTTPException(
                status_code=409,
                detail=(
                    "提出の受付は終了しました"
                    f"（{webui.local_filter(_task.accepts_until)} まででした）"
                ),
            )

        accepts = allowed_suffixes(_task.accepted_suffixes, course_obj.upload_suffixes)
        if not upload:
            raise HTTPException(status_code=400, detail="ファイルを選んでください")
        if len(upload) > MAX_FILES_PER_SUBMISSION:
            raise HTTPException(
                status_code=400,
                detail=f"1 回の提出で出せるのは {MAX_FILES_PER_SUBMISSION} ファイルまでです",
            )
        files: list[IncomingFile] = []
        seen_names: set[str] = set()
        total = 0
        for item in upload:
            payload = await item.read()
            filename = Path(item.filename or "submission").name
            suffix = Path(filename).suffix.lower()
            kind = kind_for(suffix) if suffix in accepts else None
            if kind is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"{filename}: この形式は提出できません（受付: {', '.join(accepts)}）",
                )
            if not payload:
                raise HTTPException(status_code=400, detail=f"{filename}: ファイルが空です")
            if len(payload) > app_state.max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"{filename}: ファイルが大きすぎます"
                        f"（上限 {app_state.max_upload_bytes} バイト）"
                    ),
                )
            total += len(payload)
            # 同じ名前が 2 つあると保存先が重なる。**黙って上書きしない** ──
            # 2 枚目に番号を付けて残す（順番は出した順のまま）。
            stem, dot, ext = filename.rpartition(".")
            base = filename
            counter = 2
            while filename in seen_names:
                filename = f"{stem}-{counter}.{ext}" if dot else f"{base}-{counter}"
                counter += 1
            seen_names.add(filename)
            files.append(IncomingFile(filename=filename, kind=kind, payload=payload))
        if total > app_state.max_upload_bytes * MAX_TOTAL_UPLOAD_FACTOR:
            raise HTTPException(
                status_code=413,
                detail=(
                    "合計が大きすぎます"
                    f"（上限 {app_state.max_upload_bytes * MAX_TOTAL_UPLOAD_FACTOR} バイト）"
                ),
            )
        if len(files) > 1 and any(item.kind is ArtifactKind.CODE for item in files):
            raise HTTPException(
                status_code=400,
                detail=(
                    "プログラムの課題は 1 回の提出に 1 ファイルです"
                    "（テスト実行は 1 つのソースを走らせます）"
                ),
            )

        try:
            result = app_state.submissions.accept(
                tenant_id=me.tenant_id,
                task_version_id=version.id,
                learner_id=me.user_id,
                subject_profile=version.subject_profile,
                files=files,
                # 試験の問題セットでは、採点はここでは走らせない（#67）。
                # テスト実行の結果は「どのケースで落ちたか」を含むので、
                # **試験中の学習者にとっては答えの一部**である。
                grading_starts_at=_task.grading_starts_at,
                # 出した人のそのときの役割（#108）。教員・TA の提出は採点まで
                # 通すが、成績にも測定にも数えない。**ここで焼き付ける** ──
                # 測定時に現在の受講から引くと、学生が TA になった瞬間に
                # 過去の提出が測定から消える（ADR 0013 と同じ罠）。
                submitted_as=role,
                is_demo=_is_demo_course(course_obj.id),
            )
        except SubmissionRejected as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return RedirectResponse(
            f"/submissions/{result.submission.id}" + ("?again=1" if result.deduplicated else ""),
            status_code=303,
        )

    def _video_gate(
        request: Request,
        me,
        task_version_id: str,
        filename: str,
        *,
        resumable: bool = True,
    ):
        """動画を受け付けてよいかを、**1 か所で**判定する（#119）。

        分割アップロードと 1 発の送信は**同じ関門を通る**こと ── 別々に書くと、
        片方でしか効かない制限が必ず生まれる（学内限定・受付期間・拡張子・
        上限のどれか 1 つが漏れれば、そちらの経路が抜け道になる）。

        **実際に漏れていた。** 1 発の送信（`submit_video`）はこの関門を通らず
        判定を写して持っていて、写すときに学内限定だけが落ちた ── 学内限定の
        動画課題に、学外から出せた。写しをやめてここを呼ぶ形に直してある。

        `resumable` が偽なら分割アップロードの置き場（`upload_sessions`）を
        要求しない。1 発の送信が要るのは動画の置き場だけである。
        """
        if app_state.video_store is None or (resumable and app_state.upload_sessions is None):
            raise HTTPException(status_code=501, detail="この配備は動画提出に対応していません")
        version, course_obj, task_obj = _task_and_course(
            app_state, me, TaskVersionId(task_version_id)
        )
        _require_campus(app_state, request, task_obj, me.tenant_id)
        role = _role_in(app_state, course_obj.id, me.user_id)
        window = task_obj.submission_window_at(now())
        if window is SubmissionWindow.NOT_OPEN and not may_submit_before_open(
            task_obj, role, now=now()
        ):
            opens = task_obj.submissions_open_at or task_obj.opens_at
            raise HTTPException(
                status_code=409,
                detail=f"まだ提出できません（{webui.local_filter(opens)} から受け付けます）",
            )
        if window is SubmissionWindow.CLOSED:
            raise HTTPException(
                status_code=409,
                detail=(
                    "提出の受付は終了しました"
                    f"（{webui.local_filter(task_obj.accepts_until)} まででした）"
                ),
            )
        accepts = allowed_suffixes(task_obj.accepted_suffixes, course_obj.upload_suffixes)
        name = Path(filename or "video").name
        suffix = Path(name).suffix.lower()
        kind = kind_for(suffix) if suffix in accepts else None
        if kind is None:
            raise HTTPException(
                status_code=400,
                detail=f"この形式は提出できません（受付: {', '.join(accepts)}）",
            )
        if suffix not in STREAMED_SUFFIXES:
            raise HTTPException(
                status_code=400, detail="この形式は通常の提出（/submit）で送ってください"
            )
        return version, course_obj, task_obj, role, kind, name

    @app.post("/tasks/{task_version_id}/uploads")
    def create_upload(request: Request, task_version_id: str, me: Me, filename: str) -> Response:
        """分割アップロードを始める（#119）。**受け皿を 1 つ作るだけ。**

        数 GB を 1 回の PUT で送ると、90% で切れたときに全部やり直しになる。
        学内 Wi-Fi 越しの試験提出では、それが「間に合わなかった」になる。
        """
        version, _course, task_obj, _role, _kind, name = _video_gate(
            request, me, task_version_id, filename
        )
        sessions = app_state.upload_sessions
        assert sessions is not None  # `_video_gate` が確かめている
        # **掃除はここで 1 回**。専用のタイマーを足すと、その停止に誰も
        # 気づかない（`FilesystemUploadSessions.sweep` の説明を参照）。
        sessions.sweep()
        session = sessions.create(
            upload_id=new_id("upl"),
            tenant_id=str(me.tenant_id),
            learner_id=str(me.user_id),
            task_version_id=str(version.id),
            filename=name,
            max_bytes=app_state.video_limit_for(task_obj),
        )
        return JSONResponse(
            {
                "upload_id": session.id,
                "offset": 0,
                "max_bytes": session.max_bytes,
                # 分割の大きさはサーバが決める。**画面に書かない** ──
                # 配備ごとの回線とディスクで妥当な値が違う。
                "chunk_size": UPLOAD_CHUNK_BYTES,
            }
        )

    def _own_session(upload_id: str, me) -> object:
        sessions = app_state.upload_sessions
        if sessions is None:
            raise HTTPException(status_code=501, detail="この配備は動画提出に対応していません")
        try:
            session = sessions.get(upload_id)
        except UploadSessionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not session.belongs_to(tenant_id=str(me.tenant_id), learner_id=str(me.user_id)):
            # **他人のものは「無い」と答える。** 在ることを知らせると、
            # 誰かがアップロード中かどうかが漏れる（受講の有無と同じ扱い）。
            raise HTTPException(status_code=404, detail="このアップロードは見つかりません")
        return session

    @app.get("/uploads/{upload_id}")
    def upload_status(upload_id: str, me: Me) -> Response:
        """いま何バイト届いているか。**再開はここから始まる。**"""
        session = _own_session(upload_id, me)
        return JSONResponse({"upload_id": upload_id, "offset": session.offset})

    @app.patch("/uploads/{upload_id}")
    async def append_upload(request: Request, upload_id: str, me: Me, offset: int) -> Response:
        """続きを書く。本文は生バイト列（multipart で包まない）。

        **1 チャンクぶんだけメモリに載る。** それが分割の目的でもある
        （全体を載せない・R7）。大きすぎるチャンクは断る ── 同時に何本も
        来たときに、載る量が本数ぶん積み上がる。
        """
        session = _own_session(upload_id, me)
        sessions = app_state.upload_sessions
        assert sessions is not None
        if app_state.active_video_uploads >= app_state.max_concurrent_video:
            raise HTTPException(
                status_code=429,
                detail="いま混み合っています。しばらくして自動で再試行します。",
                headers={"Retry-After": str(VIDEO_RETRY_AFTER)},
            )
        payload = await request.body()
        if len(payload) > UPLOAD_MAX_CHUNK_BYTES:
            raise HTTPException(status_code=413, detail="分割が大きすぎます")
        with app_state.video_slot():
            try:
                written = await run_in_threadpool(
                    sessions.append, upload_id, [payload], offset=offset
                )
            except OffsetMismatch as exc:
                # **サーバの位置を返す。** クライアントはそこから続ければよく、
                # やり直す必要は無い（それがこの仕組みの目的である）。
                return JSONResponse(
                    {"offset": exc.offset, "detail": "位置がずれています"}, status_code=409
                )
            except TooLarge as exc:
                raise HTTPException(status_code=413, detail=str(exc)) from exc
            except UploadSessionError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
        del session
        return JSONResponse({"upload_id": upload_id, "offset": written})

    @app.post("/uploads/{upload_id}/finish")
    async def finish_upload(request: Request, upload_id: str, me: Me) -> Response:
        """受け皿を提出に変える。

        **受付の判定をここでもう一度行う。** 始めたときに開いていても、
        送っている間に受付が終わることがある ── 判定は「提出が成立した
        時刻」で行うべきで、それはこの瞬間である。
        """
        session = _own_session(upload_id, me)
        sessions = app_state.upload_sessions
        store = app_state.video_store
        assert sessions is not None and store is not None
        version, course_obj, task_obj, role, kind, name = _video_gate(
            request, me, session.task_version_id, session.filename
        )
        submission_id = SubmissionId(new_id("sub"))
        artifact_id = ArtifactId(new_id("art"))
        storage_key = artifact_storage_key(me.tenant_id, submission_id, artifact_id, name)
        try:
            source = sessions.path_of(upload_id)
        except UploadSessionError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if source.stat().st_size == 0:
            raise HTTPException(status_code=400, detail="ファイルが空です")
        # **移すだけ**（同じファイルシステム）。ハッシュはここで 1 回計算する。
        blob = await run_in_threadpool(store.adopt, storage_key, source)
        try:
            result = app_state.submissions.record_streamed(
                tenant_id=me.tenant_id,
                task_version_id=version.id,
                learner_id=me.user_id,
                subject_profile=version.subject_profile,
                filename=name,
                kind=kind,
                submission_id=submission_id,
                artifact_id=artifact_id,
                storage_key=storage_key,
                byte_size=blob.byte_size,
                sha256=blob.sha256,
                # **受け皿の id をそのまま使う。** 確定を 2 回押しても、
                # 提出は 1 つである（`Idempotency-Key` と同じ役目）。
                idempotency_key=upload_id,
                grading_starts_at=task_obj.grading_starts_at,
                submitted_as=role,
                is_demo=_is_demo_course(course_obj.id),
            )
        except SubmissionRejected as exc:
            store.delete(storage_key)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except BaseException:
            store.delete(storage_key)
            raise
        sessions.discard(upload_id)
        return JSONResponse(
            {
                "submission_id": str(result.submission.id),
                "location": f"/submissions/{result.submission.id}",
            }
        )

    @app.post("/tasks/{task_version_id}/submit-video")
    async def submit_video(
        request: Request,
        task_version_id: str,
        me: Me,
        filename: str,
    ) -> Response:
        """動画を **メモリに載せず** 受け付ける専用ルート（`docs/design` R7）。

        本文は `application/octet-stream` の生バイト列（multipart で包まない）。
        ファイル名はクエリ `?filename=` で渡す。`Idempotency-Key` ヘッダを
        送れば、3 GB の再送を本文を読む前に弾ける。

        **動画課題はコード課題と別の課題にすること** ── 観点は
        `__human__`（`HUMAN_SCORED`）で宣言し、教員が視聴して段階を入れる。
        """
        # 学内限定・受付期間・拡張子は分割アップロードと同じ関門で判定する
        # （`_video_gate` の docstring。ここで写して持っていたときに学内限定が漏れた）。
        version, course_obj, _task, role, kind, name = _video_gate(
            request, me, task_version_id, filename, resumable=False
        )

        idem = request.headers.get("Idempotency-Key")
        # **本文を読む前に** 再送を弾く（3 GB を無駄に受けない）。
        if idem is not None:
            hit = app_state.submissions.peek_idempotent(
                tenant_id=me.tenant_id,
                idempotency_key=idem,
                subject_profile=version.subject_profile,
                grading_starts_at=_task.grading_starts_at,
            )
            if hit is not None:
                return RedirectResponse(
                    f"/submissions/{hit.submission.id}?again=1", status_code=303
                )

        # **同時アップロード数のプロセス内ガード**（SMR ディスクのスラッシング防止）。
        # 超過は即 429 ── クライアントが順番待ち表示のうえ再試行する。
        # チェックと slot 取得の間に await を挟まないので 1 ループ内では競合しない。
        if app_state.active_video_uploads >= app_state.max_concurrent_video:
            raise HTTPException(
                status_code=429,
                detail="いま混み合っています。しばらくして自動で再試行します。",
                headers={"Retry-After": str(VIDEO_RETRY_AFTER)},
            )

        # ストリームをストアへ流す。**書き込み・fsync はブロッキング**なので
        # スレッドプールへ逃がす ── 同時アップロードが多いとき、1 本の fsync
        # （SMR HDD で数 GB flush = 数秒）でイベントループ全体が止まるのを防ぐ。
        # 上限は課題で決まる（締切の無い課題は小さい・ADR 0020）。
        max_bytes = app_state.video_limit_for(_task)
        submission_id = SubmissionId(new_id("sub"))
        artifact_id = ArtifactId(new_id("art"))
        storage_key = artifact_storage_key(me.tenant_id, submission_id, artifact_id, name)
        handle = app_state.video_store.writer(storage_key)
        with app_state.video_slot():
            try:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    if handle.size + len(chunk) > max_bytes:
                        await run_in_threadpool(handle.abort)
                        raise HTTPException(
                            status_code=413,
                            detail=f"ファイルが大きすぎます（上限 {max_bytes} バイト）",
                        )
                    await run_in_threadpool(handle.write, chunk)
                blob = await run_in_threadpool(handle.commit)
            except HTTPException:
                raise
            except BaseException:
                await run_in_threadpool(handle.abort)
                raise

        try:
            result = app_state.submissions.record_streamed(
                tenant_id=me.tenant_id,
                task_version_id=version.id,
                learner_id=me.user_id,
                subject_profile=version.subject_profile,
                filename=name,
                kind=kind,
                submission_id=submission_id,
                artifact_id=artifact_id,
                storage_key=storage_key,
                byte_size=blob.byte_size,
                sha256=blob.sha256,
                idempotency_key=idem,
                grading_starts_at=_task.grading_starts_at,
                submitted_as=role,
                is_demo=_is_demo_course(course_obj.id),
            )
        except SubmissionRejected as exc:
            app_state.video_store.delete(storage_key)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except BaseException:
            app_state.video_store.delete(storage_key)
            raise

        return RedirectResponse(
            f"/submissions/{result.submission.id}" + ("?again=1" if result.deduplicated else ""),
            status_code=303,
        )

    @app.get("/images/{course_id}/{name}")
    def statement_image(course_id: str, name: str, me: Me) -> Response:
        """課題文に貼られた画像を返す。**受講者だけ。**

        課題文そのものが受講者にしか出ないので、そこに貼られた画像も同じ
        範囲で足りる。教員コンソールにも同じ経路がある ── 課題文は両方の
        画面に出るので、絶対 URL を埋め込むとどちらかのホスト名が課題文に
        焼き付く（`aijudge_authoring.images`）。
        """
        with app_state.database.unit_of_work() as uow:
            auth = AuthService(uow.identity, audit=uow.audit)
            try:
                auth.require_membership(CourseId(course_id), me.user_id)
            except PermissionDenied:
                # 存在と権限を区別しない（コース ID の存在自体を漏らさない）。
                raise HTTPException(status_code=404, detail="画像が見つかりません") from None
        try:
            payload = app_state.store.get(images.storage_key(course_id, name))
        except (images.ImageError, Exception) as exc:
            raise HTTPException(status_code=404, detail="画像が見つかりません") from exc
        return Response(
            content=payload,
            media_type=images.content_type(name),
            # 中身から名前を導いているので、同じ URL の中身は変わらない。
            headers={"Cache-Control": "private, max-age=86400"},
        )

    # -- 結果 --------------------------------------------------------------

    @app.get("/submissions/{submission_id}", response_class=HTMLResponse)
    def submission(request: Request, submission_id: str, me: Me, again: int = 0) -> HTMLResponse:
        loaded = _submission_view(app_state, me, SubmissionId(submission_id))
        source = _source_of(app_state, loaded.submission)
        return TEMPLATES.TemplateResponse(
            request,
            "submission.html",
            {
                "me": me,
                "submission": loaded.submission,
                "task": loaded.version,
                "run": loaded.run,
                "view": loaded.view,
                "lines": _numbered(source),
                # 提出物そのもの（#75）。**種別で描き分ける** ── 以前は
                # 何でもテキストとして出しており、画像は化けたバイナリに
                # なっていた。
                "files": _submitted_files(loaded.submission),
                "duplicate": bool(again),
                "awaiting_deterministic": loaded.awaiting_deterministic,
                "awaiting_ai": loaded.awaiting_ai,
                # 試験の問題セットか（#67）。**時刻は出さない** ── 教員は
                # 途中で採点を流せるし、試験は延長されうるので、時刻を
                # 約束すると両方向に食い違う。
                "grading_held": loaded.grading_held,
                "grading_in_progress": loaded.grading_in_progress,
                "min_reason": MIN_JUSTIFICATION_LENGTH,
                # この提出が関わる知識要素（#327）。**課題の宣言をそのまま
                # 出す** ── 「この提出で何点だったか」ではなく「何を問われた
                # 提出か」である。習熟度の推定値は出さない。
                "knowledge_components": knowledge_components_of(app_state, loaded.version),
                **build_context(loaded.course, loaded.task, loaded.version, loaded.submission),
            },
        )

    @app.get("/submissions/{submission_id}/state")
    def submission_state(submission_id: str, me: Me) -> JSONResponse:
        """採点が動いているかだけを返す。**画面の代わりではない。**

        結果そのものは返さない ── 返すと、点を出す判断（保留・遅延減点・
        確定の出所）が画面とこことで二重になり、片方だけ直る日が来る
        （`visibility.py` が判断を 1 か所に集めている理由と同じ）。
        ここが答えるのは「まだ動いているか」だけで、変わったら画面を
        取り直させる。

        `no-store` を付ける。中間のキャッシュに拾われると、終わったのに
        「動いています」を返し続ける。
        """
        loaded = _submission_view(app_state, me, SubmissionId(submission_id))
        body: dict[str, object] = {
            # **試験中は「動いている」と言わない。** 言うと画面が
            # 問い合わせ続ける（#67）。
            "working": loaded.grading_in_progress and not loaded.grading_held,
            "phase": (
                "deterministic"
                if loaded.awaiting_deterministic
                else "ai"
                if loaded.awaiting_ai
                else None
            ),
            "graded": loaded.run is not None,
        }
        # AI 段階の待ち状況。**試験中は出さない**（順位も時刻も約束しない・#67）。
        if loaded.awaiting_ai and not loaded.grading_held:
            with app_state.database.unit_of_work() as uow:
                position = uow.jobs.position_in_queue(
                    SubmissionId(submission_id), GradingPhase.AI, now()
                )
                depth = uow.jobs.pending_count(phase=GradingPhase.AI)
            if position is not None:
                body["queue_position"] = position
                body["queue_depth"] = depth
                # ざっくりの上限。ワーカー数で割る（`AIJUDGE_AI_WORKERS`、既定 1）。
                # 正確さより「数分か・十数分か」が分かればよい。
                #
                # **これは本数の 2 つ目の写しである。** 実際の本数は systemd が
                # 決めており（`aijudge.target`）、ここからは見えない。ずれると
                # 目安が本数の比だけ狂うので、`aijudge-config-check` が両方を
                # 突き合わせて、違っていたら知らせる（#260）。
                batches = position // max(1, app_state.ai_workers) + 1
                body["eta_seconds"] = batches * AVG_AI_SECONDS
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @app.get("/submissions/{submission_id}/artifacts/{artifact_id}")
    def submitted_file(request: Request, submission_id: str, artifact_id: str, me: Me) -> Response:
        """提出したファイルそのものを返す（#75）。

        **見せてよいのは本人だけ。** `_submission_view` が既にその判定を
        持っている（他人の提出は 404 にして、存在自体を漏らさない）ので、
        そこを通す。課題文の画像（#64）と形は近いが、**見せる相手が違う。**

        動画は別ストアから **Range 対応でストリーム配信**する
        （`<video>` のシークに 206 が要る。全体をメモリに読まない）。
        それ以外は拡張子から `Content-Type` を引き、分からなければ
        `application/octet-stream` で返し、ブラウザに解釈させない。
        """
        loaded = _submission_view(app_state, me, SubmissionId(submission_id))
        artifact = next((a for a in loaded.submission.artifacts if str(a.id) == artifact_id), None)
        if artifact is None:
            raise HTTPException(status_code=404, detail="提出物が見つかりません")
        if artifact.is_purged:
            # **404 にしない。** 消去は運用の結果であって不具合ではない
            # （ADR 0020）。同じ顔で出すと、学習者は区別できず問い合わせ先も
            # 違う。410 は「あったが、もう無い」である。
            raise HTTPException(status_code=410, detail=PURGED_MESSAGE)
        if artifact.kind is ArtifactKind.VIDEO:
            return _serve_video(app_state, request, artifact, artifact_id)
        try:
            payload = app_state.store.get(artifact.storage_key)
        except Exception as exc:
            raise HTTPException(status_code=404, detail="提出物が見つかりません") from exc
        return Response(
            content=payload,
            media_type=content_type_for(artifact.filename),
            headers={
                "Cache-Control": "private, max-age=300",
                # **画面に埋め込むのは画像と PDF だけ。** それ以外を
                # インラインにすると、ブラウザが中身を解釈しうる。
                # 非 ASCII の名前（macOS の日本語スクリーンショット）は
                # そのままヘッダに入らない ── `content_disposition` が畳む。
                "Content-Disposition": content_disposition(
                    "inline" if artifact.kind in _INLINE_KINDS else "attachment",
                    artifact.filename,
                    artifact_id,
                ),
                # 提出物は学習者が出したファイルである。
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/submissions/{submission_id}/request-review")
    def request_review(
        submission_id: str,
        me: Me,
        reason: Annotated[str, Form()] = "",
    ) -> Response:
        """再確認を依頼する。**根拠説明が必須。**

        項目が空でも欠けていても、同じ 400 と同じ案内を返す。422 を返すと
        学習者には何をすればよいか分からない。

        「納得できない」だけの依頼を受け付けると、教員は何を確認すべきか
        分からないまま全件を見ることになり、導線が機能しなくなる。
        """
        loaded = _submission_view(app_state, me, SubmissionId(submission_id))
        if loaded.run is None:
            raise HTTPException(status_code=409, detail="まだ採点されていません")
        if loaded.view is not None and not loaded.view.can_request_review:
            raise HTTPException(
                status_code=409,
                detail=loaded.view.request_reason or "この採点には依頼を出せません",
            )

        text = reason.strip()
        if len(text) < MIN_JUSTIFICATION_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"どの観点のどこが違うと考えるかを {MIN_JUSTIFICATION_LENGTH} "
                    "文字以上で書いてください"
                ),
            )

        with app_state.database.unit_of_work() as uow:
            try:
                uow.reviews.save_request(
                    ReviewRequest(
                        id=ReviewRequestId(new_id("rrq")),
                        submission_id=loaded.submission.id,
                        grading_run_id=loaded.run.id,
                        learner_id=me.user_id,
                        reason=text,
                        requested_at=datetime.now(UTC),
                    )
                )
            except Exception as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            uow.commit()
        return RedirectResponse(f"/submissions/{submission_id}", status_code=303)

    return app


class SetState(StrEnum):
    """学習者から見た問題セットの段階。**分けて並べる。**

    課題が数十件になると、平らな一覧では「いま出せるのはどれか」が
    読み取れない。学習者が最初に知りたいのはそれである。
    """

    # 提出開始まで待つ。課題文は読める。
    ANNOUNCED = "announced"
    # いま出せる。減点なし。
    OPEN = "open"
    # 締切を過ぎたが、まだ出せる。**出したぶんは減点される**（ADR 0013）。
    # 締切と受付終了のあいだがこれで、**この区間があるから締切で閉じない**。
    LATE = "late"
    # 受付終了。もう出せない。
    CLOSED = "closed"


# 教員・TA の一覧では、この見出しの下に**まだ公開していないセットも並ぶ**
# （#340）。「公開された問題セット」のままでは、公開前のものをそう呼ぶことに
# なる。並ぶのが提出開始前である点は同じなので、見出しはそこだけを言う。
PREVIEW_ANNOUNCED_LABEL = "提出開始前の問題セット（公開前のものを含む）"

SET_LABELS: dict[SetState, str] = {
    SetState.OPEN: "提出できる問題セット",
    SetState.LATE: "締切を過ぎた問題セット（減点して提出できます）",
    SetState.ANNOUNCED: "公開された問題セット（提出開始前）",
    SetState.CLOSED: "受付を終了した問題セット",
}


def _group_by_unit(
    rows: tuple,
    *,
    progress: dict | None = None,
    now: datetime | None = None,
    preview: bool = False,
) -> list[dict[str, object]]:
    """課題を問題セットでまとめ、段階ごとに分けて新しい順に並べる。

    1 回の授業で複数問出るので、平らに並べると何回目の分か分からなくなる。
    さらに学期が進むと数十件になるので、**段階で分けたうえで新しい順**に
    出す ── 学習者が最初に知りたいのは「いま出せるのはどれか」である。

    **公開前の問題セットは学習者には出さない。** 公開日時を持たせておいて何も
    起きないなら、その日付は嘘になる。日程を入れていない課題（`opens_at`
    が空）は今までどおり出る。

    `preview` は教員・TA のとき真（#340）。**公開前のセットも出す** ──
    公開前でも出せる相手なのに一覧に無いと、URL を直接叩くしかない。
    出したセットには `before_open` が立ち、画面が「公開前」と明示する。
    """
    moment = now or datetime.now(UTC)
    groups: dict[tuple, dict[str, object]] = {}
    for task, version in sorted(rows, key=lambda row: row[0].sort_key):
        if task.opens_at and moment < task.opens_at and not preview:
            continue
        key = (task.session, task.unit)
        group = groups.setdefault(
            key,
            {
                "label": task.unit_label,
                "unit": task.unit,
                "session": task.session,
                "opens_at": task.opens_at,
                "submissions_open_at": task.submissions_open_at,
                "due_at": task.due_at,
                "accepts_until": task.accepts_until,
                "tasks": [],
            },
        )
        group["tasks"].append((task, version))
        # まとまりの日程は、その中で最も早い提示・最も遅い締切を代表にする。
        if task.opens_at and (group["opens_at"] is None or task.opens_at < group["opens_at"]):
            group["opens_at"] = task.opens_at
        if task.submissions_open_at and (
            group["submissions_open_at"] is None
            or task.submissions_open_at < group["submissions_open_at"]
        ):
            group["submissions_open_at"] = task.submissions_open_at
        if task.due_at and (group["due_at"] is None or task.due_at > group["due_at"]):
            group["due_at"] = task.due_at
        # 受付終了は**最も遅いもの**を代表にする。早い側を採ると、まだ
        # 出せる課題があるセットを「受付終了」と書くことになる。
        if task.accepts_until and (
            group["accepts_until"] is None or task.accepts_until > group["accepts_until"]
        ):
            group["accepts_until"] = task.accepts_until

    for group in groups.values():
        group["state"] = _set_state(group, moment)
        # まだ学習者に出ていないセットか（#340）。`preview` のときだけ
        # 真になりうる ── 学習者の一覧には、そもそも並んでいない。
        group["before_open"] = bool(group["opens_at"] and moment < group["opens_at"])
        # **残り秒数はサーバが数える**（#73）。画面が締切と自分の時計を
        # 比べると、時計のずれがそのまま表示のずれになる。締切前は正、
        # 過ぎていれば負（＝経過時間）。
        # 畳んだ見出しで判断できるだけの情報（#93）。**中が見えなくなるので、
        # 開かずに「自分がやることが残っているか」が分かる必要がある。**
        group["task_count"] = len(group["tasks"])
        # **畳んだ見出しで「やることが残っているか」が分かる必要がある。**
        # 中が見えなくなるので、開かないと未提出に気づけないのでは畳む
        # 意味が無い。採点中も出す ── 畳んだ中で採点が進むと、届いたことに
        # 気づけない（#63 の自動更新が効くのは開いている画面だけ）。
        marks = [(progress or {}).get(version.id) for _task, version in group["tasks"]]
        group["unsubmitted"] = sum(1 for m in marks if m is None or not m.count)
        group["grading"] = sum(1 for m in marks if m is not None and m.grading)
        group["seconds_to_due"] = (
            None if group["due_at"] is None else int((group["due_at"] - moment).total_seconds())
        )
        # 並べ替えの基準になる日付。**その段階で意味のある日付を使う** ──
        # 締め切られたセットは締切、これからのセットは提出開始・公開。
        group["sort_at"] = (
            group["due_at"]
            if group["state"] in (SetState.LATE, SetState.CLOSED)
            else group["submissions_open_at"] or group["opens_at"] or group["due_at"]
        )

    ordered: list[dict[str, object]] = []
    # **出せるものが先。** 学習者が最初に知りたいのは「いま出せるのはどれか」で、
    # 締切を過ぎてもまだ出せるものはその次に近い（#73）。
    for state in (SetState.OPEN, SetState.LATE, SetState.ANNOUNCED, SetState.CLOSED):
        members = [group for group in groups.values() if group["state"] is state]
        # 新しい日付順。日付の無いセットは後ろに置く（並べる根拠が無い）。
        members.sort(key=lambda g: (g["sort_at"] is None, g["sort_at"] or MIN_TIME), reverse=True)
        label = (
            PREVIEW_ANNOUNCED_LABEL
            if preview and state is SetState.ANNOUNCED
            else SET_LABELS[state]
        )
        ordered.append({"state": state, "label": label, "groups": members})
    return ordered


MIN_TIME = datetime.min.replace(tzinfo=UTC)


def _set_state(group: dict[str, object], now: datetime) -> SetState:
    """**判定は `Task.submission_window_at` と同じ順序で行う。**

    ここと課題ページで違う答えを出すと、一覧では「提出できる」なのに開くと
    出せない、が起きる。
    """
    opens = group["submissions_open_at"] or group["opens_at"]
    if opens is not None and now < opens:
        return SetState.ANNOUNCED
    accepts_until = group["accepts_until"]
    if accepts_until is not None and now > accepts_until:
        return SetState.CLOSED
    due_at = group["due_at"]
    if due_at is not None and now >= due_at:
        return SetState.LATE
    return SetState.OPEN


# -- 権限つきの読み出し ------------------------------------------------------
#
# URL を推測されても他人のものが見えないこと。UI で隠すのは表示の都合であって
# 権限ではないので、リクエストごとに受講と所有者を確かめる。


def _tenant(raw: str):
    from aijudge_core.ids import TenantId

    # 単独運用では 1 テナント。Phase 8 でホスト名やパスから決める。
    return TenantId(raw or DEFAULT_TENANT)


DEFAULT_TENANT = "ten_" + "0" * 32


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


def my_mastery_rows(app_state: StudentApp, me, course) -> tuple:
    """この学習者の、このコースが使う知識要素ぶんの習熟度（#335）。

    **根拠はこのコースの提出だけを開く。** 習熟度はコースをまたいで積み
    上がるので、1 つの値に他の科目の観測も入っている ── 他の科目の課題名を
    ここに出すのは、その科目の成績に近い情報を出すことになる。教員の画面と
    同じ規則である（`mastery.split_evidence`）。
    """
    scope = set(course.knowledge_components)
    with app_state.database.unit_of_work() as uow:
        labels = {}
        for kc_id in {state.kc_id for state in uow.skills.list_states(me.tenant_id, me.user_id)}:
            kc = uow.skills.get_kc(kc_id)
            if kc is not None and kc.key in scope:
                labels[str(kc_id)] = (kc.key, kc.label)
        states = [
            state
            for state in uow.skills.list_states(me.tenant_id, me.user_id)
            if str(state.kc_id) in labels
        ]
        course_of, title_of = _evidence_origins(uow, states)

    rows = [
        split_evidence(
            state,
            course_of=course_of,
            title_of=title_of,
            course_id=str(course.id),
            key=labels[str(state.kc_id)][0],
            label=labels[str(state.kc_id)][1],
        )
        for state in states
    ]
    rows.sort(key=lambda row: row.label)
    return tuple(rows)


def _evidence_origins(uow, states) -> tuple[dict, dict]:
    """根拠の採点がどのコースの、どの課題から来たかを解決する（#335）。

    **同じ版を二度引かない** ── 根拠は 1 KC あたり最大 20 件あり、同じ課題
    から来ることが多い。
    """
    course_of: dict[str, str | None] = {}
    title_of: dict[str, str | None] = {}
    seen: dict[str, tuple[str | None, str | None]] = {}
    for state in states:
        for item in state.evidence:
            run_id = str(item.grading_run_id)
            if run_id in course_of:
                continue
            run = uow.runs.get(item.grading_run_id)
            if run is None:
                course_of[run_id] = title_of[run_id] = None
                continue
            version_id = str(run.context.task_version_id)
            if version_id not in seen:
                version = uow.tasks.get_version(run.context.task_version_id)
                task = None if version is None else uow.tasks.get_task(version.task_id)
                seen[version_id] = (
                    None if task is None else str(task.course_id),
                    None if task is None else task.title,
                )
            course_of[run_id], title_of[run_id] = seen[version_id]
    return course_of, title_of


def knowledge_components_of(app_state: StudentApp, version) -> tuple[tuple[str, str], ...]:
    """この課題が問う知識要素（#327）。`(キー, 表示名)` の並び。

    **これは事実であって推定ではない。** 出しているのは Q-matrix そのもの
    ── 課題に付けた宣言で、採点にも作問にも使われている同じ値である。
    習熟度（推定）は出さない：予測の妥当性がまだ測れておらず、学習者は
    数字を確定した評価として読む（#327 の段階分け）。

    **語彙に無いものは出さない。** 引けなかった KC を鍵のまま出すと、
    学習者には意味の無い文字列が並ぶ。

    表示名で並べる ── 課題ごとに順序が変わると、読み比べられない。
    """
    if not version.q_matrix:
        return ()
    found = []
    with app_state.database.unit_of_work() as uow:
        for entry in version.q_matrix:
            kc = uow.skills.get_kc(entry.kc_id)
            if kc is not None:
                found.append((kc.key, kc.label))
    return tuple(sorted(found, key=lambda pair: pair[1]))


def campus_access_for(app_state: StudentApp, request: Request, tenant_id) -> CampusAccess:
    """この要求が学内から来ているか（#333）。

    **接続元は `X-Forwarded-For` の右端**（逆プロキシが書いた値）を採る
    （`aijudge_telemetry.client_ip`）── 左端はクライアントが自由に書けるので、
    そこで判定すると名乗るだけで通れる。
    """
    with app_state.database.unit_of_work() as uow:
        settings = uow.identity.get_campus_networks(tenant_id)
    return campus_access(source_ip_of(request), () if settings is None else settings.cidrs)


def _require_campus(app_state: StudentApp, request: Request, task, tenant_id) -> None:
    """学内限定の課題を、学外から出させない（#333）。

    **断る理由を分ける**（受付の窓と同じ作法・#73）。「学外から」と「判定
    できない」は学習者にとって意味が違う ── 前者は場所を移せば出せるが、
    後者は移しても直らない（設定か経路の問題で、教員に言うしかない）。
    """
    if not getattr(task, "campus_only", False):
        return
    access = campus_access_for(app_state, request, tenant_id)
    if access.allows_submission:
        return
    if access is CampusAccess.UNKNOWN:
        raise HTTPException(
            status_code=409,
            detail=(
                "この課題は学内からのみ提出できますが、接続元を判定できませんでした。"
                "担当の教員にお知らせください。"
            ),
        )
    raise HTTPException(
        status_code=409,
        detail="この課題は学内からのみ提出できます（いまは学外から接続しています）。",
    )


def _previews_unopened_sets(role: Role) -> bool:
    """公開前の問題セットを「公開前」の印付きで一覧に出す相手か（#340）。

    **教員・TA だけ。** 出せるのに一覧に無いと、URL を直接叩くしかない。

    **どの課題を出すかはここでは決めない。** 一覧の元（`_course_and_tasks`）が
    `may_see` で既に絞っている ── 秘匿の課題は、TA の一覧には公開前の印付き
    でも出ない。ここが決めるのは、残ったもののうち公開前のものを見せるか
    だけである。

    提出の側（公開前に試しに出せるか）は `aijudge_core.may_submit_before_open`。
    広げるのは「まだ」の側だけで、受付終了（`CLOSED`）は誰にも開けない（#73）。
    テナント管理者は含めない ── コースの `Enrollment` を持たない相手まで
    広げるかは、#340 とは別に決める。
    """
    return role in (Role.INSTRUCTOR, Role.ASSISTANT)


def _role_in(app_state: StudentApp, course_id: CourseId, user_id: UserId) -> Role:
    """このコースでの役割（#108）。**受講が無ければ学習者として扱う。**

    ここに来る時点で `require_membership` は通っているので、無いのは通常
    起こらない。起きたときに試行扱いにすると、その提出は静かに測定から
    消える ── 数えられて気づくほうがよい。
    """
    with app_state.database.unit_of_work() as uow:
        enrollment = uow.identity.find_enrollment(course_id, user_id)
    return Role.LEARNER if enrollment is None else enrollment.role


def _course_and_tasks(
    app_state: StudentApp, me: Principal, course_id: CourseId
) -> tuple[Course, tuple]:
    with app_state.database.unit_of_work() as uow:
        auth = AuthService(uow.identity, audit=uow.audit)
        try:
            role = auth.require_membership(course_id, me.user_id)
        except PermissionDenied as exc:
            # 存在しないコースと、受講していないコースを区別しない。
            # 区別すると、どのコースが存在するかを列挙できる。
            raise HTTPException(status_code=404, detail="コースが見つかりません") from exc
        course_obj = uow.identity.get_course(course_id)
        if course_obj is None:
            raise HTTPException(status_code=404, detail="コースが見つかりません")
        tasks = uow.tasks.list_for_course(course_id)
        moment = now()
        versions = []
        for task in tasks:
            # 取り下げた課題は出さない（#51）。**消えてはいない** ── 提出も
            # 採点も残っており、教員の一覧には印付きで並ぶ。
            if task.withdrawn:
                continue
            # **見せてよい課題だけを出す**（`aijudge_core.access`）。学習者に
            # 公開前の課題を、TA に公開前の秘匿の課題を出さない。ここで落とす
            # ので、コースページ・進捗・到達度のどれにも現れない。
            if not may_see(task, role, now=moment):
                continue
            # **承認済みの版だけを出す**（#48）。`latest_version` は版番号
            # だけを見るので、生成したまま誰も見ていない版や却下した版が
            # そのまま学習者に出ていた ── 画面は「未承認 — 出題されません」
            # と書いてある。承認済みが無い課題はまだ存在しないものとして扱う。
            version = uow.tasks.latest_published_version(task.id)
            if version is not None:
                versions.append((task, version))
    return course_obj, tuple(versions)


def _task_and_course(
    app_state: StudentApp, me: Principal, task_version_id: TaskVersionId
) -> tuple[TaskVersion, Course, Task]:
    with app_state.database.unit_of_work() as uow:
        version = uow.tasks.get_version(task_version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        if not version.is_published:
            # **一覧から外すだけでは、URL を知っていれば開ける**（提出開始の
            # 判定と同じ理屈）。承認前・却下済みの版は開かせないし、提出も
            # 受け付けない（#48・設計原則 P5）。
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        task = uow.tasks.get_task(version.task_id)
        if task is None or task.withdrawn:
            # 一覧から外すだけでは、URL を知っていれば開ける（#48 と同じ理屈）。
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        course_obj = uow.identity.get_course(task.course_id)
        auth = AuthService(uow.identity, audit=uow.audit)
        try:
            role = auth.require_membership(task.course_id, me.user_id)
        except PermissionDenied as exc:
            raise HTTPException(status_code=404, detail="課題が見つかりません") from exc
        if course_obj is None:
            raise HTTPException(status_code=404, detail="コースが見つかりません")
        # **見せてよい課題か**（`aijudge_core.access`）。課題ページ・提出・動画の
        # 関門はすべてここを通る。以前は公開日時を見ておらず、一覧から外れて
        # いても URL を知っていれば公開前の問題文を開けた。見せない理由を
        # 「無い」と区別しない（受講していない課題と同じ 404）。
        if not may_see(task, role, now=now()):
            raise HTTPException(status_code=404, detail="課題が見つかりません")
    return version, course_obj, task


@dataclass(frozen=True)
class LoadedSubmission:
    """結果画面が要るもの一式。

    文脈（コース・問題セット・課題・提出）をすべての画面に出すために、まとめて返す。
    """

    submission: Submission
    version: TaskVersion
    task: Task
    course: Course
    run: object | None
    view: ResultView | None
    # 採点キューにまだ仕事が残っているか。**段階ごとに持つ**（ADR 0011）──
    # 決定的評価が届いても AI 評価はこれからで、その区間に何も言わないと
    # 学習者は再読み込みを繰り返すしかない。
    awaiting_deterministic: bool = False
    awaiting_ai: bool = False
    # 採点開始時刻がまだ来ていない（試験・#67）。
    grading_held: bool = False

    @property
    def grading_in_progress(self) -> bool:
        """機械の採点がまだ動いているか。**人の採点待ちは含めない** ──
        押しても届かないものを「待っています」と言うと待ち続けさせる。"""
        return self.awaiting_deterministic or self.awaiting_ai


def _submission_view(
    app_state: StudentApp, me: Principal, submission_id: SubmissionId
) -> LoadedSubmission:
    with app_state.database.unit_of_work() as uow:
        target = uow.submissions.get(submission_id)
        if target is None or target.learner_id != me.user_id:
            # 他人の提出は「無い」と答える。所有者が違うことを伝えると、
            # 提出 ID の存在自体が漏れる。
            raise HTTPException(status_code=404, detail="提出が見つかりません")
        run = uow.runs.latest_for(submission_id)
        # **その採点が使った版で描く。** 提出が指すのは出したときの版だが、
        # 実施中に課題を訂正して採点し直すと、採点はあとの版で付く（#43）。
        # 提出側の版で描くと、観点の重みや段階の説明が採点と食い違う。
        version = uow.tasks.get_version(
            target.task_version_id if run is None else run.context.task_version_id
        )
        if version is None:
            version = uow.tasks.get_version(target.task_version_id)
        if version is None:
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        task = uow.tasks.get_task(version.task_id)
        course = None if task is None else uow.identity.get_course(task.course_id)
        if task is None or course is None:
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        review = None if run is None else uow.reviews.find_review_for_run(run.id)
        # 確認が 2 件以上なら訂正されている（#275）。**件数だけ見る** ──
        # 学習者に要るのは「直された」という事実で、誰が何を書いたかではない。
        review_count = 0 if run is None else len(uow.reviews.reviews_for_run(run.id))
        request = None if run is None else uow.reviews.find_request_for_run(run.id)
        # 確定は Finalization が表す。HumanReview は「教員が読んだ」記録で
        # あって確定ではない（ADR 0010）。
        finalization = None if run is None else uow.reviews.find_finalization_for_run(run.id)
        awaiting_deterministic = uow.jobs.awaiting(submission_id, GradingPhase.DETERMINISTIC)
        # 待っているのが「順番」なのか「試験の終わり」なのかで、画面に
        # 書くことも自動更新の要否も変わる。
        held = bool(uow.jobs.waiting_count([submission_id], now()))
        awaiting_ai = uow.jobs.awaiting(submission_id, GradingPhase.AI)

    view = (
        None
        if run is None
        else build_result_view(
            run,
            version,
            review,
            request=request,
            finalization=finalization,
            # 仮確定の窓を出すのに要る。**起点は採点完了時刻**（run が持つ）で、
            # 猶予は問題セットかコースが持つ。
            auto_finalize_after_minutes=grace_minutes(
                task.auto_finalize_after_minutes, course.auto_finalize_after_minutes
            ),
            reviews=review_count,
        )
    )
    return LoadedSubmission(
        submission=target,
        version=version,
        task=task,
        course=course,
        run=run,
        view=view,
        awaiting_deterministic=awaiting_deterministic,
        awaiting_ai=awaiting_ai,
        grading_held=held,
    )


# 画面に埋め込んでよい種別。**それ以外はダウンロードさせる** ── 学習者が
# 出したファイルをインラインで返すと、ブラウザが中身を解釈しうる（#75）。
_INLINE_KINDS = (ArtifactKind.IMAGE, ArtifactKind.PDF, ArtifactKind.VIDEO)


def _serve_video(
    app_state: StudentApp, request: Request, artifact: object, artifact_id: str
) -> Response:
    """動画を Range 対応でストリーム配信する。**全体をメモリに読まない。**"""
    store = app_state.video_store
    if store is None:
        raise HTTPException(status_code=404, detail="提出物が見つかりません")
    key = artifact.storage_key  # type: ignore[attr-defined]
    filename = artifact.filename  # type: ignore[attr-defined]
    try:
        size = store.size(key)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="提出物が見つかりません") from exc
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": content_disposition("inline", filename, artifact_id),
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


def _submitted_files(submission: Submission) -> tuple[dict[str, object], ...]:
    """提出物を画面に出すための行。**種別が出し方を決める。**

    種別は提出時に決まっている（`aijudge_core.uploads.kind_for`）ので、
    ここで判定し直さない。
    """
    return tuple(
        {
            "id": str(artifact.id),
            "filename": artifact.filename or "提出物",
            "kind": artifact.kind,
            "is_image": artifact.kind is ArtifactKind.IMAGE,
            "is_pdf": artifact.kind is ArtifactKind.PDF,
            "is_video": artifact.kind is ArtifactKind.VIDEO,
            # 保存期間を過ぎて実体を消したもの（ADR 0020）。**画面で言う。**
            # 出し分けずに埋め込むと、壊れた再生器が出るだけで理由が出ない。
            "is_purged": artifact.is_purged,
            "byte_size": artifact.byte_size,
        }
        for artifact in submission.gradable_artifacts
    )


def _source_of(app_state: StudentApp, submission: Submission) -> str:
    """提出物を本文として読む。**読めるものだけ。**

    以前は種別を見ずに `decode` していたので、画像や PDF を出すと化けた
    バイナリが行番号つきで並んだ（#75）。読めないものは空を返し、画面は
    種別に応じた出し方（`<img>`・ダウンロード）に切り替える。
    """
    for artifact in submission.gradable_artifacts:
        if artifact.kind in _INLINE_KINDS or artifact.kind is ArtifactKind.DOCX:
            return ""
        try:
            return app_state.store.get(artifact.storage_key).decode("utf-8", "replace")
        except Exception:  # pragma: no cover - ストアが読めない状況
            return ""
    return ""


def build_context(
    course: Course,
    task: Task | None = None,
    version: TaskVersion | None = None,
    submission: Submission | None = None,
) -> dict[str, object]:
    """どのコースのどの問題セットのどの課題か、誰の何回目の提出かを 1 つにまとめる。

    **すべての画面に出す。** 出さないと、複数のコース・問題セット・提出を行き来する
    うちに「いま何を見ているか」が分からなくなる。ブラウザの戻る操作や
    リンクの共有で途中の画面から入ることもある。
    """
    return {
        "ctx_course": course,
        "ctx_task": task,
        "ctx_version": version,
        "ctx_submission": submission,
    }


def _numbered(source: str) -> list[tuple[int, str]]:
    return list(enumerate(source.replace("\r\n", "\n").split("\n"), 1))


def now() -> datetime:  # pragma: no cover - テンプレート用
    return datetime.now(UTC)
