"""S1 共通の例外。

`service.py`・`oidc.py` の双方が投げる。別ファイルに切り出しているのは、
`oidc.py`（`OidcSettings` を使う `repository.py`）と `service.py`
（`repository.py` を使う）の間で import が循環しないようにするため。
"""

from __future__ import annotations


class AuthenticationFailed(Exception):
    """認証に失敗した。

    **理由を分けない。** 「利用者が居ない」と「パスワードが違う」を
    分けて返すと、有効な ID の一覧を作れてしまう。OIDC 側でも同じ理屈で、
    state 不一致・ドメイン外・トークン検証失敗を分けずにここへ正規化する。
    """


class PermissionDenied(Exception):
    """権限が無い。"""
