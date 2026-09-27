"""画面の領域をまたいで使う、権限の確かめとテナント画面のパンくず（段階 4）。

`manage/` を領域ごとのモジュールに分けると、どの領域も最初に「誰が・どのコースで」を
確かめる。その薄い包みをここ 1 か所に置く（地図 `docs/design/manage-split-map.md`）。
判定そのものは `aijudge_reviewconsole.access` と `Principal` が持ち、ここは画面の
応答（403 など）に読み替えるだけ。
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from aijudge_core import Role
from aijudge_identity import Principal


def _console(request: Request):
    return request.app.state.aijudge


def _require_admin(request: Request, me: Principal) -> None:
    """テナント全体の管理者であること（#128）。

    コースの作成はコース単位の権限では表せない（まだコースが無い）。
    以前は「どこかのコースで ADMIN の受講がある」を代わりにしていたが、
    #128 でテナント単位の属性（`User.is_tenant_admin`）に置き換えた。
    """
    if not me.is_tenant_admin:
        raise HTTPException(status_code=403, detail="コースの作成には管理者権限が必要です")


def _is_admin(request: Request, me: Principal) -> bool:
    """テナント全体の管理者か（`_require_admin` の判定版）。"""
    return me.is_tenant_admin


# テナント単位の管理画面の親（#165）。**画面ごとに文字列を書き写さない** ──
# 書き写すと、一覧の見出しを直したときにパンくずの側が古い名前のまま残る。
USERS_STEP = ("利用者の一覧", "/manage/users")


SUBJECTS_STEP = ("科目プロファイル", "/manage/subjects")


def _trail(*steps: tuple[str, str | None]) -> tuple[dict[str, str | None], ...]:
    """パンくずの経路。`担当コース` の下に続く段を `(名前, 経路)` で並べる。

    `経路` が `None` の段が現在地で、リンクにしない。**コースの下にない画面の
    ためにある**（#165）── コース配下の経路は `course` / `section` /
    `task_meta` / `submission` から `base.html` が組み立てており、そちらは
    そのまま。両方を 1 つの `<nav>` に入れると、コース名の位置に管理画面の
    名前が入る形になり、階層の意味が壊れる。
    """
    return tuple({"label": label, "href": href} for label, href in steps)


# 画面から与えてよい役割。**`admin` は入らない。**
#
# `admin` はコースを作れて、テナント内のどのコースにも届く。担当教員が
# 自分のコースの受講者一覧から配れる権限ではない。以前は `Role` の全値を
# そのまま選択肢にしていたので、`assistant` と `instructor` の間に
# `admin` が並んでいた（#100）。
#
# **`admin` は `aijudge-admin` で作る。** 利用者の新規作成を CLI に限って
# あるのと同じ規則で、画面から配れない権限は画面に出さない。
#
# **付与者による上限の差は無い**（2026-09-08 に #126 を覆した）。担当教員も
# `instructor` を付けられる ── 実運用では、コースの担当を増やすのに毎回
# 管理者を呼ぶ形が回らなかった。#126 は「担当教員どうしが際限なく教員を
# 増やせる」ことを避けて `assistant` までに狭めていたが、その心配は
# **コース単位**の権限にとどまる（コースをまたぐ権限＝`admin` は今も
# 画面から配れない）ので、担当を任せられる相手を担当教員が決められる方を採る。
GRANTABLE_ROLES: tuple[Role, ...] = (Role.LEARNER, Role.ASSISTANT, Role.INSTRUCTOR)


def _require_grantable(role: Role) -> Role:
    """画面から与えてよい役割か。**弾く理由をそのまま返す。**"""
    if role not in GRANTABLE_ROLES:
        raise HTTPException(
            status_code=403,
            detail=(
                f"{role.value} はこの画面からは付けられません（`aijudge-admin` で行ってください）"
            ),
        )
    return role
