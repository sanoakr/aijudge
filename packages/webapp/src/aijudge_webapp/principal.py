"""要求の主体（ログインしている人）の解決。"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Request

from aijudge_identity import Principal

#: 解決した主体を置く `request.state` の属性名。**依存を通れない読み手**
#: （コンソールの帯の context processor、`rail_context`）がここを見る。
PRINCIPAL_STATE = "principal"

#: `request.state` の主体が「まだ引いていない」ことを表す番兵。
#: **`None` と区別する** ── 署名の無い要求で毎回引き直すのを避ける。
_UNRESOLVED: object = object()


def current_principal(
    request: Request, *, cookie: str, resolve: Callable[[str], Principal | None]
) -> Principal | None:
    """セッションの Cookie から主体を引く。**1 要求につき 1 回だけ。**

    結果を `request.state` に持たせるのは、帯（#189）が context processor
    から同じ値を要るため ── そこは依存を通れないので、自分で引くと
    1 ページあたりセッションの解決が 2 回になる。`AuthService.resolve` は
    副作用の無い読み取りなので、要求の中で使い回してよい。

    以前はコンソールだけがこうしていて、学習者アプリは呼ぶたびに引いていた
    （段階的な立て直し 1-2）。揃えてよいことは確かめてある: 学習者アプリで
    主体を引くのは要求の冒頭（`require_principal` と `/`）だけで、ログアウト
    などで状態を変えた後に同じ要求の中で引き直す経路は無い。**そういう経路を
    足すなら、キャッシュが古い主体を返すことに注意する。**

    `resolve` はトークンを主体に変える関数（保存先を開くのはアプリの仕事）。
    Cookie が無ければ呼ばない。
    """
    cached = getattr(request.state, PRINCIPAL_STATE, _UNRESOLVED)
    if cached is not _UNRESOLVED:
        return cached  # type: ignore[return-value]

    token = request.cookies.get(cookie, "")
    principal = resolve(token) if token else None
    setattr(request.state, PRINCIPAL_STATE, principal)
    return principal
