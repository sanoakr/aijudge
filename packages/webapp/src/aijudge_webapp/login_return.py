"""ログインが切れたら、ログインし直して元のページへ戻す。

以前はセッションが切れたページを開くと `{"detail":"ログインしてください"}` の
JSON がそのまま画面に出た。利用者はそこから自分でログイン画面を探し、ログイン
後は一覧（`/`）から開いていたページを辿り直すしかなかった。

**戻り先はこのアプリの中のパスに限る。** `next` は URL に載る外部入力なので、
そのまま `Location` に置くと、ログインを経由して任意のサイトへ飛ばせる
（open redirect）── 本物のログイン画面を踏ませた直後に偽物へ渡せる。
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import quote, unquote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, Response

#: ログイン画面とログインの経路が戻り先を運ぶ引数の名前。
NEXT_PARAM = "next"

#: Google へ行って戻るあいだ戻り先を預ける Cookie。認可の往復は外部を通るので、
#: URL の引数では運べない（`redirect_uri` は Google に登録した値と一字一句同じで
#: なければならない）。
NEXT_COOKIE = "aijudge_login_next"

#: `NEXT_COOKIE` の寿命（秒）。state の Cookie（`OIDC_STATE_COOKIE`）と揃える ──
#: 認可の往復より長く残しても使い道が無い。
NEXT_COOKIE_MAX_AGE = 600

#: 戻り先の長さの上限。Cookie は 1 本 4KB までで、絞り込みを載せた一覧の URL でも
#: これを超えることはない。超えるものは戻り先として扱わない（`/` に戻す）。
MAX_NEXT_LENGTH = 2048

LOGIN_REQUIRED_MESSAGE = "ログインしてください"

#: 戻り先にしない経路。ログイン画面へ戻すと、ログインした直後にまたログイン画面が出る。
_LOGIN_PATHS = ("/login", "/logout")
_AUTH_PREFIX = "/auth/"


class LoginRequired(HTTPException):
    """セッションが無い（切れた）ことを表す 401。

    **他の 401 と分けるための型。** ページを開いた要求に限ってログイン画面へ
    送り直すが、それはこの理由の 401 だけに当てる ── API トークンの 401
    （`aijudge_reviewconsole.api`）は呼び出し側のプログラムが読むものである。
    """

    def __init__(self) -> None:
        super().__init__(status_code=401, detail=LOGIN_REQUIRED_MESSAGE)


def safe_next(value: str | None) -> str | None:
    """戻り先として使ってよいパスなら返す。使えなければ `None`。

    通すのは `/` で始まるアプリ内のパス（クエリ付き可）だけ。`//host` と
    `/\\host` はブラウザが別ホストとして読むので落とす。制御文字は
    `Location` ヘッダの中で行を割れるので落とす。
    """
    if not value or len(value) > MAX_NEXT_LENGTH:
        return None
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return None
    path = value.split("?", 1)[0]
    if path in _LOGIN_PATHS or path.startswith(_AUTH_PREFIX):
        return None
    return value


def wants_page(request: Request) -> bool:
    """ブラウザがページとして開いた要求か（`Accept` に `text/html` がある）。

    **画面の自動更新（`live.js`）は `fetch` の既定の `Accept: */*` で来る。**
    それをログイン画面へ送ると、更新がログイン画面の HTML を差し込んでしまうので、
    こちらは従来どおり 401 を返す。
    """
    return "text/html" in request.headers.get("accept", "")


def return_path(request: Request, *, prefix: str = "") -> str | None:
    """ログインし直した後に戻すパス（アプリ内・接頭辞なし）。

    ページを開いた要求（GET）ならそのページ。フォームの送信（POST など）は
    送った内容をもう一度送れないので、**送信元のページ**（`Referer`）へ戻す ──
    同じ URL を GET で開くと、送信先の経路は多くが 405 になる。

    `Referer` はブラウザが外から見たパスなので、逆プロキシの接頭辞
    （`/console` など）が付いている。アプリ内のパスに直してから検査する。
    """
    if request.method in ("GET", "HEAD"):
        query = request.url.query
        return safe_next(request.url.path + (f"?{query}" if query else ""))

    referer = request.headers.get("referer", "")
    if not referer:
        return None
    parts = urlsplit(referer)
    path = parts.path
    if prefix:
        if path != prefix and not path.startswith(f"{prefix}/"):
            return None
        path = path[len(prefix) :] or "/"
    return safe_next(path + (f"?{parts.query}" if parts.query else ""))


def login_location(next_path: str | None) -> str:
    """ログイン画面のパス（アプリ内・接頭辞なし）。戻り先があれば載せる。"""
    if next_path is None:
        return "/login"
    return f"/login?{NEXT_PARAM}={quote(next_path, safe='')}"


def keep_next(response: Response, next_path: str | None, cookie_kwargs: dict[str, object]) -> None:
    """Google へ渡す応答に戻り先を預ける。戻り先が無ければ前の預かりを消す。

    **消すのは、古い戻り先で戻されないため。** 前回の往復を途中でやめた
    Cookie が残っていると、今回は一覧から入った人が昔のページへ飛ばされる。
    """
    if next_path is None:
        response.delete_cookie(NEXT_COOKIE, path="/")
        return
    # パーセント符号化して置く。`/`・`?`・`;` を含む値は Cookie の引用符で
    # 包まれ、読む側の実装しだいで引用符ごと返ってくる。
    response.set_cookie(
        NEXT_COOKIE,
        quote(next_path, safe=""),
        max_age=NEXT_COOKIE_MAX_AGE,
        **cookie_kwargs,  # type: ignore[arg-type]
    )


def kept_next(request: Request) -> str | None:
    """`keep_next` で預けた戻り先。**読むときにもう一度検査する**（Cookie も外部入力）。"""
    return safe_next(unquote(request.cookies.get(NEXT_COOKIE, "")))


def install_login_return(app: FastAPI, *, prefix: Callable[[], str] = lambda: "") -> None:
    """`LoginRequired` をページの要求ではログイン画面への転送に変える。

    `prefix` は外から見えるパス接頭辞を返す関数（コンソールの
    `AIJUDGE_CONSOLE_ROOT_PREFIX`）。要求のたびに読む ── 起動後に環境変数を
    差し替えるテストがあるので、組み立て時に固定しない。
    """

    async def handle(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, LoginRequired)
        if not wants_page(request):
            return await http_exception_handler(request, exc)
        root = prefix()
        target = login_location(return_path(request, prefix=root))
        return RedirectResponse(f"{root}{target}", status_code=303)

    app.add_exception_handler(LoginRequired, handle)
