"""パス接頭辞つき配置（#103 §5）の下で、絶対パスのリンクを外から見える経路に合わせる。

nginx が `/console` のような接頭辞でこのアプリへプロキシしていても、アプリ自身は
接頭辞を知らずに `/courses/123` のような絶対パスを生成する（テンプレートも
`RedirectResponse` の呼び出しもそう書かれている）。ここで一括して接頭辞を足す ──
呼び出し側を書き換えずに済ませることで、100 か所を超える書き換え漏れのリスクを消す。

既定（環境変数未設定）は空文字列で、従来どおりルート直下で動く。
"""

from __future__ import annotations

import os

from fastapi.responses import RedirectResponse as _BaseRedirectResponse

ENV_ROOT_PREFIX = "AIJUDGE_CONSOLE_ROOT_PREFIX"


def root_prefix() -> str:
    """外部から見えるこのアプリのパス接頭辞（例: `/console`）。既定は空。"""
    return os.environ.get(ENV_ROOT_PREFIX, "").rstrip("/")


def prefixed(path: str) -> str:
    """絶対パス（`/` 始まり）にだけ接頭辞を足す。それ以外はそのまま返す。"""
    if path.startswith("/"):
        return f"{root_prefix()}{path}"
    return path


class RedirectResponse(_BaseRedirectResponse):
    """`fastapi.responses.RedirectResponse` の差し替え。絶対パスに接頭辞を足す。

    呼び出し側（`app.py` / `manage.py` の 40 か所超）は import 元を変えるだけで、
    引数の書き方は一切変えなくてよい。
    """

    def __init__(self, url: str, *args: object, **kwargs: object) -> None:
        super().__init__(prefixed(url), *args, **kwargs)
