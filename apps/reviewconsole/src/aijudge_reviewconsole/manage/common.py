"""画面の領域をまたいで使う、権限の確かめとテナント画面のパンくず（段階 4）。

`manage/` を領域ごとのモジュールに分けると、どの領域も最初に「誰が・どのコースで」を
確かめる。その薄い包みをここ 1 か所に置く（地図 `docs/design/manage-split-map.md`）。
判定そのものは `aijudge_reviewconsole.access` と `Principal` が持ち、ここは画面の
応答（403 など）に読み替えるだけ。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from urllib.parse import quote, unquote

from fastapi import HTTPException, Request
from pydantic import ValidationError

import aijudge_webui as webui
from aijudge_core import Course, LatePenaltyStep, Role
from aijudge_core.ids import CourseId
from aijudge_course_admin.kc import allowed_namespaces, list_for_namespaces
from aijudge_grading import load_profile
from aijudge_identity import INSTRUCTOR_ROLES, AuthService, PermissionDenied, Principal

from .. import access


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


def _require_reader(request: Request, me: Principal, course_id: CourseId) -> tuple[Course, Role]:
    """そのコースの**採点者以上**（TA を含む）。読むだけの画面はここを通す。

    TA が課題を読めないと、学習者の質問にも自分が採点している提出にも
    答えられない ── **読むことと直すことは別の権限である**（#102）。
    公開前の課題も読める：採点は公開前に用意されるものだから。

    返り値に役割を含めるのは、画面が「直せるかどうか」で描き分けるため。
    権限の判定をテンプレート側でやり直させない。
    """
    console = _console(request)
    with console.database.unit_of_work() as uow:
        auth = AuthService(uow.identity, audit=uow.audit)
        try:
            role = auth.require_membership(course_id, me.user_id)
        except PermissionDenied as exc:
            # 存在と権限を区別しない（コースを列挙させない）。
            raise HTTPException(status_code=404, detail="コースが見つかりません") from exc
        if role is Role.LEARNER:
            raise HTTPException(status_code=403, detail="この画面には採点者の権限が必要です")
        course = uow.identity.get_course(course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="コースが見つかりません")
    return course, role


def _require_instructor(request: Request, me: Principal, course_id: CourseId) -> Course:
    """そのコースの教員であること。**TA には開けない。**

    締切と受講の変更は成績に直接効く。採点を分担する TA と、履修の管理を
    する教員は別の権限である。判定と応答の規則は `access.require_instructor`
    に 1 つだけある（段階的な立て直し 1-1）。
    """
    return access.require_instructor(_console(request), me, course_id)


def _course_kcs(console, course):
    """このコースが作問で選べる知識要素 ── **コースに足したものだけ**（#289）。
    引退したものは出さない ── 選べば課題に付いてしまう。
    """
    namespaces = allowed_namespaces(
        load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
    )
    kcs = list_for_namespaces(console.database, namespaces, include_deprecated=False)
    chosen = set(course.knowledge_components)
    return [kc for kc in kcs if kc.key in chosen]


def _role_counts(enrollments) -> list[dict[str, object]]:
    """役割ごとの人数。**0 名の役割も並べる。**

    総数だけでは、TA を登録し忘れているのか 0 名が正しいのかが読み取れない。
    並びは `Role` の宣言順にする（多い順にすると、コースを開くたびに順番が
    変わって目で追えない）。
    """
    counted = Counter(str(enrollment.role.value) for enrollment in enrollments)
    return [{"role": role.value, "count": counted.get(role.value, 0)} for role in Role]


def _first_error(exc: ValidationError) -> str:
    """模型の検証エラーを 1 行にする。教員に読める文だけを出す。"""
    for error in exc.errors():
        message = str(error.get("msg", ""))
        return message.removeprefix("Value error, ")
    return "指定が不正です"


def _normalized_unit(raw: str) -> str:
    """URL から来た問題セットの鍵を、`unit_key` と同じ形に揃える。

    経路パラメータは復号された状態で届くので、`quote` した鍵と直接
    比べると、記号を含む鍵（`ex 03` など）が一致しない。往復させて
    どちらの形で来ても同じ鍵になるようにする。
    """
    return quote(unquote(raw), safe="")


def _parse_minutes(raw: str) -> int | None:
    """自動確定までの猶予（分）。空なら未指定。

    0 を許すと締切と同時に確定し、締切直前の提出が採点前に確定しうる。
    猶予は正の値でなければ意味がない。
    """
    text = raw.strip()
    if not text:
        return None
    try:
        minutes = int(text)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"分の形式が不正です: {raw!r}") from None
    if minutes <= 0:
        raise HTTPException(status_code=400, detail="猶予は 1 分以上にしてください")
    return minutes


def _parse_when(raw: str) -> datetime | None:
    """`YYYY-MM-DDTHH:MM` を読む。空なら None（締切なし）。

    **入力欄の時刻は機関のタイムゾーン**（`AIJUDGE_TIMEZONE`・既定 JST）として
    読み、保存は UTC。以前は UTC として読んでいたので、教員が「23:59」と
    打った締切が JST の翌朝 8:59 になっていた。素の naive 値のまま入れると
    締切判定がサーバのローカル時刻に依存する（ADR 0006 で同じ罠を踏んでいる）。
    """
    text = raw.strip()
    if not text:
        return None
    try:
        return webui.from_local(datetime.fromisoformat(text))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"日時の形式が不正です: {raw!r}") from exc


def _can_edit(role: Role) -> bool:
    """課題や設定を**変えて**よい役割か。TA は読むだけ（#102）。"""
    return role in INSTRUCTOR_ROLES


def _kc_keys_of(uow, version) -> tuple[str, ...]:
    """この課題版が問う知識要素のキー。**Q-matrix が正**（#267）。

    画面には ID ではなくキーを出す ── `kc_7d9d…` は人には読めない。
    引けなかったものは ID のまま出す（黙って落とすと、件数が合わない）。
    """
    keys: list[str] = []
    for entry in version.q_matrix:
        component = uow.skills.get_kc(entry.kc_id)
        keys.append(getattr(component, "key", None) or str(entry.kc_id))
    return tuple(keys)


def _plain(value):
    """監査の `detail` に入れられる形へ均す。

    日時は ISO 文字列にする ── JSON にそのまま入らないし、入れ方を
    書き込み点ごとに決めると、後から同じ条件で引けなくなる。
    """
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, LatePenaltyStep):
        # 割合ではなく % で残す（画面と照らし合わせるため・`late_penalty.describe` と同じ）。
        return {"after_hours": value.after_hours, "percent": round(value.ratio * 100, 4)}
    if isinstance(value, tuple) and value and isinstance(value[0], LatePenaltyStep):
        return [_plain(step) for step in value]
    return value
