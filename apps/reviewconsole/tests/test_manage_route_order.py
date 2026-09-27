"""`/manage` の**どのルートも、先に登録された別のルートに横取りされない**（段階 4）。

FastAPI（Starlette）は登録順にパスを照合し、最初に合ったものが応答する。
`/tasks/new` より先に `/tasks/{task_id}` が登録されると、「新しい課題」の画面は
`task_id="new"` の課題として扱われる。`manage/` を領域ごとのモジュールに分けると
登録の順が動くので、それを見張る。

写しのテスト（`test_reviewconsole_routes.py`）は行を並べ替えて比べるので、順序の
変化は捕まえない。ここではパスの引数に見本の値を入れた URL を作り、各ルートに
最初に合うのがそのルート自身であることを確かめる。
"""

from __future__ import annotations

import re

from fastapi.routing import APIRoute
from starlette.routing import Match

from aijudge_reviewconsole.app import TEMPLATES
from aijudge_reviewconsole.manage import register

# 見本の値。`new` などの固定の語と重ならない語にする。
SAMPLE_SEGMENT = "sample0"
PARAMETER = re.compile(r"\{[^}]+\}")


def _scope(method: str, path: str) -> dict:
    return {"type": "http", "method": method, "path": path, "root_path": ""}


def test_every_manage_route_answers_its_own_path() -> None:
    routes = [route for route in register(TEMPLATES).routes if isinstance(route, APIRoute)]
    assert routes, "ルートが 1 本も無い"

    shadowed: list[str] = []
    for route in routes:
        path = PARAMETER.sub(SAMPLE_SEGMENT, route.path)
        for method in sorted(route.methods - {"HEAD"}):
            first = next(
                candidate
                for candidate in routes
                if candidate.matches(_scope(method, path))[0] is Match.FULL
            )
            if first is not route:
                shadowed.append(
                    f"{method} {route.path} ({route.endpoint.__name__}) は "
                    f"{first.path} ({first.endpoint.__name__}) に横取りされる"
                )
    assert not shadowed, "\n".join(shadowed)


def test_the_check_catches_a_parameter_registered_before_a_fixed_word() -> None:
    """確かめ方そのものの確認: 順を逆にすると捕まる。"""
    routes = [route for route in register(TEMPLATES).routes if isinstance(route, APIRoute)]
    fixed = next(route for route in routes if route.path.endswith("/users/new"))
    parameter = next(route for route in routes if route.path.endswith("/users/{user_id}"))
    path = PARAMETER.sub(SAMPLE_SEGMENT, fixed.path)
    assert parameter.matches(_scope("GET", path))[0] is Match.FULL
