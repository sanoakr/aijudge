"""学習者アプリの**全ルートを固定する**（段階的な立て直し・段階 0-1）。

`docs/design/staged-refactor.md` の約束 1 と 5: 移動は振る舞いを変えない。
共有部品を `packages/webapp` に移す（段階 1〜3）と、ルートの登録が別の場所に動く。
1 つ落としても、そのボタンを押すまで誰も気づかない ── 押されるのが学期中の
締切前だったりする。**ルートの一覧（メソッド・パス・ハンドラ）を写しとして
置き、1 行でも変われば落とす。**

ルートを意図して足した・消したときは、写しを作り直す:

    AIJUDGE_UPDATE_ROUTES=1 uv run pytest apps/studentweb/tests/test_studentweb_routes.py

ハンドラ名まで含めるのは、移動で**同じパスが別の関数に付け替わる**のを捕まえる
ため（段階 4 で関数の置き場所が変わるときは、モジュール名の差分だけが出る）。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.routing import Mount, Route, WebSocketRoute

from aijudge_persistence import Database
from aijudge_studentweb import StudentApp, create_app
from aijudge_submission import FilesystemArtifactStore

SNAPSHOT = Path(__file__).with_name("routes_studentweb.txt")
REPO_ROOT = Path(__file__).resolve().parents[3]


def route_lines(app) -> list[str]:
    lines: list[str] = []
    for route in app.routes:
        if isinstance(route, APIRoute | Route):
            endpoint = route.endpoint
            where = f"{endpoint.__module__}:{endpoint.__name__}"
            for method in sorted(route.methods or ()):
                if method == "HEAD":
                    continue
                lines.append(f"{method} {route.path} {where}")
        elif isinstance(route, WebSocketRoute):
            lines.append(f"WS {route.path} {route.endpoint.__module__}:{route.endpoint.__name__}")
        elif isinstance(route, Mount):
            lines.append(f"MOUNT {route.path} {route.name}")
        elif hasattr(route, "effective_route_contexts"):
            # `include_router` で入れたルータ。**この版の FastAPI は展開せずに入れ物で持つ**
            # （`_IncludedRouter`）── 前置き（`/manage` など）を付けた実際のパスは、
            # 入れ物が組み立てる。非公開の仕組みなので、名前ではなく属性で見分ける。
            for context in route.effective_route_contexts():
                endpoint = context.endpoint
                where = f"{endpoint.__module__}:{endpoint.__name__}"
                methods = (
                    getattr(context, "methods", None)
                    or getattr(context.starlette_route, "methods", None)
                    or {"WS"}
                )
                for method in sorted(methods):
                    if method == "HEAD":
                        continue
                    lines.append(f"{method} {context.path} {where}")
        else:  # pragma: no cover - 未知の種類は写しに残して気づかせる
            lines.append(f"OTHER {getattr(route, 'path', '?')} {type(route).__name__}")
    return sorted(lines)


def test_the_routes_are_exactly_the_snapshot(tmp_path: Path) -> None:
    database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
    state = StudentApp(
        database,
        FilesystemArtifactStore(tmp_path / "artifacts"),
        profiles_dir=REPO_ROOT / "subjects",
    )
    lines = route_lines(create_app(state))
    database.dispose()

    if os.environ.get("AIJUDGE_UPDATE_ROUTES"):
        SNAPSHOT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    expected = SNAPSHOT.read_text(encoding="utf-8").splitlines()

    missing = sorted(set(expected) - set(lines))
    added = sorted(set(lines) - set(expected))
    assert not missing and not added, (
        "ルートが写しと違います。意図した変更なら AIJUDGE_UPDATE_ROUTES=1 で作り直してください。\n"
        + "".join(f"  - 消えた: {line}\n" for line in missing)
        + "".join(f"  + 増えた: {line}\n" for line in added)
    )
