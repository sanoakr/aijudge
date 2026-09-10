"""帯を全ページに配る（#189・ADR 0017 §2）。

**ハンドラには渡させない。** 教員コンソールの画面は 34 経路あり、帯のデータを
引数として渡す形にすると渡し忘れる場所が 34 できる。忘れると帯が消えるか、
数字だけ消える ── どちらも画面を見ただけでは「そういう画面なのか」と
区別できない。

解決は 2 段。

    request.state.rail_course_id   3 経路（/review/{submission_id}/…）
    request.path_params            17 経路（/courses/{course_id}/… など）
    どちらも無ければテナントの帯   14 経路

`request.state` を先に見るのは、`/review/…` の 3 経路が**既にコースを読んで
いる**から（`reveal` は問題文と課題を出すのに要る）。帯のために
`submission → task_version → task → course` を引き直すのは、手元にある値を
捨ててもう一度買うことになる。

**数えられなくても画面は出す**（ADR 0017 §3）。帯は成績を読む・付ける機能の
前提ではないので、集計の不調がレビューの画面を巻き込んではいけない（P2）。
"""

from __future__ import annotations

import logging

from fastapi import Request

from aijudge_core import Role
from aijudge_core.ids import CourseId

from .rail import Rail, RailGroup, RailItem, course_rail, tenant_rail
from .urls import root_prefix

logger = logging.getLogger(__name__)

#: ハンドラが帯にコースを教えるための場所。パスから解決できない経路で使う。
RAIL_COURSE_ID = "rail_course_id"


def _course_id_of(request: Request) -> CourseId | None:
    """この要求がどのコースの下にあるか。**引かずに分かる範囲だけ**で答える。"""
    from_state = getattr(request.state, RAIL_COURSE_ID, None)
    if from_state:
        return CourseId(str(from_state))
    raw = request.path_params.get("course_id")
    return CourseId(str(raw)) if raw else None


def rail_context(request: Request):
    """`Jinja2Templates(context_processors=[...])` に渡す。

    **返す鍵は `rail` 1 つだけ。** Starlette は context processor の結果で
    ハンドラの context を上書きするので（`context.update(...)`）、名前が
    衝突するとハンドラが渡した値が黙って消える。
    """
    console = getattr(request.app.state, "aijudge", None)
    principal = getattr(request.state, "principal", None)
    if console is None or principal is None:
        # ログイン前の画面（`/login`・`/auth/…`）。**帯を出すものが無い** ──
        # 誰の担当コースかも決まっていない。
        return {}

    try:
        rail = _build(console, request, principal)
        # 接頭辞を外した経路で突き合わせる（帯の href はまだ素の形）。
        path = request.url.path.removeprefix(root_prefix())
        return {"rail": _resolved(rail, path)}
    except Exception:
        # **握って画面を出す**（ADR 0017 §3）。ここで例外を上げると、集計の
        # 不調が採点の画面ごと落とす。記録は残す ── 黙って消えると、帯が
        # 出ない理由を追う手がかりが無くなる。
        logger.warning("rail unavailable", exc_info=True)
        return {}


def _resolved(rail: Rail, path: str) -> Rail:
    """行き先に接頭辞を当て、現在地に印を付ける。

    接頭辞（`AIJUDGE_CONSOLE_ROOT_PREFIX`）は**要求のたびに解決する**

    **要求のたびに解決する** ── `root_prefix()` は環境変数を読む関数で、
    ── `root_prefix()` は環境変数を読む関数で、値を組み立て時に固定すると
    `RedirectResponse` とずれる（`urls.py`）。忘れると `/console` の下で帯の
    リンクが全部 404 になる（#165 で同じ忘れ方を出荷している）。

    現在地は**いま見ているパスと突き合わせて**決める。画面ごとに鍵を書く
    取り決めにすると、書き忘れた画面だけ現在地が出ない ── 見ただけでは
    「そういう画面なのか」と区別できない壊れ方になる。
    """
    prefix = root_prefix()
    groups = tuple(
        RailGroup(
            title=g.title,
            meta=g.meta,
            items=tuple(
                RailItem(
                    label=i.label,
                    href=f"{prefix}{i.href}",
                    count=i.count,
                    attention=i.attention,
                    current=i.href == path,
                )
                for i in g.items
            ),
        )
        for g in rail.groups
    )
    return Rail(
        title=rail.title,
        subtitle=rail.subtitle,
        groups=groups,
        href=f"{prefix}{rail.href}",
        counts_unavailable=rail.counts_unavailable,
    )


def _build(console, request: Request, principal) -> Rail:
    course_id = _course_id_of(request)
    is_admin = bool(getattr(principal, "is_tenant_admin", False))
    if course_id is None:
        return tenant_rail(is_admin=is_admin)

    with console.database.unit_of_work() as uow:
        course = uow.identity.get_course(course_id)
        if course is None:
            return tenant_rail(is_admin=is_admin)
        enrollment = uow.identity.find_enrollment(course_id, principal.user_id)
        # **帯も自分で確かめる。** 経路のハンドラが認可するのが本筋だが、
        # ここはパスの `course_id` をそのまま信じて全ページで走る ── 認可を
        # 忘れたハンドラが 1 つあれば、帯がコース名と件数を漏らす。
        # 引き直しはしない（受講登録はこの下でどのみち要る）。
        grades = is_admin or (
            enrollment is not None
            and enrollment.role in (Role.INSTRUCTOR, Role.ADMIN, Role.ASSISTANT)
        )
        if not grades:
            return tenant_rail(is_admin=is_admin)

        # TA にはコースの設定を出さない（`manage.py` の権限と揃える）。
        # **テナント管理者は受講登録が無くても管理できる**（#128）。
        can_manage = is_admin or (
            enrollment is not None and enrollment.role in (Role.INSTRUCTOR, Role.ADMIN)
        )
        counts = uow.reviews.attention_counts_for_course(course_id)

    return course_rail(
        course,
        contested=counts.contested,
        unfinalized=counts.unfinalized,
        can_manage=can_manage,
    )
