"""コース・課題・受講の管理（教員向け）。

**YAML の直接編集を運用の前提にしない**（設計方針 §9.2 Phase 2）。学期の頭に
やることは CLI（`aijudge-admin`）でもできるが、学期中に発生する作業
── 締切の設定、受講者の追加、課題の追加 ── を教員がターミナルで行うのは
現実的でない。

課題の追加は**画面と API の両方から**でき、保存は同じ経路を通る
（`aijudge_admin.save_task`）。zip での一括取り込みは廃止した ── 移行元
（Sharif Judge）の形式をサーバの入口の語彙にしてしまっており、移行が
終わったあとも一生ついて回る形だった。まとまった投入は API で行う
（`api.py`）。

**コースが使っている科目プロファイル（`*.yaml`）は読み取り専用。** 1 つの
プロファイルは複数のコースの雛形になりうるので、書き換えると自分が担当して
いないコースの採点まで変わる（ADR 0002 の「コードと同じ扱いでレビューを
通す」はこの範囲のこと）。

そこで #146 で編集できる範囲を絞った ── **未参照のものだけ直接編集・改名
でき、参照中のものへの唯一の操作は「複製して編集」**（`/manage/subjects`）。
判定は `aijudge_admin.profiles` が持ち、この層は画面と繋ぐだけ。判定を画面に
写すと、食い違ったときに「画面では編集できるのに保存が拒否される」形で出る。
コース設定の画面（`_course_page`）から雛形を書く口は、以前どおり無い
── そこで触るのはコースごとの上書き（`aijudge_grading.overrides`）。

権限は 2 段。
- コースの作成・削除は **ADMIN**
- そのコースの課題・受講の管理は **INSTRUCTOR 以上**（TA には開けない。
  締切や受講の変更は成績に直接効く）

成績の確定もここに置く。教員の待ち行列は異議申立だけなので（ADR 0009）、
依頼が出なかった提出を閉じる導線がどこかに要る。課題ごとの一括確定と、
締切からの猶予（`auto_finalize_after_hours`）の設定がそれである
（ADR 0010）。**猶予は科目プロファイルではなくコースに持つ** ── 締切と
同じ性質の運用値で、教員が学期中に決めるものだから。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, unquote

from fastapi import APIRouter, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

import aijudge_webui as webui
from aijudge_admin import (
    AdminError,
    allowed_namespaces,
    assert_registered,
    delete_course,
    delete_kc,
    duplicate_course,
    duplicate_profile,
    edit_kc,
    enrol_roster,
    ensure_course,
    finalize_task,
    finalize_tasks,
    kc_usage,
    list_for_namespaces,
    list_profiles,
    outputs_for,
    parse_roster,
    pending_counts,
    plan_bundle,
    read_bundle,
    read_profile_text,
    rename_profile,
    restore_kc,
    retire_kc,
    rubric,
    save_grading_settings,
    save_profile_text,
    save_task,
    template_bundle,
    template_of,
    try_settings,
)
from aijudge_admin.bundles import MAX_ARCHIVE_BYTES
from aijudge_admin.course_definition import course_template
from aijudge_admin.drafting import TaskDrafter
from aijudge_admin.revision import TaskReviser
from aijudge_admin.roster import RosterEntry, RosterError, generate_password
from aijudge_admin.syllabus import (
    MAX_SYLLABUS_BYTES,
    SyllabusError,
    SyllabusReader,
    read_document,
    to_markdown,
)
from aijudge_admin.task_verifier import TaskVerifier
from aijudge_admin.tasks import clear_unit
from aijudge_admin.tasks import delete as delete_task
from aijudge_admin.tasks import withdraw as withdraw_task
from aijudge_admin.test_cases import InputProposer, SolutionWriter, TestCaseWriter
from aijudge_audit import AuditAction
from aijudge_authoring import (
    DraftKind,
    TaskChecks,
    TaskDraftRecord,
    TaskSpec,
    images,
    render_markdown,
    render_statement,
)
from aijudge_authoring.drafting import Blueprint, Difficulty
from aijudge_authoring.spec import AI_EVALUATOR, TestCaseSpec
from aijudge_core import (
    DEFAULT_UPLOAD_SUFFIXES,
    DIVISIONS,
    MIN_JUSTIFICATION_LENGTH,
    SUFFIX_GROUPS,
    Aggregation,
    Course,
    EvaluatorKind,
    GradeWindow,
    ReviewState,
    Role,
    Task,
    TestCase,
    campus_access,
    format_term,
    new_id,
    normalize_suffixes,
    offered_years,
    parse_cidrs,
)
from aijudge_core.ids import CourseId, TaskId, TaskVersionId, UserId, derived_id
from aijudge_eval_code_test_runner import EVALUATOR_ID as CODE_TEST_RUNNER
from aijudge_eval_code_test_runner import LANGUAGES
from aijudge_grading import (
    LOCKED_KEYS,
    EvaluatorRegistry,
    OverrideError,
    effective_profile,
    load_profile,
    test_case_shape,
)
from aijudge_grading.overrides import diff
from aijudge_identity import AuthenticationFailed, AuthService, PermissionDenied, Principal
from aijudge_identity.network import MAX_CIDRS, CampusNetworkSettings
from aijudge_identity.oidc import DEFAULT_LOGIN_LABEL, LOGIN_LABEL_MAX, OidcSettings
from aijudge_submission import SubmissionService

from . import mastery
from .audit_context import recorder_for, source_ip_of
from .overview import empty_unit, find_unit, load_units, unit_key
from .urls import RedirectResponse

# ルータは `register()` の中で毎回作る。モジュール階層に置くと、
# `create_app` を 2 回呼んだときに同じ経路が二重に登録される
# （テストで複数のアプリを作ると起きる。FastAPI が Duplicate Operation ID を
# 警告して気づいた）。


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


def _require_local_account(me: Principal) -> None:
    """ローカルパスワードを持つ利用者であること（#180）。

    存在しない画面として扱う（403 ではなく 404）。権限の問題ではない ──
    SSO 利用者にはローカルパスワードという概念が無い。「権限がありません」は
    「昇格すれば使える」と読めてしまう。
    """
    if me.is_external:
        raise HTTPException(status_code=404, detail="この利用者にパスワードはありません")


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


def _can_edit(role: Role) -> bool:
    """課題や設定を**変えて**よい役割か。TA は読むだけ（#102）。"""
    return role in (Role.INSTRUCTOR, Role.ADMIN)


def _require_instructor(request: Request, me: Principal, course_id: CourseId) -> Course:
    """そのコースの教員であること。**TA には開けない。**

    締切と受講の変更は成績に直接効く。採点を分担する TA と、履修の管理を
    する教員は別の権限である。
    """
    console = _console(request)
    with console.database.unit_of_work() as uow:
        auth = AuthService(uow.identity, audit=uow.audit)
        try:
            role = auth.require_membership(course_id, me.user_id)
        except PermissionDenied as exc:
            # 存在と権限を区別しない（コースを列挙させない）。
            raise HTTPException(status_code=404, detail="コースが見つかりません") from exc
        if role not in (Role.INSTRUCTOR, Role.ADMIN):
            raise HTTPException(status_code=403, detail="この操作には担当教員の権限が必要です")
        course = uow.identity.get_course(course_id)
    if course is None:
        raise HTTPException(status_code=404, detail="コースが見つかりません")
    return course


def _require_enrolment_manager(
    request: Request, me: Principal, course_id: CourseId
) -> tuple[Course, Role]:
    """受講者の役割を変えられる者。**そのコースの教員か、テナント全体の管理者。**

    `_require_instructor` はコースの受講登録を要求するが、管理者は
    「指定されたコースに限定した教員を昇格させる」ことができる必要があり、
    昇格させる前のコースにまだ自分が受講登録されているとは限らない（#126）。
    ここだけ、コースの一員でない管理者も通す。

    返す役割は「付与者としての役割」── 管理者はそのコースの実際の受講が
    `learner` や無所属でも `Role.ADMIN` として扱う。`_require_grantable`
    が上限を決めるときの基準になる。
    """
    console = _console(request)
    with console.database.unit_of_work() as uow:
        auth = AuthService(uow.identity, audit=uow.audit)
        try:
            role = auth.require_membership(course_id, me.user_id)
        except PermissionDenied:
            role = None
        course = uow.identity.get_course(course_id)

    if role in (Role.INSTRUCTOR, Role.ADMIN):
        if course is None:
            raise HTTPException(status_code=404, detail="コースが見つかりません")
        return course, role
    if _is_admin(request, me):
        if course is None:
            raise HTTPException(status_code=404, detail="コースが見つかりません")
        return course, Role.ADMIN
    if role is None:
        # 存在と権限を区別しない（コースを列挙させない）。
        raise HTTPException(status_code=404, detail="コースが見つかりません")
    # 受講はしているが役割が足りない（例: TA）。ここは存在を隠す理由が無い。
    raise HTTPException(status_code=403, detail="この操作には担当教員の権限が必要です")


@dataclass(frozen=True)
class _Merged:
    """複数課題の確定結果を 1 つに畳んだもの。画面が読む形は 1 件と同じ。"""

    task: object
    finalized: int
    contested: int


def _merged(outcomes) -> _Merged:
    return _Merged(
        task=outcomes[0].task if outcomes else None,
        finalized=sum(outcome.finalized for outcome in outcomes),
        contested=sum(outcome.contested for outcome in outcomes),
    )


def _wants_tests(request: Request, course, version=None) -> bool:
    """この課題はテスト実行で正しさを確定させるか。

    宣言していない科目（レポートなど）では、テストケースが無いのが正常で
    あって落ちたわけではない。両者を同じ顔で警告すると、警告が読まれなくなる。

    **課題のプロファイルで見る**（#195・#264）。混在コース（レポートと
    プログラム）では、コースの値で見ると課題ごとに答えが変わらず、
    「テストが無い」の警告が出るべき課題に出ず、出ない課題に出る。
    版が無い場面（新しい課題を作る前）だけコースの既定に落ちる。
    """
    name = version.subject_profile if version is not None else course.subject_profile
    profile = load_profile(_console(request).profiles_dir / f"{name}.yaml")
    return CODE_TEST_RUNNER in profile.deterministic


def _data_driven_criteria(registry, version) -> dict[str, tuple[str, ...]]:
    """この課題版が**検証データで採点する**観点の評価器を、データの形ごとに。

    返す形は `{"io": ("code_test_runner",), "items": ("checklist_ai_judge",)}`。
    画面はこれで編集欄を選ぶ ── 入出力の組と項目の並びは別の部品である。

    **科目の宣言ではなく、この課題の観点で見る**（#300）。観点はそれぞれ
    自分の評価器を持ち（`RubricCriterion.evaluator_id`・ADR 0018）、科目が
    宣言していない評価器を 1 問にだけ割り当てることがある。科目の宣言だけで
    判断していたので、`code_test_runner` を割り当てた課題でも入出力セットが
    画面に出ず、**その評価器が何を走らせるのかを確かめる手段が無かった**。

    **「決定論的か」では答えにならない**（#302）。提出の遵守
    （`submission_compliance`）は決定論的だが検証データを持たず、項目表の
    照合（`checklist_ai_judge`）は AI だが課題ごとの項目表を読む。読むか、
    どの形かは評価器が宣言する（`aijudge_grading.test_case_shape`）── 画面が
    評価器名の表を持つと、評価器を足した日にその表だけが古くなる。

    登録済みの評価器だけを見る ── `HUMAN_SCORED`（人が採点する）と空
    （AI が判定する）はここに入らない。
    """
    if version is None:
        return {}
    shapes = {name: test_case_shape(registry.get(name)) for name in registry.ids()}
    out: dict[str, set[str]] = {}
    for criterion in rubric.from_criteria(version.criteria):
        shape = shapes.get(criterion.evaluator)
        if shape is None:
            continue
        out.setdefault(shape, set()).add(criterion.evaluator)
    return {shape: tuple(sorted(names)) for shape, names in sorted(out.items())}


def _cases_by_shape(registry, version) -> dict[str, tuple]:
    """この課題が持つ検証データを、形ごとに分ける。

    **混ぜて出さない**（#302）。1 つの課題が入出力と項目表の両方を持てる
    （`TestCase.evaluator_id` で分かれる）ので、画面もその単位で見せる ──
    混ぜると、入出力の表に項目が並び、どちらの編集欄で直すのか読めない。

    **観点ではなく、データ自身の評価器で分ける**（#303）。観点が評価器を
    指名していない課題でも、データは持っていることがある（観点を宣言する前に
    作られた課題、評価器を付け替えた課題）── 観点を基準に選ぶと、持っている
    のに画面から消える。使われていないことは画面の側で言う。
    """
    if version is None:
        return {}
    out: dict[str, list] = {}
    for case in version.test_cases:
        try:
            shape = test_case_shape(registry.get(case.evaluator_id))
        except KeyError:
            # 入っていない評価器あてのデータ。科目の構成を変えた直後に
            # 起きうる。**形が分からないので出さない**（入出力の欄で
            # 編集させると黙って壊す）。
            shape = None
        if shape is None:
            continue
        out.setdefault(shape, []).append(case)
    return {shape: tuple(cases) for shape, cases in out.items()}


#: 入出力の組を読む評価器（この画面が編集している側）。**名前で書かない** ──
#: 評価器が形を名乗るので、そこから引く（`_io_evaluator_ids`）。
def _io_evaluator_ids(registry) -> tuple[str, ...]:
    return tuple(name for name in registry.ids() if test_case_shape(registry.get(name)) == "io")


def _kept_cases(version, *, editing: tuple[str, ...]):
    """いま直していない評価器あての検証データを、そのまま持ち越す（#302）。

    **保存は版を作り直す操作なので、渡さなかったものは消える。** 1 つの課題が
    入出力の組と項目表の両方を持てるようになったので、片方の画面で保存すると
    もう片方が黙って消えた ── 消えても例外は出ず、採点の段になって「検証
    データが無い」として現れる。

    宣言は `TestCaseSpec` に残す（評価器と中身をそのまま）── 課題の既定に
    倒すと、別の評価器あてに書き換わる。
    """
    if version is None:
        return ()
    return tuple(
        TestCaseSpec(
            name=case.name,
            evaluator=case.evaluator_id,
            payload=dict(case.payload),
            hidden=case.hidden,
            weight=case.weight,
        )
        for case in version.test_cases
        if case.evaluator_id not in editing
    )


def _split_aliases(raw: str) -> tuple[str, ...]:
    """言い換えの入力を分ける。カンマ・読点・改行のどれでも区切れる。

    **区切り文字を 1 つに決めない。** 日本語の一覧は読点で書くのが自然で、
    決めつけると「、で区切ったら 1 件になった」が起きる。
    """
    parts = re.split(r"[,、\n]+", raw)
    return tuple(part.strip() for part in parts if part.strip())


def _statement_diff(before: str, after: str) -> tuple[dict[str, str], ...]:
    """問題文の差分（#306）。行単位の unified 差分を、画面が描ける形で返す。

    **承認は差分を見て決めるもの**である。承認待ちの一覧は新しい版の本文を
    出すだけだったので、改訂を承認する人は書き換わった問題文を頭から読み直す
    ことになっていた ── どこが変わったのかは、読み比べないと分からない。

    文脈は 2 行。**全文の差分にしない** ── 長い課題文では変わっていない行が
    画面を埋め、変わった行が探しにくくなる。
    """
    rows: list[dict[str, str]] = []
    for line in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=2):
        if line.startswith("+++") or line.startswith("---"):
            continue
        kind = (
            "meta"
            if line.startswith("@@")
            else "add"
            if line.startswith("+")
            else "del"
            if line.startswith("-")
            else "same"
        )
        rows.append({"kind": kind, "text": line})
    return tuple(rows)


def _submission_count(console, task) -> int:
    """この課題への提出の件数。**削除してよいかの判定に使う。**"""
    if task is None:
        return 0
    with console.database.unit_of_work() as uow:
        return uow.tasks.submission_count(task.id)


def _task_of(console, course, task_id: str):
    """このコースの課題。**存在と権限を区別しない**（他コースを探らせない）。"""
    with console.database.unit_of_work() as uow:
        task = uow.tasks.get_task(TaskId(task_id))
    if task is None or task.course_id != course.id:
        raise HTTPException(status_code=404, detail="課題が見つかりません")
    return task


def _test_case_error(request: Request, course, task) -> str | None:
    """直近のテストケース生成の失敗理由。**この課題のものだけ。**

    `Console` は全利用者で共有なので、コースと課題を突き合わせる ── 添えないと
    別の課題の失敗が出る。
    """
    if task is None:
        return None
    recorded = getattr(_console(request), "last_test_case_error", None)
    if recorded is None:
        return None
    course_id, task_id, reason = recorded
    if course_id != str(course.id) or task_id != str(task.id):
        return None
    return reason


def _regradable(console, task, version) -> int:
    """この課題で、**まだ確定していない**うち古い版で採点されている件数。

    確定済みは数えない。確定は「この成績で閉じた」という事実で（ADR 0010）、
    あとから機械が採点し直すと、確定の記録が指す採点と学習者に見える採点が
    食い違う。訂正前に確定した提出を直すのは、教員が 1 件ずつ読む仕事として
    残す。
    """
    if task is None or version is None:
        return 0
    with console.database.unit_of_work() as uow:
        rows = uow.reviews.unfinalized_for_task(task.id)
    return sum(
        1 for _submission, run, _request in rows if run.context.task_version_id != version.id
    )


def _plain(value):
    """監査の `detail` に入れられる形へ均す。

    日時は ISO 文字列にする ── JSON にそのまま入らないし、入れ方を
    書き込み点ごとに決めると、後から同じ条件で引けなくなる。
    """
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _digest(text: str) -> str:
    """プロファイル本文の指紋。

    全文は記録しない ── 数十 KB あり、その大半は「なぜこの値なのか」を書いた
    コメントである。**どの版だったかが後から言えれば足りる。**
    """
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _record_profile_change(console, request, me, *, action, name, summary, detail) -> None:
    """科目プロファイルの変更を監査に残す（ADR 0016）。

    **これは書き込みの後である。** プロファイルはファイルで、DB の
    トランザクションには載らない ── 監査行が書けなくてもファイルは既に
    変わっている。DB の中の操作（成績・権限）が持つ「記録できなければ操作も
    成立しない」という保証は、ここには無い。

    それでも記録する価値はある。プロファイルは採点の設定そのもので、
    **1 つが複数のコースの採点を変える**（`subjects/README.md`）ので、
    誰がいつ触ったかが分からないのは困る。
    """
    with console.database.unit_of_work() as uow:
        recorder_for(uow, request, me).record(
            action,
            target_type="subject_profile",
            target_id=name,
            summary=summary,
            detail=detail,
        )
        uow.commit()


def _criteria_graded_by_tests(version, profile):
    """テストケースを足したときの観点。**正しさをテスト実行に戻す。**

    テストケースを足しただけでは何も変わらない ── 観点は自分の評価器を
    持っており（`RubricCriterion.evaluator_id`）、テストの無い課題では
    正しさが AI 判定になっている。差し替えなければ、テストは作られたのに
    誰も実行しない。

    **題名と説明は、自動生成の文面のままのときだけ戻す。** 教員が書き直して
    いれば、その言葉を機械が上書きしない。
    """
    swapped = []
    for criterion in rubric.from_criteria(version.criteria):
        if criterion.code != "correctness" or criterion.evaluator != AI_EVALUATOR:
            swapped.append(criterion)
            continue
        update: dict[str, object] = {"evaluator": _test_evaluator_of(profile)}
        if criterion.title == "仕様の充足":
            update["title"] = "出力の正しさ"
            update["description"] = (
                "与えられた入力に対して仕様どおりの出力を返すか。テスト実行で判定する。"
            )
        swapped.append(criterion.model_copy(update=update))
    return tuple(swapped)


def _test_evaluator_of(profile) -> str:
    """この科目でテスト実行を担う評価器。宣言の順に見て最初のもの。"""
    for evaluator_id in profile.deterministic:
        if evaluator_id == CODE_TEST_RUNNER:
            return evaluator_id
    return CODE_TEST_RUNNER


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


def _gate_report(profile, spec, course, authored_by):
    """下書きに門をかけ、結果を返す（#321・ADR 0008）。

    **保存はしない。** 下書きは課題ではないので、検査の記録を課題版に紐づける
    場所が無い ── 結果は下書きそのものが持つ（`TaskDraftRecord.checks`）。

    仮の課題版を組んで検査に渡す。**採点と同じ経路で走らせる**ためで、
    ここだけ別の検査を書くと、門を通ったのに採点で落ちる下書きができる
    （`TaskVerifier` の冒頭と同じ判断）。

    検査そのものが動かないことはある（サンドボックス不在）。**そのときは
    None。** 「検査していない」は「合格」ではない（`VerificationReport`）。
    """
    from aijudge_authoring import build_task_version

    try:
        candidate = build_task_version(
            spec,
            course_id=course.id,
            subject_profile=course.subject_profile,
            authored_by=authored_by,
        )
        verifier = TaskVerifier(EvaluatorRegistry().load_installed(), profile)
        return TaskChecks(verification=verifier.verify(candidate), checked_at=datetime.now(UTC))
    except Exception:
        return None


def _record_gates(console, profile, version) -> None:
    """門 1・門 2 を通して結果を残す。**残さないと教員に何も示せない。**

    生成物は承認待ちで保存されるので、教員は未承認の一覧でこの結果を読んで
    承認するかどうかを決める（ADR 0008）。門が落ちても保存は取り消さない ──
    落ちたこと自体が教員の判断材料で、黙って消すと何も残らない。
    """
    from datetime import UTC, datetime

    try:
        verifier = TaskVerifier(EvaluatorRegistry().load_installed(), profile)
        report = verifier.verify(version)
    except Exception:
        # 検査そのものが動かないことはある（サンドボックス不在など）。
        # **保存は成立させる。** 検査が無いことは未承認の一覧に出る。
        return
    with console.database.unit_of_work() as uow:
        uow.tasks.save_checks(
            version.id,
            TaskChecks(verification=report, checked_at=datetime.now(UTC)),
        )
        uow.commit()


def _language_of(profile) -> str:
    """この科目の言語。`code_test_runner` の設定から取る。

    プロファイルが言語を持っているのに生成側で別に指定させると、
    「C の科目に Python の課題が生成される」が起きる。
    """
    options = profile.evaluator_options.get("code_test_runner", {})
    return str(options.get("language") or "c")


def _role_counts(enrollments) -> list[dict[str, object]]:
    """役割ごとの人数。**0 名の役割も並べる。**

    総数だけでは、TA を登録し忘れているのか 0 名が正しいのかが読み取れない。
    並びは `Role` の宣言順にする（多い順にすると、コースを開くたびに順番が
    変わって目で追えない）。
    """
    counted = Counter(str(enrollment.role.value) for enrollment in enrollments)
    return [{"role": role.value, "count": counted.get(role.value, 0)} for role in Role]


def _normalized_unit(raw: str) -> str:
    """URL から来た問題セットの鍵を、`unit_key` と同じ形に揃える。

    経路パラメータは復号された状態で届くので、`quote` した鍵と直接
    比べると、記号を含む鍵（`ex 03` など）が一致しない。往復させて
    どちらの形で来ても同じ鍵になるようにする。
    """
    return quote(unquote(raw), safe="")


def _key_of(task, version) -> str:
    """この課題を作った `TaskSpec.key`。

    ID は鍵から導いてある（`derived_id`）ので、次の版の ID も観点の ID も
    鍵が無いと作れない。新しい版は鍵を持っている（`TaskVersion.source_key`）。

    **持っていない古い版のために復元を試す。** 鍵は取り込み元の
    `<まとまり>/p<番号>` の形をしており、課題 ID と突き合わせれば当たりを
    確かめられる ── 当たらなければ諦めて断る（間違った鍵で保存すると、
    別の課題を上書きする）。
    """
    if version.source_key:
        return str(version.source_key)
    for candidate in _key_candidates(task):
        if derived_id("tsk", candidate) == str(task.id):
            return candidate
    raise HTTPException(
        status_code=409,
        detail=(
            "この課題は画面から直せません（取り込み時の課題キーが記録されて "
            "いない古い課題です）。API から同じキーで入れ直してください。"
        ),
    )


def _key_candidates(task) -> list[str]:
    unit = task.unit or ""
    position = task.position
    names: list[str] = []
    if unit and position:
        names += [f"{unit}/p{position}", f"{unit}/{position}", f"{unit}/p{position:02d}"]
    if unit:
        names += [unit, f"{unit}/p1"]
    names.append(task.title)
    return names


# 保存の合図。**押したことが分かるようにする。** 同じ画面に戻る操作は、
# 成功しても見た目が変わらないので、押せていないのか効いていないのかを
# 教員が区別できない。
# 保存後の戻り先。**その場に戻す。** 画面の先頭に飛ぶと、教員は自分が
# どこを触っていたのかを探し直すことになる（設定が縦に並ぶ画面ほど効く）。
# 素の HTML でこれをやるには、リダイレクト先に錨を付けるのが確実で、
# JavaScript も要らない。
# 受講登録の結果に載せる「未登録アカウント」の上限。それ以上は件数だけ言う。
MAX_UNKNOWN_SHOWN = 20

SAVED_MESSAGES: dict[str, str] = {
    "schedule": "日程を保存しました（この問題セットの全課題に反映）",
    "moved": "課題を移しました（日程は移動先に揃えました）",
    "grace": "保存しました",
    "number": "保存しました",
    "course_grace": "保存しました",
    "formats": "保存しました",
    "enrolled": "受講登録を保存しました",
    "removed": "受講を取り消しました",
    "tests_added": (
        "テストケースを生成し、**新しい版**を作りました。承認するまで学習者には"
        "いまの版が出続けます（未承認の課題（AI 作問）から中身を確かめて承認してください）"
    ),
    # **原因を決めつけない。** 作れない理由はいくつもある（モデルが止まって
    # いる・応答が長さで切れた・スキーマに合わない形で返した）。「S6 が
    # 止まっている可能性があります」と書いていたが、**実際に出たとき S6 は
    # 動いていた**（#52）。理由は `last_test_case_error` にそのまま出す。
    "tests_failed": "テストケースを作れませんでした。課題はいまの版のままです",
    "regraded": "この版で採点し直します（確定済みの提出はそのままです）",
    "tests_revised": (
        "テストケースを直して**新しい版**にしました。過去の採点は元の版のままです。"
        "出題済みの提出をこの版で見直すなら「この版で採点し直す」を押してください"
    ),
    "withdrawn": "出題を取り下げました（学習者に出なくなります。記録は残ります）",
    "restored": "出題の取り下げを取り消しました",
    "campus_networks": "学内ネットワークを保存しました",
    "campus_only": "この問題セットの受付範囲を変えました（セット内の全課題に反映）",
    # **版は上がらない。** 日程は課題の内容ではないので、直しても過去の
    # 採点基準は変わらない（ADR 0013・P8 の対象外）。
    "task_schedule": "この課題の日程を保存しました（版は上がりません）",
    "task_deleted": "課題を削除しました（提出が 1 件も無いもの）",
    "unit_cleared": "問題セットを片付けました",
    "released": "いままでの提出を採点に回しました（以後の提出はまた採点開始時刻まで待ちます）",
    "retried": "失敗していた採点を流し直しました",
    "items_revised": "項目表を直して新しい版にしました",
    "revision_queued": "書き直した版を承認待ちに積みました。差分を読んで承認してください",
    "revision_none": "直すところはありませんでした（版は増やしていません）",
    "revision_failed": "書き直せませんでした",
    "restored_version": "選んだ版の中身で新しい版を作りました（この版が学習者に出ます）",
    "draft_approved": "採用しました。課題になり、出題できます",
    "draft_dropped": "下書きを捨てました（課題にはなっていません）",
    "kc_added": "知識要素を追加しました",
    "kc_retired": "知識要素を引退させました",
    "kc_restored": "引退を取り消しました",
    "kc_deleted": "知識要素を削除しました（一度も使われていないもの）",
    "kc_edited": "知識要素の名前と説明を直しました（キーは変わりません）",
    "kc_scoped": "このコースが使う知識要素を更新しました（語彙からは消えません）",
    "basics": "基本情報を保存しました",
    "role": "役割を変えました",
    # **削除ではない**（#144・`AuthService.disable`）。過去の提出と採点が
    # 利用者を参照しているので、行は残したまま状態を倒し、セッションを切る。
    "disabled": "利用者を無効化しました（記録は残ります。セッションも切りました）",
    "course_deleted": "コースを削除しました（学習者の提出はありませんでした）",
    # 束の取り込み（#161）。**未承認で入ったことを黙らせない** ── 出題されて
    # いると思ったまま学期が進む形が、いちばん高くつく。
    "bundle_in_review": (
        "取り込みました。**未承認なので、まだ学習者には出ません** —— "
        "「未承認の課題（AI 作問）」から中身を確かめて承認してください"
    ),
    "bundle_saved": "取り込みました（承認済みとして入れたので、日程の範囲で出題されます）",
    "tenant_admin_granted": "テナント管理者にしました（すべてのコースで教員として扱われます）",
    "tenant_admin_revoked": "テナント管理者から外しました（役割はコースごとの受講で決まります）",
    "grading": "採点設定を保存しました",
    # 科目プロファイル（#146）。**参照中のものは書き換えられない**ので、
    # 保存できたのは未参照のものだけ。
    "profile_saved": "科目プロファイルを保存しました",
    "profile_duplicated": (
        "複製しました。この複製はどのコースからも参照されていないので、自由に編集できます"
    ),
    "profile_renamed": "名前を変えました",
    "order": "並びを変えました",
    "task": "課題を保存しました",
    # **黙って落とさない。** テストケースが無いと正しさの観点は AI 判定に
    # なる（`TaskSpec.auto_graded`）。保存できたことだけ伝えると、教員は
    # テスト実行で確定する課題を作ったつもりのまま学期を過ごす。
    "task_without_tests": (
        "課題を保存しました。テストケースが無いので、正しさは AI が判定します"
        "（テスト実行では確定しません）"
    ),
    # **作れなかったことと、作らないことは別。** 前者は直せる（S6 が戻れば
    # 作り直せる）ので、そう言えるようにしておく。
    "task_generation_failed": (
        "課題を保存しました。**テストケースを作れなかったので**、正しさは AI が"
        "判定します（S6 が止まっている可能性があります）。あとで作り直せます"
    ),
    "rubric": "共通ルーブリックを保存しました（新しい課題から使われます）",
    "generated": "課題を生成しました。承認するまで出題されません",
}


def _units_of(console, course):
    """このコースの問題セット。承認画面の選択肢に要る。"""
    with console.database.unit_of_work() as uow:
        return load_units(uow, course)


def _place_in_unit(console, course, task, target: str) -> None:
    """課題を問題セットへ置く。**日程は置いた先に揃える。**

    `move_task_to_unit` が課題の移動でしていることと同じで、承認のときにも
    要る（#84）── 作問の時点ではセットを決めないので、承認が「どこに出すか」
    を決める唯一の場面になる。規則を 2 か所に書くと、片方だけ直る。
    """
    with console.database.unit_of_work() as uow:
        siblings = [
            other
            for other in uow.tasks.list_for_course(course.id)
            if other.id != task.id and unit_key(other) == quote(target, safe="")
        ]
        head = sorted(siblings, key=lambda item: item.sort_key)[0] if siblings else None
        update: dict[str, object] = {"unit": target}
        if head is not None:
            update |= {
                "session": head.session,
                "opens_at": head.opens_at,
                "submissions_open_at": head.submissions_open_at,
                "due_at": head.due_at,
                "auto_finalize_after_minutes": head.auto_finalize_after_minutes,
            }
        positions = [other.position for other in siblings if other.position is not None]
        update["position"] = max(len(siblings), max(positions, default=0)) + 1
        uow.tasks.save_task(task.model_copy(update=update))
        uow.commit()


def _unit_group(console, course, unit: str):
    """URL の鍵から問題セットを引く。**画面と同じまとめ方**（`unit_key`）。"""
    key = _normalized_unit(unit)
    with console.database.unit_of_work() as uow:
        units = load_units(uow, course)
    group = find_unit(units, key)
    if group is None:
        raise HTTPException(status_code=404, detail="問題セットが見つかりません")
    return group


def _unit_group_or_empty(console, course, unit: str):
    """URL の鍵から問題セットを引く。**無ければ「まだ空の回」として返す。**

    回は課題が持つ属性で、それ自体の記録は無い（`create_unit` は何も保存
    しない）。だから「まだ 1 問も無い回」は存在しない回と区別できず、
    404 にすると回を作る導線が消える（`unit_settings` と同じ判断）。
    """
    key = _normalized_unit(unit)
    with console.database.unit_of_work() as uow:
        units = load_units(uow, course)
    return find_unit(units, key) or empty_unit(key, course)


def _exam_state(console, course, group, now: datetime) -> dict[str, object]:
    """試験の問題セットの状態（#67）。

    **落ちたジョブを必ず出す。** 一括採点で 90 名分が一斉に流れて一部が
    落ちると、いまはログにしか出ない ── 気づかなければ、その提出だけ成績が
    欠けたまま確定する。
    """
    version_ids = {str(version.id) for _task, version in group.tasks}
    if not version_ids:
        return {"waiting_jobs": 0, "failed_jobs": (), "last_release": None}
    with console.database.unit_of_work() as uow:
        # **課題版で絞ってから引く**（#230）。コース全件を引いて Python で
        # 絞ると、絞り込みが `list_for_course` の上限の後ろに来て、押す前に
        # 出すこの件数から新しい提出が抜ける。
        ids = [s.id for s in uow.submissions.list_for_versions(sorted(version_ids))]
        failed = uow.jobs.failed_for(ids)
        # 待たせている件数は「流したら何件動くか」。押す前に出す。
        waiting = uow.jobs.waiting_count(ids, now)
    return {
        "waiting_jobs": waiting,
        "failed_jobs": failed,
        "last_release": (
            console.last_release[1]
            if console.last_release is not None and console.last_release[0] == str(course.id)
            else None
        ),
    }


def _clear_plan(console, course, group) -> dict[str, int]:
    """このセットを片付けると何がどうなるか。**変えずに数えるだけ。**"""
    if not group.tasks:
        return {"deleted": 0, "withdrawn": 0, "untouched": 0, "total": 0}
    report = clear_unit(console.database, course_id=course.id, unit=group.unit, dry_run=True)
    return {
        "deleted": len(report.deleted),
        "withdrawn": len(report.withdrawn),
        "untouched": len(report.untouched),
        "total": report.total,
    }


def _update_unit(
    request: Request, course_id: str, unit: str, *, update: dict, saved: str
) -> Response:
    """問題セット内の全課題に同じ更新を当てる。

    **`model_copy` を使わない。** あれは検証を走らせないので、締切が公開より
    前の課題がそのまま保存され、次に読むときに初めて落ちる（実際にそうなった）。
    作り直して検証を通す。
    """
    from .app import require_principal

    me = require_principal(request)
    _require_instructor(request, me, CourseId(course_id))
    console = _console(request)

    key = _normalized_unit(unit)
    with console.database.unit_of_work() as uow:
        tasks = [
            task for task in uow.tasks.list_for_course(CourseId(course_id)) if unit_key(task) == key
        ]
        if not tasks:
            raise HTTPException(status_code=404, detail="この問題セットには課題がありません")
        before: dict[str, object] = {}
        for task in tasks:
            try:
                updated = Task.model_validate(task.model_dump() | update)
            except ValidationError as exc:
                # 日程の前後関係は模型が見ている（`Task._check_schedule`）。
                raise HTTPException(status_code=400, detail=_first_error(exc)) from None
            before |= {key: getattr(task, key, None) for key in update}
            uow.tasks.save_task(updated)
        # **締切と自動確定の猶予はここを通る。** どちらも成績がいつ閉じるかを
        # 決める値で（ADR 0013・ADR 0014）、後から「誰がいつ動かしたか」を
        # 言えないと、締切を巡る問い合わせに答えられない。
        recorder_for(uow, request, me).record(
            AuditAction.TASK_UPDATED,
            target_type="unit",
            # **記録が名指すのは問題セットそのもので、URL での姿ではない。**
            # `key` は経路に載せるために percent-encode してあり、日本語の
            # 名前だと 1 文字が 9 字に膨らむ ── 「第3回 配列とポインタ入門」で
            # 103 字になり、コースの id と合わせて列（128 字）を超える。
            # 復号すれば `tasks.unit` の幅（64 字）に収まり、記録としても
            # 読める（符号化された鍵は人にも機械にも引きにくい）。
            target_id=f"{course_id}/{unquote(key)}",
            summary=f"問題セットの設定を変えた（{saved}・課題 {len(tasks)} 件）",
            detail={
                "course_id": course_id,
                "field": saved,
                "changed": {
                    name: {"before": _plain(before.get(name)), "after": _plain(value)}
                    for name, value in update.items()
                },
            },
        )
        uow.commit()
    return RedirectResponse(
        f"/manage/courses/{course_id}/units/{key}?saved={saved}#saved", status_code=303
    )


def _chosen_suffixes(raw: list[str], marker: str, course: Course) -> tuple[str, ...]:
    """課題に入れる提出形式を決める。**空で保存しない。**

    画面はコースの既定をチェック済みで出すので、送られてくるのは常に
    「教員が選んだ結果」である。**1 つも選ばずに保存させない** ── 空で
    保存できると、その課題には何も提出できなくなり、原因が学習者側の
    問題に見える。

    `marker` はフォームから来たことの印。画面を経由しない呼び出し
    （API・移行）は形式を送らないので、そこではコースの既定を入れる。
    """
    chosen = normalize_suffixes(raw)
    if chosen:
        return chosen
    if marker:
        raise HTTPException(
            status_code=400, detail="提出できるファイル形式を 1 つ以上選んでください"
        )
    return course.upload_suffixes or DEFAULT_UPLOAD_SUFFIXES


def _collect_overrides(form) -> dict:
    """画面から来た値を上書きの形にする。**空欄は上書きしない。**

    空欄を 0 や空文字として保存すると、雛形に戻したいのか 0 にしたいのかが
    区別できなくなる。空欄は「雛形のまま」である。
    """

    def value(name: str) -> str:
        return str(form.get(name) or "").strip()

    overrides: dict = {}

    runner: dict = {}
    if value("language"):
        runner["language"] = value("language")
    if value("case_timeout_seconds"):
        runner["case_timeout_seconds"] = _positive_number(
            value("case_timeout_seconds"), "テストケースの上限"
        )
    if value("compile_timeout_seconds"):
        runner["compile_timeout_seconds"] = _positive_number(
            value("compile_timeout_seconds"), "コンパイルの上限"
        )
    judge: dict = {}
    if value("samples"):
        judge["samples"] = int(_positive_number(value("samples"), "サンプル数"))
    # 提出の遵守（#316）。**いままで YAML にしか無かった** ── 効いているのに
    # 画面のどこからも読めず、直すにはサーバ上のファイルを触るしかなかった。
    #
    # 上書きはそのコースにしか効かないので、教員が触ってよい（`overrides` の
    # 冒頭）。雛形（`subjects/*.yaml`）は読み取り専用のまま。
    compliance: dict = {}
    kinds = [name for name in form.getlist("required_kinds") if name]
    if kinds:
        compliance["required_kinds"] = kinds
    if value("filename_pattern"):
        pattern = value("filename_pattern")
        try:
            re.compile(pattern)
        except re.error as exc:
            # **保存させない。** 壊れた正規表現は採点の時に落ちる ── そのとき
            # 画面に出るのは「体裁が判定できません」だけで、原因が読めない。
            raise HTTPException(
                status_code=400, detail=f"ファイル名の規則が正規表現として不正です: {exc}"
            ) from None
        compliance["filename_pattern"] = pattern
    options = {}
    if runner:
        options["code_test_runner"] = runner
    if judge:
        options["rubric_ai_judge"] = judge
    if compliance:
        options["submission_compliance"] = compliance
    if options:
        overrides["evaluator_options"] = options

    if value("timeout_seconds"):
        overrides["timeout_seconds"] = _positive_number(value("timeout_seconds"), "評価器の上限")

    if value("blind_sample_rate"):
        rate = _positive_number(value("blind_sample_rate"), "blind 抽出率", allow_zero=True)
        if rate > 1:
            raise HTTPException(status_code=400, detail="blind 抽出率は 0〜1 で指定してください")
        overrides["measurement"] = {"blind_sample_rate": rate}

    policy: dict = {}
    for name, label in (
        ("confidence_below", "確信度の水準"),
        ("boundary_score", "合否の境界"),
        ("boundary_margin", "境界の幅"),
    ):
        if value(name):
            number = _positive_number(value(name), label, allow_zero=True)
            if number > 1:
                raise HTTPException(status_code=400, detail=f"{label}は 0〜1 で指定してください")
            policy[name] = number
    if policy:
        overrides["review_policy"] = policy

    for key, field in (("deterministic", "deterministic"), ("ai_evaluators", "ai_evaluators")):
        chosen = [name for name in form.getlist(field) if name]
        if chosen:
            overrides[key] = chosen
    return overrides


def _default_rubric_criteria():
    """組み込みの既定（正しさ＋読みやすさ）を宣言の形で返す。

    未設定のコースでも**いま何が使われているか**を画面に出すため。空欄を
    見せると、観点が無いのか既定なのかが分からない。
    """
    from aijudge_authoring.importers.sharif_judge import (
        correctness_criterion,
        readability_criterion,
    )

    correctness = correctness_criterion()
    return (
        correctness.model_copy(update={"weight": 0.7}),
        readability_criterion(0.3),
    )


def _aggregation_from_form(form) -> Aggregation | None:
    """観点の畳み方をフォームから読む。空なら None（＝コースに従う）。

    知らない値は落とす ── 黙って OR に倒すと、AND のつもりの課題が
    重み付き和で採点され、画面には何も出ない。
    """
    raw = str(form.get("aggregation") or "").strip()
    if not raw:
        return None
    try:
        return Aggregation(raw)
    except ValueError:
        raise AdminError(f"観点の畳み方が不正です: {raw!r}") from None


def _is_sso_login(login: str, domains: set[str]) -> bool:
    """学内ログイン（OIDC）のアカウントか ── 許可ドメインのメールアドレス。"""
    local, at, domain = login.rpartition("@")
    return bool(at and local) and domain.lower() in {d.lower() for d in domains}


def _rubric_from_form(form) -> list[dict[str, str]]:
    """ルーブリックの表を行に戻す。**行数は画面が決める**（増減できる）。

    `code`・`title`… の同名フィールドが行数ぶん並ぶので、位置で組み直す。
    """
    codes = form.getlist("criterion_code")
    # **削除は明示の印で**（`criterion_delete`・値は元のコード）。印は付いた
    # 行しか送られてこないので位置では組めず、元のコードで突き合わせる。
    deleted = {str(code) for code in form.getlist("criterion_delete")}
    originals = form.getlist("criterion_original")
    rows: list[dict[str, str]] = []
    for index in range(len(codes)):
        if index < len(originals) and str(originals[index]) in deleted:
            continue

        def at(field: str, index: int = index) -> str:
            values = form.getlist(field)
            return str(values[index]) if index < len(values) else ""

        rows.append(
            {
                "code": at("criterion_code"),
                "title": at("criterion_title"),
                "description": at("criterion_description"),
                "weight": at("criterion_weight"),
                "order": at("criterion_order"),
                "evaluator": at("criterion_evaluator"),
                "levels": at("criterion_levels"),
            }
        )
    return rows


def _artifact_kind_rows() -> list[dict[str, str]]:
    """提出物の種別と、それに当たる拡張子（#316）。

    **表は 1 つだけ**（`aijudge_core.uploads.SUFFIX_KINDS`）。画面に種別の
    一覧を書き写すと、拡張子を足した日にそこだけが古くなる ── 種別は拡張子
    から決まる（`kind_for`）ので、表を畳めば選択肢になる。
    """
    from aijudge_core import SUFFIX_KINDS

    grouped: dict[str, list[str]] = {}
    for suffix, kind in SUFFIX_KINDS.items():
        grouped.setdefault(kind.value, []).append(suffix)
    return [
        {"name": kind, "suffixes": " ".join(sorted(suffixes))}
        for kind, suffixes in sorted(grouped.items())
    ]


def _transcription_note(profile, accepted: tuple[str, ...] = ()) -> dict[str, object] | None:
    """この科目では、何が採点のときに本文へ書き起こされるか（#351）。

    **画面が黙っていると、教員は画像の観点を「人が採点する」に倒す。** 実際
    には書き起こされた本文を観点が読むので、決定的な照合も AI 評価器もその
    まま使える。それを知らせる場所は、観点に評価器を割り当てるまさにその
    画面しかない。

    **上書きを当てた後のプロファイルを渡すこと。** 書き起こすかどうかは
    コースの上書きで変わりうるので、ファイルの値で書くと画面が嘘をつく。

    **扱える種類は抽出器に訊く**（`applies_to`）。画面に対応表を書くと、
    抽出器を足した日にそこだけが古くなる（`_artifact_kind_rows` と同じ理由）。
    """
    from aijudge_core import SUFFIX_KINDS, ArtifactKind
    from aijudge_grading.registry import ExtractorRegistry

    declared = profile.input.transcription if profile is not None else ()
    if not declared:
        return None
    registry = ExtractorRegistry().load_installed()
    rows: list[dict[str, str]] = []
    covered: set[ArtifactKind] = set()
    for name in declared:
        try:
            extractor = registry.get(name)
        except KeyError:
            # **名前が解決できないことをここで騒がない。** この注記は補助で
            # あって、編集を止める理由ではない（起動時と採点時には落ちる）。
            continue
        kinds = [kind for kind in ArtifactKind if extractor.applies_to(kind)]
        covered.update(kinds)
        suffixes = sorted(suffix for suffix, kind in SUFFIX_KINDS.items() if kind in kinds)
        if suffixes:
            rows.append({"extractor": name, "suffixes": " ".join(suffixes)})
    if not rows:
        return None
    # **読まれない受付形式を名指しする。** 宣言した抽出器のどれも扱わない
    # 種類は、原本のまま評価器に渡り「読めない」と判定されて人に回る ──
    # 採点は止まらないぶん、**設定の誤りが結果に出ない。**
    unread = sorted(
        suffix
        for suffix in accepted
        if (kind := SUFFIX_KINDS.get(suffix)) is not None
        and kind not in covered
        and (kind.is_document or kind is ArtifactKind.IMAGE)
    )
    return {
        "rows": rows,
        "suffixes": " ".join(sorted({s for row in rows for s in row["suffixes"].split()})),
        "extractors": " ".join(row["extractor"] for row in rows),
        "unread": " ".join(unread),
    }


def _effective_profile_of(console, course, version):
    """この課題（無ければこのコース）に効いているプロファイル。

    **課題のプロファイルで見る**（#195・#264）── 混在コースではコースの値と
    食い違う。上書きは当てる（`effective_profile`）。読めなければ None。
    """
    name = version.subject_profile if version is not None else course.subject_profile
    try:
        base = load_profile(console.profiles_dir / f"{name}.yaml")
    except Exception:
        return None
    if not course.grading_overrides:
        return base
    try:
        return effective_profile(
            base, course.grading_overrides, EvaluatorRegistry().load_installed()
        )
    except OverrideError:
        return base


def _evaluator_rows(registry, kind) -> list[dict[str, str]]:
    """評価器の名前と 1 行説明。

    **説明は評価器が持つ**（クラスの docstring の 1 行目）。画面が名前ごとの
    表を持つと、評価器を足したときに説明だけ抜ける。
    """
    rows = []
    for name in sorted(registry.ids_of_kind(kind)):
        doc = (registry.get(name).__doc__ or "").strip()
        rows.append({"name": name, "about": doc.splitlines()[0] if doc else ""})
    return rows


def _positive_number(raw: str, label: str, *, allow_zero: bool = False) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{label}の形式が不正です: {raw!r}") from None
    if value < 0 or (value == 0 and not allow_zero):
        raise HTTPException(status_code=400, detail=f"{label}は正の値にしてください")
    return value


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


def _first_error(exc: ValidationError) -> str:
    """模型の検証エラーを 1 行にする。教員に読める文だけを出す。"""
    for error in exc.errors():
        message = str(error.get("msg", ""))
        return message.removeprefix("Value error, ")
    return "指定が不正です"


def _compose_key(unit: str, suffix: str) -> str:
    """問題セットの鍵と、その中での鍵を繋ぐ。

    `ex02` + `p8` → `ex02/p8`。まとまりが無い課題（未分類）は後半だけを
    鍵にする。後半に `/` が入っていればそれを尊重する ── 取り込み済みの
    課題と鍵を揃えたい場合があり、そこで縛ると直す手段が無くなる。
    """
    if not suffix:
        return ""
    if not unit or suffix.startswith(f"{unit}/"):
        return suffix
    return f"{unit}/{suffix}"


def _unit_href(course_id: str, task) -> str:
    """その課題が属する回のページ。

    課題を触る操作（締切・一括確定・追加）は**その回のページから来る**ので、
    そこへ戻す。コースのトップに返すと、教員は毎回同じ回を開き直すことになる。
    """
    return f"/manage/courses/{course_id}/units/{unit_key(task)}"


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


def register(templates) -> APIRouter:
    """テンプレートを束ねてルータを返す。呼ぶたびに新しいルータを作る。"""
    router = APIRouter(prefix="/manage")

    @router.get("", response_class=HTMLResponse)
    def index() -> Response:
        """コースの一覧は担当コース（`/`）に 1 つだけ置く。

        以前はここにも一覧があり、採点の入口（`/`）と管理の入口（`/manage`）で
        同じコースが 2 度並んでいた。**入口が 2 つあること自体が構成の
        分かりにくさの元**だったので、古い経路は残したまま集約する。
        """
        return RedirectResponse("/", status_code=303)

    @router.post("/courses")
    def create_course(
        request: Request,
        code: Annotated[str, Form()],
        title: Annotated[str, Form()],
        term_year: Annotated[int, Form()],
        term_division: Annotated[str, Form()],
        profile: Annotated[str, Form()],
        instructors: Annotated[str, Form()],
    ) -> Response:
        """コースを作る。**担当教員を 1 名以上、明示的に指定させる**（#130）。

        指定が無いまま作れてしまうと、誰も教えていないコースができる
        ── 「作成者を機械的に教員にする」だけでは、管理者自身が実際に
        教えるとは限らない（コースを立てるのと教えるのは別の役目）。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        # **学期は選ばせる**（#167）。年度と区分の 2 つの `<select>` から
        # 正準形式を組み立てる。組み立てるのは `aijudge_core.terms` で、
        # ここでは文字列を作らない ── 学期は course_id の素材なので、
        # 形を知っている場所を増やすと、増えた場所の分だけ表記がゆれる。
        try:
            term = format_term(term_year, term_division.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # 名簿の貼り付けと同じ書式（`add_enrolments`）。1 行 1 ID でよい。
        # `parse_roster` は有効な行が 1 つも無ければ自分で例外にする
        # （「1 名以上」の要求はこれで足りる）。
        try:
            entries = parse_roster(instructors, default_role=Role.INSTRUCTOR)
        except (RosterError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        logins = [entry.login for entry in entries]

        with console.database.unit_of_work() as uow:
            unknown = [
                login
                for login in logins
                if uow.identity.find_user_by_login(me.tenant_id, login) is None
            ]
        if unknown:
            # 画面からは既存利用者の指定だけを許す（`add_enrolments` と同じ
            # 規則）。新規作成はパスワード配布が伴うので CLI に回す。
            raise HTTPException(
                status_code=400,
                detail=(
                    f"利用者が未登録です: {', '.join(unknown[:10])}"
                    f"{' ほか' if len(unknown) > 10 else ''}。"
                    "新規作成は `aijudge-admin` で行ってください"
                ),
            )

        try:
            course, _created = ensure_course(
                console.database,
                tenant_id=me.tenant_id,
                code=code.strip(),
                title=title.strip(),
                term=term,
                subject_profile=profile.strip(),
                profiles_dir=console.profiles_dir,
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        with console.database.unit_of_work() as uow:
            auth = AuthService(uow.identity, audit=uow.audit)
            audit = recorder_for(uow, request, me)
            # 作った本人も担当教員にする。でないと自分のコースが見えない
            # （テナント管理者が受講登録なしで全コースに届くようになるまでの
            # 措置。#128 が入ればここは指定した教員だけで足りる）。
            auth.enroll(
                tenant_id=me.tenant_id,
                course_id=course.id,
                user_id=me.user_id,
                role=Role.INSTRUCTOR,
            )
            granted = [me.login]
            for login in logins:
                user = uow.identity.find_user_by_login(me.tenant_id, login)
                if user is not None and user.id != me.user_id:
                    auth.enroll(
                        tenant_id=me.tenant_id,
                        course_id=course.id,
                        user_id=user.id,
                        role=Role.INSTRUCTOR,
                    )
                    granted.append(login)
            # 担当教員の付与はコースの全成績に届く権限である。**1 行にまとめる**
            # ── ここは 1 回の操作なので、人ごとに散らすと後から画面での 1 操作を
            # 復元できない。
            audit.record(
                AuditAction.ENROLLED,
                target_type="course",
                target_id=str(course.id),
                summary=f"担当教員を {len(granted)} 名割り当てた",
                detail={"role": Role.INSTRUCTOR.value, "logins": granted},
            )
            uow.commit()
        return RedirectResponse(f"/courses/{course.id}", status_code=303)

    # -- ローカル利用者の管理（画面から、管理者専用）-----------------------
    #
    # **#127: `admin` にだけ開ける例外。** 利用者の新規作成は
    # `aijudge-admin`（CLI）に限る、という規則（#100 のコメント、
    # `add_enrolments` の docstring）はそのまま ── ここは「画面から
    # 操作できるのが管理者本人に限られるなら、画面共有・端末履歴の
    # リスクは許容できる」という別枠で、一般の教員には開かない。
    #
    # #144 で作成だけの画面から一覧・属性確認・無効化・パスワード再発行へ
    # 広げた。**無効化は削除ではない**（`AuthService.disable` ── 過去の提出と
    # 採点が利用者を参照しているので、消すと成績の履歴が壊れる）。

    @router.get("/users/new", response_class=HTMLResponse)
    def new_user_form(request: Request) -> Response:
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        return templates.TemplateResponse(
            request,
            "manage_new_user.html",
            {"me": me, "trail": _trail(USERS_STEP, ("利用者を作成", None))},
        )

    @router.post("/users")
    def create_user(
        request: Request,
        login: Annotated[str, Form()],
        display_name: Annotated[str, Form()] = "",
        tenant_admin: Annotated[bool, Form()] = False,
    ) -> Response:
        """ローカル利用者を作る。パスワードはここで生成し、**一度だけ**表示する。

        以降どこにも平文を残さない ── `AuthService.issue_token` が API
        トークンでやっているのと同じ約束。コースへの受講登録はここでは
        行わない（既存の受講者一覧画面が、既存利用者を対象にそれをやる）。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        login = login.strip()
        password = generate_password()
        with console.database.unit_of_work() as uow:
            auth = AuthService(uow.identity, audit=uow.audit)
            try:
                principal = auth.register(
                    tenant_id=me.tenant_id,
                    login=login,
                    display_name=display_name.strip() or login,
                    password=password,
                )
            except AuthenticationFailed as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if tenant_admin:
                auth.set_tenant_admin(principal.user_id, admin=True)
            # **平文は記録しない**（画面に一度だけ出すもの・#144）。
            recorder_for(uow, request, me).record(
                AuditAction.USER_CREATED,
                target_type="user",
                target_id=str(principal.user_id),
                summary=f"利用者を作成した（{login}）",
                detail={"login": login, "tenant_admin": bool(tenant_admin)},
            )
            uow.commit()
        return templates.TemplateResponse(
            request,
            "manage_new_user_created.html",
            {
                "me": me,
                "login": login,
                "password": password,
                "tenant_admin": tenant_admin,
                "trail": _trail(USERS_STEP, ("利用者を作成", None)),
            },
        )

    @router.get("/users", response_class=HTMLResponse)
    def user_list(
        request: Request,
        q: str = "",
        local: str = "",
        login_kind: str = "",
        role: str = "",
        saved: str = "",
    ) -> Response:
        """利用者の一覧（#144）。

        絞り込みは前方一致（受講者一覧と同じ作法）。テナントの規模が
        大きくなると、一覧をそのまま読むより ID を打つ方が速くなる。

        **ログイン方式で絞れる**（#326）。パスワードの再発行や無効化の対象に
        なるのはローカル利用者だけなので、その一覧を出せると運用の単位に合う
        （大学アカウントの利用者は Google 側が本人確認を持っている）。逆向き
        ── 大学アカウントだけ ── も要る。片側しか選べない札だったので、
        「SSO で入った人が何人いるか」が数えられなかった。

        **役割でも絞れる**（#326・受講者一覧と同じ）。TA だけ・教員だけを
        見たいとき、学生の中から探すことになっていた。役割はコースごとに
        付くので、ここで見るのは「**いずれかのコースで**その役割を持つ人」で
        ある ── 1 人が教員と TA の両方であることは普通にあり、丸めない
        （`roles_by_user` の注記）。

        `local=1` は `login_kind=local` の旧名。**受け続ける** ── 運用の
        手元に残った URL が黙って全件に戻ると、絞ったつもりの一覧を読む。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        prefix = q.strip()
        kind = login_kind.strip()
        if not kind and local:
            kind = "local"
        if kind not in {"local", "sso"}:
            kind = ""
        # 値は役割の語彙にあるものだけ（無ければ絞らない・受講者一覧と同じ）。
        wanted = role.strip() if role.strip() in {r.value for r in Role} else ""

        with console.database.unit_of_work() as uow:
            users = uow.identity.list_all_users(me.tenant_id)
            roles = uow.identity.roles_by_user(me.tenant_id)
        if prefix:
            users = tuple(user for user in users if user.login.startswith(prefix))
        if kind == "local":
            users = tuple(user for user in users if user.external_id is None)
        elif kind == "sso":
            users = tuple(user for user in users if user.external_id is not None)
        if wanted:
            users = tuple(
                user for user in users if any(r.value == wanted for r in roles.get(user.id, ()))
            )
        return templates.TemplateResponse(
            request,
            "manage_users.html",
            {
                "me": me,
                "trail": _trail(("利用者の一覧", None)),
                "users": users,
                # 行ごとの役割。**役割の語彙の順に並べる** ── 人ごとに順番が
                # 変わると、列を縦に読み比べられない。
                "roles": {
                    user.id: tuple(r for r in Role if r in roles.get(user.id, frozenset()))
                    for user in users
                },
                "q": prefix,
                "login_kind": kind,
                "role": wanted,
                "roles_vocabulary": tuple(Role),
                "filtered": bool(prefix or kind or wanted),
                "saved": SAVED_MESSAGES.get(saved),
            },
        )

    @router.get("/users/{user_id}", response_class=HTMLResponse)
    def user_detail(request: Request, user_id: str, saved: str = "") -> Response:
        """1 人の属性と、どのコースにどの役割で居るか（#144）。"""
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        with console.database.unit_of_work() as uow:
            user = uow.identity.get_user(UserId(user_id))
            if user is None or user.tenant_id != me.tenant_id:
                # 他テナントの利用者は「無い」として扱う（存在を漏らさない）。
                raise HTTPException(status_code=404, detail="利用者が見つかりません")
            # **テナント管理者は受講登録なしで全コースに届く**（#128）。
            # `AuthService.courses_for` はその場合に全コースを返すので、
            # ここでは使わずに実際の受講登録だけを並べる ── 全コースを
            # 並べると、無い `Enrollment` があるように見えてしまう。
            # 「管理者だからすべてに届く」はテンプレート側で明示する。
            rows: list[dict[str, object]] = []
            for course in uow.identity.list_courses_for_user(me.tenant_id, user.id):
                enrollment = uow.identity.find_enrollment(course.id, user.id)
                rows.append(
                    {"course": course, "role": None if enrollment is None else enrollment.role}
                )
        return templates.TemplateResponse(
            request,
            "manage_user_detail.html",
            {
                "me": me,
                "trail": _trail(USERS_STEP, (user.login, None)),
                "user": user,
                "rows": rows,
                "is_self": user.id == me.user_id,
                # ローカル利用者だけがパスワードを持つ。大学アカウントの人に
                # 再発行を出すと、押しても入り口が変わらないものを見せることになる。
                "is_local": user.external_id is None,
                "roles": [role.value for role in GRANTABLE_ROLES],
                "saved": SAVED_MESSAGES.get(saved),
            },
        )

    @router.post("/users/{user_id}/tenant-admin")
    def set_user_tenant_admin(
        request: Request, user_id: str, admin: Annotated[str, Form()] = ""
    ) -> Response:
        """テナント管理者フラグを立てる／外す。

        **コースをまたぐ権限なので、コースの受講者一覧からは配れない**
        （そちらの上限は `GRANTABLE_ROLES`）。ここは管理者専用の画面なので、
        管理者が次の管理者を決められる ── 利用者の作成画面が同じ例外を
        既に開けている（#127）のと同じ扱い。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        if UserId(user_id) == me.user_id:
            # 自分から管理者を外すと、自分では戻せない（無効化と同じ理屈）。
            raise HTTPException(status_code=400, detail="自分自身の管理者権限は変えられません")

        with console.database.unit_of_work() as uow:
            user = uow.identity.get_user(UserId(user_id))
            if user is None or user.tenant_id != me.tenant_id:
                raise HTTPException(status_code=404, detail="利用者が見つかりません")
            AuthService(uow.identity, audit=uow.audit).set_tenant_admin(user.id, admin=bool(admin))
            # 権限の変更は成績に届く（管理者はどのコースの成績にも触れる）。
            # **前後の値を書く** ── 「変えた」だけでは、いま管理者なのが
            # この操作の結果なのか元からなのか、後から読めない。
            recorder_for(uow, request, me).record(
                AuditAction.TENANT_ADMIN_CHANGED,
                target_type="user",
                target_id=str(user.id),
                summary=("管理者権限を与えた" if admin else "管理者権限を外した"),
                detail={
                    "login": user.login,
                    "is_tenant_admin": {"before": user.is_tenant_admin, "after": bool(admin)},
                },
            )
            uow.commit()
        saved = "tenant_admin_granted" if admin else "tenant_admin_revoked"
        return RedirectResponse(f"/manage/users/{user_id}?saved={saved}#saved", status_code=303)

    @router.post("/users/{user_id}/courses/{course_id}/role")
    def set_user_course_role(
        request: Request, user_id: str, course_id: str, role: Annotated[str, Form()]
    ) -> Response:
        """この利用者の、そのコースでの役割を変える。

        コースの受講者一覧（`set_role`）と**同じ規則**を通す ── `admin` は
        画面から配れない。入口が 2 つあるので、規則を写さずに同じ関数を呼ぶ。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        try:
            new_role = Role(role)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"役割が不正です: {role!r}") from None
        _require_grantable(new_role)

        with console.database.unit_of_work() as uow:
            user = uow.identity.get_user(UserId(user_id))
            if user is None or user.tenant_id != me.tenant_id:
                raise HTTPException(status_code=404, detail="利用者が見つかりません")
            existing = uow.identity.find_enrollment(CourseId(course_id), user.id)
            if existing is None:
                # 受講していないコースの役割はここでは作らない（受講登録は
                # コース側の画面の仕事）。
                raise HTTPException(status_code=404, detail="このコースの受講登録がありません")
            AuthService(uow.identity, audit=uow.audit).enroll(
                tenant_id=me.tenant_id,
                course_id=CourseId(course_id),
                user_id=user.id,
                role=new_role,
            )
            recorder_for(uow, request, me).record(
                AuditAction.ENROLLED,
                target_type="user",
                target_id=str(user.id),
                summary=f"コースでの役割を {new_role.value} に変えた",
                detail={
                    "course_id": str(course_id),
                    "login": user.login,
                    "role": {"before": existing.role.value, "after": new_role.value},
                },
            )
            uow.commit()
        return RedirectResponse(f"/manage/users/{user_id}?saved=role#saved", status_code=303)

    @router.post("/users/{user_id}/disable")
    def disable_user(request: Request, user_id: str) -> Response:
        """利用者を無効化する。**削除ではない**（`AuthService.disable`）。"""
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        if UserId(user_id) == me.user_id:
            # 自分を無効化すると、自分の管理者権限で自分を戻せない
            # （復旧手段が CLI だけになる）。画面からは塞ぐ。
            raise HTTPException(status_code=400, detail="自分自身は無効化できません")

        with console.database.unit_of_work() as uow:
            user = uow.identity.get_user(UserId(user_id))
            if user is None or user.tenant_id != me.tenant_id:
                raise HTTPException(status_code=404, detail="利用者が見つかりません")
            AuthService(uow.identity, audit=uow.audit).disable(user.id)
            recorder_for(uow, request, me).record(
                AuditAction.USER_DISABLED,
                target_type="user",
                target_id=str(user.id),
                summary="利用者を無効化した",
                detail={"login": user.login},
            )
            uow.commit()
        return RedirectResponse(f"/manage/users/{user_id}?saved=disabled#saved", status_code=303)

    @router.post("/users/{user_id}/password", response_class=HTMLResponse)
    def reissue_user_password(request: Request, user_id: str) -> Response:
        """パスワードを再発行し、**一度だけ**表示する（#144）。

        `manage_new_user_created.html` が「再発行のみ可能」と書いていた
        その再発行がこれ。平文はレスポンス以外のどこにも残さない。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        password = generate_password()
        with console.database.unit_of_work() as uow:
            user = uow.identity.get_user(UserId(user_id))
            if user is None or user.tenant_id != me.tenant_id:
                raise HTTPException(status_code=404, detail="利用者が見つかりません")
            if user.external_id is not None:
                # 大学アカウントの利用者はパスワードでログインしない。発行しても
                # 使い道が無く、「配ったのに入れない」を生むだけ。
                raise HTTPException(
                    status_code=400,
                    detail="この利用者は大学アカウントでログインします（パスワードはありません）",
                )
            try:
                AuthService(uow.identity, audit=uow.audit).reissue_password(user.id, new=password)
            except AuthenticationFailed as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            # **平文は記録しない。** 画面に一度だけ出すためのもので、
            # 消さない場所に写せば「一度だけ」が嘘になる（#144）。
            recorder_for(uow, request, me).record(
                AuditAction.PASSWORD_REISSUED,
                target_type="user",
                target_id=str(user.id),
                summary="パスワードを再発行した",
                detail={"login": user.login},
            )
            uow.commit()
            login = user.login
        return templates.TemplateResponse(
            request,
            "manage_password_reissued.html",
            {
                "me": me,
                "login": login,
                "password": password,
                "user_id": user_id,
                "trail": _trail(
                    USERS_STEP, (login, f"/manage/users/{user_id}"), ("パスワードの再発行", None)
                ),
            },
        )

    # -- Google OIDC 設定（テナント単位、管理者専用、#124）------------------
    #
    # ログイン画面自体の切替え（`/auth/login` `/auth/callback` の実際の配線・
    # 「大学アカウントでログイン」ボタン）は #125 の範囲。ここは管理者が
    # client_id/secret・許可ドメインを設定する画面だけを持つ。**このリポジトリ
    # は公開物なので、特定機関のドメインや値はどこにもハードコードしない**
    # ── 未設定テナントでは #125 のログイン画面が Google ボタンを出さない。

    @router.get("/campus-networks", response_class=HTMLResponse)
    def campus_networks_form(request: Request, saved: str = "") -> Response:
        """学内と見なすアドレス範囲（#333）。**テナント管理者だけ。**

        **いまの接続元と、その判定を一緒に出す。** 範囲は人が調べて書き写す
        値で、書き写しは間違う ── しかも間違いは「試験当日に全員が提出でき
        ない」という形でしか現れない。学内の端末でこの画面を開けば、登録
        すべき値がその場で読めて、保存した設定が自分に効くかも見える。

        **判定は保存済みの値で行う。** 入力欄の中身で判定すると、保存前の
        文字列で「学内です」と出して、保存に失敗しても気づけない。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)
        with console.database.unit_of_work() as uow:
            settings = uow.identity.get_campus_networks(me.tenant_id)
        cidrs = () if settings is None else settings.cidrs
        source = source_ip_of(request)
        return templates.TemplateResponse(
            request,
            "manage_campus_networks.html",
            {
                "me": me,
                "cidrs": cidrs,
                "text": "\n".join(cidrs),
                "source_ip": source,
                "access": campus_access(source, cidrs),
                "saved": SAVED_MESSAGES.get(saved),
                "saved_key": saved,
                "trail": _trail(("学内ネットワーク", None)),
            },
        )

    @router.post("/campus-networks")
    def save_campus_networks(request: Request, cidrs: Annotated[str, Form()] = "") -> Response:
        """範囲を保存する。**読めない行はその場で突き返す。**

        保存してから「読めないものは無視されました」と言うより、書き直して
        もらうほうがよい ── 無視された行があることに気づかないまま試験を
        迎えるのが、いちばん高くつく形である。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        lines = [line.strip() for line in cidrs.splitlines() if line.strip()]
        if len(lines) > MAX_CIDRS:
            raise HTTPException(status_code=400, detail=f"範囲は {MAX_CIDRS} 件までです")
        # **1 行ずつ確かめる。** `parse_cidrs` は読めるものだけを返す設計
        # なので、ここで数を比べても「どれが読めなかったか」は言えない。
        bad = [line for line in lines if not parse_cidrs([line])]
        if bad:
            raise HTTPException(
                status_code=400,
                detail="範囲として読めない行があります: " + "、".join(bad[:5]),
            )

        with console.database.unit_of_work() as uow:
            before = uow.identity.get_campus_networks(me.tenant_id)
            uow.identity.save_campus_networks(
                CampusNetworkSettings(tenant_id=me.tenant_id, cidrs=tuple(lines))
            )
            # **誰がいつ変えたかを残す。** ここを変えると、誰が提出できるかが
            # 変わる ── 締切と同じ性質の値である（ADR 0013 と同じ理由）。
            recorder_for(uow, request, me).record(
                AuditAction.CAMPUS_NETWORKS_UPDATED,
                target_type="tenant",
                target_id=str(me.tenant_id),
                summary=f"学内ネットワークを変えた（{len(lines)} 件）",
                detail={
                    "before": list(() if before is None else before.cidrs),
                    "after": lines,
                },
            )
            uow.commit()
        return RedirectResponse("/manage/campus-networks?saved=campus_networks#saved", 303)

    @router.get("/oidc-settings", response_class=HTMLResponse)
    def oidc_settings_form(request: Request, saved: str = "") -> Response:
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)
        with console.database.unit_of_work() as uow:
            settings = uow.identity.get_oidc_settings(me.tenant_id)
        return templates.TemplateResponse(
            request,
            "manage_oidc_settings.html",
            {
                "me": me,
                "settings": settings,
                "saved": bool(saved),
                "default_login_label": DEFAULT_LOGIN_LABEL,
                "trail": _trail(("Google ログイン設定", None)),
            },
        )

    @router.post("/oidc-settings", response_class=HTMLResponse)
    def oidc_settings_save(
        request: Request,
        client_id: Annotated[str, Form()],
        client_secret: Annotated[str, Form()] = "",
        allowed_domains: Annotated[str, Form()] = "",
        issuer: Annotated[str, Form()] = "",
        login_label: Annotated[str, Form()] = "",
    ) -> Response:
        """保存する。

        **`client_secret` は空欄なら既存の値を引き継ぐ。** 画面には保存後
        二度と平文を出さない ── ただし `/manage/users` の生成パスワードの
        「一度だけ表示」とは違い、こちらは管理者自身が入力した値なので、
        常に伏せておくだけでよい（見せ直す約束はしない）。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        domains = tuple(
            sorted(
                {d.strip() for d in allowed_domains.replace(",", "\n").splitlines() if d.strip()}
            )
        )

        with console.database.unit_of_work() as uow:
            existing = uow.identity.get_oidc_settings(me.tenant_id)

            def error(message: str) -> Response:
                return templates.TemplateResponse(
                    request,
                    "manage_oidc_settings.html",
                    {
                        "me": me,
                        "settings": existing,
                        "error": message,
                        "default_login_label": DEFAULT_LOGIN_LABEL,
                        "trail": _trail(("Google ログイン設定", None)),
                    },
                )

            secret = client_secret.strip() or (existing.client_secret if existing else "")
            if not secret:
                return error("client secret を入力してください")
            if not domains:
                return error("許可ドメインを 1 つ以上入力してください")
            # **画面の `maxlength` は検証ではない**（#212）。curl や
            # `maxlength` を無視する客体から 65 字が来ると、開いている
            # unit_of_work の中で `ValidationError` が出て素の 500 になる
            # ── すぐ上で用意している `error()` を通さずに終わってしまう。
            label = login_label.strip()
            if len(label) > LOGIN_LABEL_MAX:
                return error(f"ログインボタンの文言は {LOGIN_LABEL_MAX} 字までです")

            uow.identity.save_oidc_settings(
                OidcSettings(
                    tenant_id=me.tenant_id,
                    client_id=client_id.strip(),
                    client_secret=secret,
                    allowed_domains=domains,
                    issuer=issuer.strip() or "https://accounts.google.com",
                    # **空欄は既定に戻す。** 機関の語彙を入れる欄なので、
                    # 消したときに前の機関名が残り続けてはいけない（#209）。
                    login_label=label or DEFAULT_LOGIN_LABEL,
                )
            )
            uow.commit()
        return RedirectResponse("/manage/oidc-settings?saved=1#saved", status_code=303)

    # -- 科目プロファイル（管理者専用、#146）--------------------------------
    #
    # **参照されているプロファイルは読み取り専用のまま。** 1 つのプロファイルは
    # 複数のコースの雛形になりうるので、書き換えると自分が担当していない
    # コースの採点まで変わる（ADR 0002 の「コードと同じ扱いでレビューを通す」）。
    #
    # 未参照のものだけ直接編集・改名でき、参照中のものへの唯一の操作は
    # 「複製して編集」。この判定は `aijudge_admin.profiles` が持ち、ここは
    # 画面と繋ぐだけ ── 判定を画面側に写すと、2 つが食い違ったときに
    # 「画面では編集できるのに保存が拒否される」形で現れる。

    def _used_by(request: Request, names: list[str]) -> dict[str, tuple]:
        """名前ごとの参照コース。**テナントを越えて調べる**（profiles.py 参照）。"""
        console = _console(request)
        with console.database.unit_of_work() as uow:
            return {name: uow.identity.list_courses_using_profile(name) for name in names}

    @router.get("/subjects", response_class=HTMLResponse)
    def subject_list(request: Request, saved: str = "") -> Response:
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        names = [path.stem for path in sorted(console.profiles_dir.glob("*.yaml"))]
        profiles = list_profiles(console.profiles_dir, _used_by(request, names))
        return templates.TemplateResponse(
            request,
            "manage_subjects.html",
            {
                "me": me,
                "trail": _trail(("科目プロファイル", None)),
                "profiles": profiles,
                "saved": SAVED_MESSAGES.get(saved),
            },
        )

    @router.get("/subjects/{name}", response_class=HTMLResponse)
    def subject_detail(request: Request, name: str, saved: str = "") -> Response:
        """1 件の全文。**参照中なら読み取り専用で、参照コースをその場に出す。**

        単に灰色にするだけでは、教員は「バグか」「権限が無いだけか」を
        判別できない（#146）。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        try:
            text = read_profile_text(name, console.profiles_dir)
        except AdminError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        used_by = _used_by(request, [name])[name]
        return templates.TemplateResponse(
            request,
            "manage_subject.html",
            {
                "me": me,
                "trail": _trail(SUBJECTS_STEP, (name, None)),
                "name": name,
                "text": text,
                "used_by": used_by,
                "editable": not used_by,
                "saved": SAVED_MESSAGES.get(saved),
            },
        )

    @router.post("/subjects/{name}", response_class=HTMLResponse)
    async def save_subject(request: Request, name: str) -> Response:
        """全文を保存する。**参照中なら `AdminError` で拒否される**（profiles.py）。"""
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        form = await request.form()
        text = str(form.get("text") or "")
        used_by = _used_by(request, [name])[name]
        try:
            save_profile_text(
                name,
                text,
                profiles_dir=console.profiles_dir,
                registry=EvaluatorRegistry().load_installed(),
                used_by=used_by,
            )
        except AdminError as exc:
            # **書きかけを捨てない。** 直して出し直せるように、送られてきた
            # 全文をそのまま返す（検証エラーで消えると打ち直しになる）。
            return templates.TemplateResponse(
                request,
                "manage_subject.html",
                {
                    "me": me,
                    "trail": _trail(SUBJECTS_STEP, (name, None)),
                    "name": name,
                    "text": text,
                    "used_by": used_by,
                    "editable": not used_by,
                    "error": str(exc),
                },
                status_code=400,
            )
        _record_profile_change(
            console,
            request,
            me,
            action=AuditAction.PROFILE_UPDATED,
            name=name,
            summary=f"科目プロファイル {name} を書き換えた",
            detail={"sha256": _digest(text), "bytes": len(text.encode())},
        )
        return RedirectResponse(
            f"/manage/subjects/{name}?saved=profile_saved#saved", status_code=303
        )

    @router.post("/subjects/{name}/duplicate")
    def duplicate_subject(
        request: Request,
        name: str,
        new_name: Annotated[str, Form()],
        description: Annotated[str, Form()] = "",
    ) -> Response:
        """複製して、複製先の編集画面へ渡す（#146 の「複製して編集」）。"""
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        try:
            duplicate_profile(
                name,
                new_name.strip(),
                profiles_dir=console.profiles_dir,
                description=description.strip() or None,
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        _record_profile_change(
            console,
            request,
            me,
            action=AuditAction.PROFILE_DUPLICATED,
            name=new_name.strip(),
            summary=f"科目プロファイル {name} を {new_name.strip()} に複製した",
            detail={"source": name},
        )
        return RedirectResponse(
            f"/manage/subjects/{new_name.strip()}?saved=profile_duplicated#saved", status_code=303
        )

    @router.post("/subjects/{name}/rename")
    def rename_subject(request: Request, name: str, new_name: Annotated[str, Form()]) -> Response:
        """改名する。**未参照のときだけ**（参照中は採点が止まるので拒否）。"""
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        try:
            rename_profile(
                name,
                new_name.strip(),
                profiles_dir=console.profiles_dir,
                used_by=_used_by(request, [name])[name],
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        _record_profile_change(
            console,
            request,
            me,
            action=AuditAction.PROFILE_UPDATED,
            name=new_name.strip(),
            summary=f"科目プロファイル {name} を {new_name.strip()} に改名した",
            detail={"renamed_from": name},
        )
        return RedirectResponse(
            f"/manage/subjects/{new_name.strip()}?saved=profile_renamed#saved", status_code=303
        )

    # -- 自分のパスワードを変える --------------------------------------------
    #
    # `/users/new` と違って**本人なら誰でも**使える（管理者限定にしない）。
    # ローカル利用者（#127 で管理者が作った学習者・教員アカウント）は、
    # 発行時の一度きり表示パスワードのまま使い続けるほかなく、それを変える
    # 手段がどこにも無かった。
    #
    # **SSO 利用者には出さない**（#180）。当初は「無意味だが害も無い」と
    # 出し分けをしなかったが、Google で入った利用者の `password_hash` は
    # 誰も知らない捨て値（`AuthService.login_with_google`）なので、この画面は
    # 「現在のパスワードが違います」しか返しようがない ── 変えられるはずの
    # ものが変えられない、という誤った案内になる。ヘッダのリンクを隠すだけ
    # では境界にならないので、経路の側でも 404 を返す。

    @router.get("/account/password", response_class=HTMLResponse)
    def account_password_form(request: Request) -> Response:
        from .app import require_principal

        me = require_principal(request)
        _require_local_account(me)
        return templates.TemplateResponse(
            request,
            "manage_account_password.html",
            {"me": me, "trail": _trail(("パスワード変更", None))},
        )

    @router.post("/account/password", response_class=HTMLResponse)
    def account_password_change(
        request: Request,
        current_password: Annotated[str, Form()],
        new_password: Annotated[str, Form()],
        new_password_confirm: Annotated[str, Form()],
    ) -> Response:
        from .app import SESSION_COOKIE, require_principal

        me = require_principal(request)
        _require_local_account(me)
        console = _console(request)

        def error(message: str) -> Response:
            return templates.TemplateResponse(
                request,
                "manage_account_password.html",
                {"me": me, "error": message, "trail": _trail(("パスワード変更", None))},
            )

        # `hash_password`/`change_password` 自体は長さを見ない
        # （生成パスワードは記号を含まないことがある）。ここで最低限だけ弾く。
        if len(new_password) < 12:
            return error("新しいパスワードは 12 文字以上にしてください")
        if new_password != new_password_confirm:
            return error("新しいパスワードが一致しません")

        with console.database.unit_of_work() as uow:
            auth = AuthService(uow.identity, audit=uow.audit)
            try:
                auth.change_password(me.user_id, current=current_password, new=new_password)
            except AuthenticationFailed as exc:
                return error(str(exc))
            uow.commit()
        # change_password は自分のセッションも含めて全部失効させる
        # （乗っ取られていた場合の復旧手段がこれしかない、が本人の操作でも
        # 例外なく効く）。ログイン画面へ戻し、Cookie も捨てる。
        # ローカル利用者向けの画面（#125）。ログインし直す先も隠し経路。
        response = RedirectResponse("/auth/local?changed=1", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @router.get("/courses/{course_id}", response_class=HTMLResponse)
    def course_settings(
        request: Request,
        course_id: str,
        saved: str = "",
        tasks: int = 0,
        skipped: int = 0,
    ) -> Response:
        """コースの**共通設定** ── 基本情報・受講者・自動確定・提出形式・採点設定。

        **画面の見出し・区画名・帯の項目名は「共通設定」で揃える**（#300）。
        帯が「問題セット」と呼びながらこの画面へ送っていたので、押した先が
        目当ての画面かどうかを見出しで確かめられなかった。

        課題（日程・一括確定・追加）はここには出さない。問題セットのページに
        分けてある（`unit_settings`）。1 枚に積むと、教員は「ex03 の締切を
        直す」ために縦に長い画面を目で探すことになる。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        # 複製の直後は**何件写したかを告げる**（#170）。件数だけを黙って
        # 変えると、教員は自分が何を手に入れたのか画面から確かめられない。
        note = None
        if saved == "course_duplicated":
            note = (
                f"複製しました（課題 {tasks} 件）。日程は写していません —— "
                "元の学期の日付を持ち込むと、初日から全課題が締切済みになるためです。"
                "問題セットのページで日程を入れてください。受講登録は空です。"
            )
            if skipped:
                note += f" 未承認などの理由で写さなかった課題が {skipped} 件あります。"
        return _course_page(request, me, course, saved=saved, note=note)

    def _course_page(
        request: Request,
        me,
        course,
        *,
        saved: str = "",
        note: str | None = None,
        trial=None,
        values=None,
    ) -> Response:
        """コースの共通設定の画面。

        採点設定もここに出す。**別のページに分けない** ── 雛形からの差分は
        コースの設定の一部で、他の設定と行き来しながら決めるものだから。
        """
        console = _console(request)
        registry = EvaluatorRegistry().load_installed()
        base = template_of(course, console.profiles_dir)
        current = values if values is not None else course.grading_overrides
        try:
            applied = effective_profile(base, current, registry)
        except OverrideError:
            applied = base

        with console.database.unit_of_work() as uow:
            enrollments = uow.identity.list_enrollments(course.id)
            # 学習者の提出があるコースは消せない（#156）。**何件あるかを
            # 先に出す** ── 押してから断られるより、押す前に理由が読める方が
            # よい。教員の動作確認（trial・#108）は数えない（消せる）。
            # **数えるだけなので、行は持ってこない**（#219）。`is_trial` が
            # 列になったので SQL の側で分けられる。
            learner_submissions = uow.submissions.count_for_course(course.id).learner
        people_count = len(enrollments)

        return templates.TemplateResponse(
            request,
            "manage_course.html",
            {
                "me": me,
                "course": course,
                "section": {"label": "共通設定", "href": f"/manage/courses/{course.id}"},
                "saved": note or SAVED_MESSAGES.get(saved),
                "saved_key": saved,
                # コースの削除は作成と同じくテナント管理者だけ（#156）。
                # 担当教員には出さない ── 押せないものを見せない。
                "is_admin": _is_admin(request, me),
                "learner_submissions": learner_submissions,
                # 受付のときに書き起こされるもの（#351）。**観点が読むのは
                # その本文である**ことを、評価器を割り当てる画面で言う。
                # 上書きを当てた後の `applied` で見る ── 書き起こすかどうかは
                # コースの上書きで変わりうる。
                "transcription": _transcription_note(
                    applied, course.upload_suffixes or DEFAULT_UPLOAD_SUFFIXES
                ),
                "people_count": people_count,
                "role_counts": _role_counts(enrollments),
                # シラバスの本文は Markdown。素のまま出すと見出しも箇条書きも
                # 記号のまま並ぶ（課題文で実際に起きた・`statement.py`）。
                "description_html": (
                    render_markdown(course.description) if course.description else None
                ),
                "suffix_groups": SUFFIX_GROUPS,
                "course_suffixes": course.upload_suffixes or DEFAULT_UPLOAD_SUFFIXES,
                # 束の上限（#161）。**画面に書く値をコードから取る** ──
                # 書き写すと、上限を変えた日に画面だけが古い数字を出す。
                "bundle_max_mb": MAX_ARCHIVE_BYTES // (1024 * 1024),
                # 複製先の学期の選択肢（#170）。作成フォームと同じ語彙から
                # 取る（#167）── 画面ごとに書き写すと、片方だけが古くなる。
                "term_years": offered_years(),
                "term_divisions": DIVISIONS,
                # 共通ルーブリック。未設定なら組み込みの既定を出して、
                # **いま何が使われているか**を見えるようにする。
                "rubric_rows": rubric.to_rows(
                    rubric.from_stored(course.rubric)
                    if course.rubric
                    else _default_rubric_criteria()
                ),
                "rubric_is_default": not course.rubric,
                # **既定の観点が、この科目では誰にも採点できない場合。**
                #
                # 組み込みの既定は「正しさ（テスト実行）＋読みやすさ」で、
                # 正しさの担当は `code_test_runner` である。テスト実行を走らせ
                # ない科目（レポートなど）のコースがこの既定のままだと、その
                # 観点は**恒久的に未採点**になり、総点は伏せられる（ADR 0015）。
                # 設定はどこも正しく見えるのに点が出ない、という形で現れる
                # ので、画面から理由が読めない ── だからここで言う。
                #
                # **黙って別の既定に差し替えない。** 何を問うかは科目の中身で、
                # 機械が決めてよいことではない（設計原則 P5）。言うだけにする。
                "rubric_default_is_unscorable": (
                    not course.rubric and CODE_TEST_RUNNER not in applied.deterministic
                ),
                # -- 採点設定 --
                "base": base,
                "profile": applied,
                "overrides": current,
                "changed": diff(base, current),
                "locked": LOCKED_KEYS,
                # インストール済みから選ばせる。**自由入力にしない** ── 存在しない
                # 名前を書けると、その科目の採点が恒久的に失敗する。
                "deterministic": _evaluator_rows(registry, EvaluatorKind.DETERMINISTIC),
                "ai_evaluators": _evaluator_rows(registry, EvaluatorKind.AI),
                # 提出の遵守が見る値（#316）。選択肢は拡張子の表から作る。
                "artifact_kinds": _artifact_kind_rows(),
                "languages": sorted(LANGUAGES),
                "trial": trial,
            },
        )

    @router.post("/courses/{course_id}/units")
    def open_unit(
        request: Request,
        course_id: str,
        unit: Annotated[str, Form()] = "",
    ) -> Response:
        """回のページを開く（無ければ空の回として開く）。

        新しい回を作る導線がこれである。**保存は伴わない** ── 回は課題が
        持つ属性であって、それ自体の記録は無い。最初の 1 問を足した時点で
        回が実在する。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        key = quote(unit.strip() or "_", safe="")
        return RedirectResponse(f"/manage/courses/{course_id}/units/{key}", status_code=303)

    @router.get("/courses/{course_id}/units/{unit}", response_class=HTMLResponse)
    def unit_settings(request: Request, course_id: str, unit: str, saved: str = "") -> Response:
        """**1 回ぶん**の設定 ── その回の課題の締切、一括確定、課題の追加。

        まとまりの鍵は `unit`（`overview.unit_key`）。教員が用があるのは
        たいてい「いまの回」で、その単位で開けることが構成の分かりやすさに
        直結する。
        """
        from .app import require_principal

        me = require_principal(request)
        course, role = _require_reader(request, me, CourseId(course_id))
        console = _console(request)

        # 課題ごとの未確定件数。**画面に出す。** 自動確定を設定したつもりで
        # cron を仕掛け忘れても、件数が減らないことで気づける。
        pending = pending_counts(console.database, course.id)
        now = datetime.now(UTC)
        with console.database.unit_of_work() as uow:
            units = load_units(uow, course, pending=pending, now=now)
        # **知らない鍵でも 404 にしない。** 課題を 1 問も持たない回は
        # 「まだ何も無い回」であって存在しない回ではなく、ここが最初の
        # 1 問を足す場所になる。404 にすると新しい回を作る導線が無くなる。
        key = _normalized_unit(unit)
        group = find_unit(units, key) or empty_unit(key, course)

        # **同じ題名が 2 件あることを出す。** 以前は「テストケースを付けるには
        # 作り直してください」と書いてあり、そのとおりにすると別の課題が増えて
        # 同じ問題が 2 件並んだ（#43）。並んでいること自体を画面に出さないと、
        # 提出と採点がどちらに付いたのかを教員が追えない。
        titles: dict[str, int] = {}
        for task, _version in group.tasks:
            titles[task.title] = titles.get(task.title, 0) + 1

        # 課題ごとの知識要素（#292）。**付いていない課題の成績は習熟度に
        # 記録されない**ので、無いことも一覧で分かるようにする。
        with console.database.unit_of_work() as uow:
            kc_keys = {task.id: _kc_keys_of(uow, version) for task, version in group.tasks}
            # **学習者に出ているのはどれか**（#319）。一覧は `latest_version` を
            # 並べるので、承認待ちや却下済みの**新しい版**があると、その状態を
            # 課題そのものの状態として出していた ── 改訂を却下しただけで
            # 「却下済み — 出題されません」と出るが、**1 つ前の承認済みは出て
            # いる**。画面が学習者の見え方と食い違う。
            published = {
                task.id: uow.tasks.latest_published_version(task.id) for task, _ in group.tasks
            }

        rows = []
        for task, version in group.tasks:
            rows.append(
                {
                    "task": task,
                    "version": version,
                    "kc_keys": kc_keys.get(task.id, ()),
                    "duplicate_title": titles.get(task.title, 0) > 1,
                    # 取り下げた課題。**消えてはいない**ので一覧には残す（#51）。
                    "withdrawn": task.withdrawn,
                    "test_cases": len(version.test_cases),
                    # 自動採点できるか。できない課題は AI 観点だけで、
                    # 教員の確定が前提になる（ADR 0008）。
                    "auto_graded": bool(version.test_cases),
                    "evaluators": sorted(
                        {c.evaluator_id for c in version.criteria if c.evaluator_id}
                    ),
                    "unfinalized": pending.get(task.id, 0),
                    # **承認済みと同じ見た目で並べない。** 一覧は
                    # `latest_version` をレビュー状態で絞らないので、生成した
                    # ままの課題もここに出る。印が無いと、教員は「この回は
                    # 5 問」と読むのに出題されるのは承認済みのぶんだけになる。
                    # 新しい版の状態。**課題そのものの状態ではない**（#319）。
                    "in_review": (version.provenance.review_state is ReviewState.IN_REVIEW),
                    "rejected": version.provenance.review_state is ReviewState.REJECTED,
                    # 学習者に出ている版（無ければ None）。ラベルはこれで決める
                    # ── 出ているかどうかは、承認済みの版があるかどうかである。
                    "published": published.get(task.id),
                    # 訂正フォームの初期値。読みやすさの観点の重みは
                    # 版の中にあるので、そこから取り出す。
                    "readability_weight": next(
                        (c.weight for c in version.criteria if c.code == "readability"), 0.0
                    ),
                    # 訂正フォームで直せるように、いまの観点を行にして渡す。
                    "rubric_rows": rubric.to_rows(version.criteria),
                }
            )

        # **TA には読むだけの画面を出す**（#102）。編集用のテンプレートを
        # `{% if %}` で隠して回さない ── 隠し忘れが 1 か所あれば、そこから
        # 押せてしまう。出す形が違うなら、テンプレートも分ける。
        if not _can_edit(role):
            return templates.TemplateResponse(
                request,
                "unit_readonly.html",
                {
                    "me": me,
                    "course": course,
                    "section": {
                        "label": group.label,
                        "href": f"/manage/courses/{course.id}/units/{group.key}",
                    },
                    "unit": group,
                    "tasks": rows,
                    "course_grace": course.auto_finalize_after_minutes,
                },
            )

        return templates.TemplateResponse(
            request,
            "manage_unit.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": group.label,
                    "href": f"/manage/courses/{course.id}/units/{group.key}",
                },
                "unit": group,
                "tasks": rows,
                # 「丸ごと片付ける」を押す前に出す内訳（#59）。**押してから
                # でないと分からないのでは確認にならない** ── 1 回の操作で
                # 課題ごとに削除か取り下げかが変わる。
                "clear_plan": _clear_plan(console, course, group),
                # 学内限定の表示に要る（#333）。**範囲が未設定なら効いて
                # いない**ので、そう書く ── 切り替えただけで守られていると
                # 読まれるのが、いちばん高くつく誤解である。
                "campus_configured": _campus_configured(console, me),
                # 試験の一括採点（#67）。待機中の件数と、落ちたジョブ。
                **_exam_state(console, course, group, now),
                "min_reason": MIN_JUSTIFICATION_LENGTH,
                # コースの既定。問題セットで指定しなければこれが効く。
                "course_grace": course.auto_finalize_after_minutes,
                "saved": SAVED_MESSAGES.get(saved),
                "saved_key": saved,
                "suffix_groups": SUFFIX_GROUPS,
                "course_suffixes": course.upload_suffixes or DEFAULT_UPLOAD_SUFFIXES,
                # 共通ルーブリック。未設定なら組み込みの既定を出して、
                # **いま何が使われているか**を見えるようにする。
                "rubric_rows": rubric.to_rows(
                    rubric.from_stored(course.rubric)
                    if course.rubric
                    else _default_rubric_criteria()
                ),
                "rubric_is_default": not course.rubric,
                # **生成は登録済み KC からの選択だけ**（`aijudge_admin.kc` の
                # 規則 4）。引退したものは選ばせない。
                # **このコースが使う範囲だけ出す。** 同じ名前空間を複数の
                # コースが使うほど関係のない候補が増え、C の科目に
                # `cs.python.*` が並ぶ。見にくいだけでなく、誤った知識要素を
                # 課題に付けられるということでもある（設計原則 P6）。
                "kcs": _course_kcs(console, course),
                "difficulties": [d.value for d in Difficulty],
                # ルーブリックの編集で、観点に指名できる評価器を出す。
                "deterministic": _evaluator_rows(
                    EvaluatorRegistry().load_installed(), EvaluatorKind.DETERMINISTIC
                ),
                "PROVISIONAL": GradeWindow.PROVISIONAL,
                "last_task": (
                    console.last_task[1]
                    if console.last_task is not None and console.last_task[0] == str(course.id)
                    else None
                ),
                "last_finalize": (
                    console.last_finalize[1]
                    if console.last_finalize is not None
                    and console.last_finalize[0] == str(course.id)
                    else None
                ),
            },
        )

    @router.post("/courses/{course_id}/units/{unit}/retry-failed")
    def retry_failed(request: Request, course_id: str, unit: str) -> Response:
        """上限まで落ちたジョブを流し直す（#80）。

        **一覧を出しておいて手が無いのは中途半端である。** #67 で失敗した
        ジョブを問題セットのページに出したが、直したあとに戻す手立てが
        無かったので、SQL を叩くしかなかった。

        **教員が押したときだけ動く。** 自動で戻すなら上限を設けた意味が無い
        ── 直っていない原因で回し続けることになる。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        group = _unit_group(console, course, unit)

        version_ids = {str(version.id) for _task, version in group.tasks}
        now = datetime.now(UTC)
        with console.database.unit_of_work() as uow:
            # **上限の内側だけを再試行しない**（#230）。
            submissions = uow.submissions.list_for_versions(sorted(version_ids))
            failed = uow.jobs.failed_for([s.id for s in submissions])
            for job in failed:
                uow.jobs.update(job.retried(now))
            uow.commit()

        console.last_release = (str(course.id), len(failed))
        return RedirectResponse(
            f"/manage/courses/{course_id}/units/{group.key}?saved=retried#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/units/{unit}/grade-now")
    def grade_now(request: Request, course_id: str, unit: str) -> Response:
        """いま溜まっている提出を採点する（#67）。**何度でも押せる。**

        試験モードを解除しない ── 押したあとに出された提出はまた採点開始時刻
        まで待つ。試験中に「ここまでの提出が採点を通るか」を確かめられ、
        延長しても勝手に始まらない、という両方が要るため。

        やっているのは**寝かせてあるジョブの時刻を早めることだけ**で、採点
        そのものはワーカーが走らせる（提出時と同じ経路）。ここで採点を
        起動すると、レビュー画面が採点器を呼ぶ構造に戻る（ADR 0007）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        group = _unit_group(console, course, unit)

        version_ids = {str(version.id) for _task, version in group.tasks}
        now = datetime.now(UTC)
        with console.database.unit_of_work() as uow:
            # **一括採点の対象を打ち切られた一覧から決めない**（#230）。
            # 古い順に切るので、落ちるのは試験直後の提出だった ── しかも
            # 押す前の件数も同じ一覧から出ていたので、画面の中では
            # 矛盾せず、取りこぼしはどこにも現れなかった。
            submissions = uow.submissions.list_for_versions(sorted(version_ids))
            released = uow.jobs.release_waiting([s.id for s in submissions], now)
            uow.commit()

        console.last_release = (str(course.id), released)
        return RedirectResponse(
            f"/manage/courses/{course_id}/units/{group.key}?saved=released#saved",
            status_code=303,
        )

    async def _store_statement_image(request: Request, course, upload: UploadFile, alt: str) -> str:
        """画像をストアに置き、**課題文に貼り付ける 1 行**を返す。

        **受け口は課題の編集画面の 1 つだけ**（#300）。以前は共通設定にも
        フォームの受け口があり、そこで作った 1 行を教員が手で貼る形だったが、
        書きかけの問題文を置いて往復することになる ── 課題の編集は 1 つの
        フォームで、保存するまで何も残らない。
        """
        console = _console(request)
        payload = await upload.read()
        try:
            name = images.new_name(payload, upload.filename or "")
            key = images.storage_key(str(course.id), name)
        except images.ImageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # 同じ中身なら同じ鍵。貼り直しても増えない。
        if not console.store.exists(key):
            console.store.put(key, payload)
        # 表示幅を書いておく。**縮めずに貼ると写真 1 枚で画面が埋まり**、
        # 課題文の続きが画面外へ出る。幅だけを書くので縦横比は保たれる
        # （`aijudge_authoring.images`）。教員はあとから数字を直せる。
        return images.markdown_for(str(course.id), name, alt, width=images.display_width(payload))

    @router.post("/courses/{course_id}/images.json")
    async def upload_statement_image_json(
        request: Request,
        course_id: str,
        upload: UploadFile,
        alt: Annotated[str, Form()] = "",
    ) -> Response:
        """課題の編集画面から上げる画像（#64）。**貼り付けまでやる。**

        **課題文に貼る画像の受け口はこれ 1 つである**（#300）。別の画面で
        1 行を出して手で貼らせる形も持っていたが、書きかけの問題文を置いて
        別の画面へ行き、戻ってきて貼る、という往復になる ── その間に編集中の
        内容は失われる（課題の編集は 1 つのフォームで、保存するまで何も
        残らない）。**同じ画面で受け取り、カーソル位置に差し込む。**

        画面を遷移させないので JSON で返す。差し込みは呼び出し側の
        JavaScript（`base.html`）が行う。JavaScript が無い場合はここへ来ない
        ── 課題の編集画面はそう言う（`<noscript>`）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        line = await _store_statement_image(request, course, upload, alt)
        return JSONResponse({"markdown": line})

    @router.post("/courses/{course_id}/statement-preview", response_class=HTMLResponse)
    async def statement_preview(
        request: Request,
        course_id: str,
        statement: Annotated[str, Form()] = "",
    ) -> Response:
        """書きかけの問題文を、**学習者に出るのと同じ描画**で返す（#105）。

        ブラウザ側で Markdown を描かない。課題文の描画は `html=False`・数式は
        サーバ側 MathML・画像の幅は属性プラグイン（`statement.py`）で、
        JavaScript の Markdown 実装は**普通の文章では一致し、間違いが起きる
        ところ（数式・画像・生 HTML の遮断）でだけ食い違う**。それではプレビュー
        ではなく別の意見になる。

        返すのは本文の断片だけ。保存はしない ── 版が上がるのは「保存」で行う。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        return HTMLResponse(render_statement(statement))

    @router.get("/courses/{course_id}/images/{name}")
    def statement_image(request: Request, course_id: str, name: str) -> Response:
        """課題文に貼られた画像（教員側）。

        **学習者アプリと同じ経路を持つ**（`/images/...`）。課題文は両方の
        画面に出るので、絶対 URL を埋め込むとどちらかのホスト名が課題文に
        焼き付く。相対パスなら、開いている側が自分で返す。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        try:
            payload = console.store.get(images.storage_key(str(course.id), name))
        except Exception as exc:
            raise HTTPException(status_code=404, detail="画像が見つかりません") from exc
        return Response(
            content=payload,
            media_type=images.content_type(name),
            headers={"Cache-Control": "private, max-age=86400"},
        )

    # -- 束（zip）で課題を入れる（#161）------------------------------------
    #
    # **読み取り → 確認 → 人が保存**（シラバス読み取りと同じ作法）。押した
    # 瞬間に何十件も入る操作にしない ── まとまった投入で怖いのは「押したら
    # 何件変わったか分からない」ことである。
    #
    # 受け取る構造はこのシステム自身の語彙（`aijudge_admin.bundles`）。
    # 移行元の形式をここに持ち込まない ── 一度その形で入口を作り、廃止した。

    @router.get("/courses/{course_id}/units/{unit}/bundle/template")
    def bundle_template(request: Request, course_id: str, unit: str) -> Response:
        """アップロードする一式のひな形を返す（#171）。

        **画面の説明文から構造を組み立てさせない。** 書き写しの失敗は
        「取り込めません」の 1 行になって返り、何を直せばよいかは画面から
        読めない。書ける状態のものを渡せば、そもそも書き写しが要らない。

        **コースに合わせて作る** ── 使える評価器・共通ルーブリックの観点
        コード・このコースが使う知識要素のキーを、`task.yaml` のコメントに
        入れる。知識要素は登録済みのものしか名指しできないので、一覧が
        手元にあるかどうかで書きやすさが変わる。

        権限は取り込みと同じ（担当教員以上・#102）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")

        payload = template_bundle(
            unit=unit,
            evaluators=tuple(profile.deterministic),
            criterion_codes=tuple(
                criterion.code for criterion in rubric.from_stored(course.rubric)
            ),
            kc_keys=tuple(kc.key for kc in _course_kcs(console, course)),
        )
        # **ファイル名は ASCII に留める。** 回の名前は教員が付けるもので、
        # 日本語も入りうる ── `filename*=` の符号化まで持ち込むより、
        # 中身の README で「どの問題セット向けか」を言う方が壊れない。
        stem = unit if unit.isascii() and unit.isprintable() else "bundle"
        return Response(
            content=payload,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{stem}-template.zip"'},
        )

    @router.post("/courses/{course_id}/units/{unit}/bundle", response_class=HTMLResponse)
    async def read_task_bundle(request: Request, course_id: str, unit: str) -> Response:
        """zip を読んで、**何が起きるかを見せる**。まだ保存しない。"""
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        # **まだ課題が 1 問も無い回にも入れられる。** 束で入れるのは
        # 「新しい問題セットを作る」でもあるので、存在しない回として断ると
        # 導線が無くなる（`unit_settings` と同じ扱い・`empty_unit`）。
        group = _unit_group_or_empty(console, course, unit)

        form = await request.form()
        upload = form.get("archive")
        payload = await upload.read() if hasattr(upload, "read") else b""
        try:
            bundled = read_bundle(payload)
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        # 画像は**この時点で置く**。鍵は中身のハッシュなので、置き直しても
        # 増えず、保存しなかった場合に残るのは孤児のファイルだけ（教員が
        # 画像だけ上げて貼らなかった場合と同じ状態）。こうしておくと、
        # 確認画面はサーバに一時状態を持たずに済む。
        prepared = []
        for task in bundled:
            spec = task.spec
            statement = spec.statement
            for image in task.images:
                try:
                    name = images.new_name(image.payload, image.name)
                    key = images.storage_key(str(course.id), name)
                except images.ImageError as exc:
                    raise HTTPException(
                        status_code=400, detail=f"{task.leaf}/{image.name}: {exc}"
                    ) from None
                if not console.store.exists(key):
                    console.store.put(key, image.payload)
                # 束の中の相対リンクを、置いた先のリンクに書き換える。
                statement = statement.replace(
                    f"images/{image.name}", images.url_for(str(course.id), name)
                )
            # 鍵の前半は**この画面が持つ問題セット**が決める（#70）。
            prepared.append(
                spec.model_copy(
                    update={
                        "statement": statement,
                        "key": _compose_key(group.unit or "", spec.key),
                        "unit": group.unit,
                        "session": group.session,
                    }
                )
            )

        try:
            planned = plan_bundle(
                console.database,
                course_id=course.id,
                subject_profile=course.subject_profile,
                authored_by=me.user_id,
                specs=tuple(prepared),
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        return templates.TemplateResponse(
            request,
            "manage_bundle_preview.html",
            {
                "me": me,
                "course": course,
                "unit": group,
                "planned": planned,
                # 確認画面から保存へ渡す。**サーバに一時状態を持たない** ──
                # 持つと、2 人が同時に上げたときにどちらの束か分からなくなる。
                "payload": json.dumps([item.spec.model_dump(mode="json") for item in planned]),
            },
        )

    @router.post("/courses/{course_id}/units/{unit}/bundle/confirm")
    def save_task_bundle(
        request: Request,
        course_id: str,
        unit: str,
        specs: Annotated[str, Form()],
        approved: Annotated[str, Form()] = "",
    ) -> Response:
        """確認した束を保存する。**保存は既存の経路（`save_task`）を通す。**

        承認済みで入れるかどうかはここで選ぶ（#161）。既定は未承認 ──
        中身は他所で書かれたもので、このシステムでは誰も読んでいない。
        承認するまで学習者には出ない（#48）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        group = _unit_group_or_empty(console, course, unit)

        try:
            # **送られてきたものを信じない。** 確認画面を経由しても、POST は
            # 手で作れる（`TaskSpec` の検証をもう一度通す）。
            parsed = tuple(TaskSpec.model_validate(item) for item in json.loads(specs))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"読み取れませんでした: {exc}") from None

        review_state = ReviewState.APPROVED if approved.strip() else ReviewState.IN_REVIEW
        for spec in parsed:
            try:
                save_task(
                    console.database,
                    course_id=course.id,
                    spec=spec,
                    subject_profile=course.subject_profile,
                    authored_by=me.user_id,
                    revise=True,
                    course_rubric=course.rubric,
                    review_state=review_state,
                )
            except AdminError as exc:
                raise HTTPException(status_code=409, detail=f"{spec.key}: {exc}") from None

        saved_key = "bundle_saved" if review_state is ReviewState.APPROVED else "bundle_in_review"
        return RedirectResponse(
            f"/manage/courses/{course.id}/units/{group.key}?saved={saved_key}#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/units/{unit}/clear")
    def clear_unit_route(request: Request, course_id: str, unit: str) -> Response:
        """問題セットを丸ごと片付ける。**課題ごとに削除か取り下げか**（#59）。

        規則は `aijudge_admin.tasks` に置いてある ── 画面と CLI の両方から
        使うので、どちらが正しいかを問わずに済むよう 1 か所にする。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        group = _unit_group(console, course, unit)

        try:
            report = clear_unit(console.database, course_id=course.id, unit=group.unit)
        except AdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # **何がどうなったかを持ち帰る。** 件数だけでは、消えたのか残ったのか
        # 教員に分からない。
        console.last_clear = (str(course.id), report)
        if not report.withdrawn and not report.untouched:
            # 全部消えたのでセットのページはもう無い。**コースのトップへ戻す**
            # （#82）── 以前は `/manage/courses/{id}`、つまり共通ルーブリックや
            # 遅延減点を設定する画面に飛ばしていた。消した直後に見たいのは
            # 「このコースに何が残っているか」であって、設定ではない。
            return RedirectResponse(f"/courses/{course_id}", status_code=303)
        return RedirectResponse(
            f"/manage/courses/{course_id}/units/{group.key}?saved=unit_cleared#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/units/{unit}/campus-only")
    def set_unit_campus_only(
        request: Request,
        course_id: str,
        unit: str,
        campus_only: Annotated[str, Form()] = "",
    ) -> Response:
        """**問題セットを学内からだけ受け付けるかを切り替える**（#333）。

        日程と同じで、値はセット単位で決めてその中の全課題に入れる
        （`_update_unit`）── 課題ごとに違うと、同じ回の中で「出せる課題と
        出せない課題」が混ざり、学習者にはその理由が読めない。

        **何を学内と見なすかはここでは決めない。** 範囲はテナント管理者が
        設定する（`/manage/campus-networks`）── 機関の属性であって、
        コースごとに違うものではない。
        """
        return _update_unit(
            request,
            course_id,
            unit,
            update={"campus_only": bool(campus_only.strip())},
            saved="campus_only",
        )

    @router.post("/courses/{course_id}/units/{unit}/schedule")
    def set_unit_schedule(
        request: Request,
        course_id: str,
        unit: str,
        opens_at: Annotated[str, Form()] = "",
        submissions_open_at: Annotated[str, Form()] = "",
        due_at: Annotated[str, Form()] = "",
        grading_starts_at: Annotated[str, Form()] = "",
        accepts_until: Annotated[str, Form()] = "",
    ) -> Response:
        """**問題セットの日程。その中の全課題に同じ値を入れる。**

        課題ごとに違う締切を持てると、同じセットの中で締切がずれ、学習者にも
        教員にも「この回はいつまでか」が言えなくなる。日程はセットの性質で
        あって課題の性質ではない。

        締切の判定は `Submission.submitted_at`（提出確定の時刻）で行う。
        ここで入れる値がその基準になる。
        """
        return _update_unit(
            request,
            course_id,
            unit,
            update={
                "opens_at": _parse_when(opens_at),
                "submissions_open_at": _parse_when(submissions_open_at),
                "due_at": _parse_when(due_at),
                # 試験の問題セット（#67）。空なら提出と同時に採点する。
                "grading_starts_at": _parse_when(grading_starts_at),
                # 受付終了（#73）。空なら締切後も無期限に受け付ける。
                "accepts_until": _parse_when(accepts_until),
            },
            saved="schedule",
        )

    @router.post("/courses/{course_id}/units/{unit}/auto-finalize")
    def set_unit_grace(
        request: Request,
        course_id: str,
        unit: str,
        after_minutes: Annotated[str, Form()] = "",
    ) -> Response:
        """この問題セットの自動確定までの猶予（分）。空ならコースの既定に戻す。

        **日程とは別のフォームにする。** 締切を直しに来たときに猶予まで
        書き換えてしまう（あるいはその逆）事故を、フォームの単位で防ぐ。
        """
        return _update_unit(
            request,
            course_id,
            unit,
            update={"auto_finalize_after_minutes": _parse_minutes(after_minutes)},
            saved="grace",
        )

    @router.post("/courses/{course_id}/units/{unit}/number")
    def set_unit_number(
        request: Request,
        course_id: str,
        unit: str,
        session: Annotated[str, Form()] = "",
    ) -> Response:
        """回番号。**並べ替えと表示のためだけの値**で、採点には効かない。

        日程と混ぜない ── 効き方が違うものを 1 つの保存ボタンにまとめると、
        何が変わったのか教員に分からない。
        """
        number = int(session) if session.strip() else None
        return _update_unit(request, course_id, unit, update={"session": number}, saved="number")

    @router.post("/courses/{course_id}/auto-finalize")
    def set_auto_finalize(
        request: Request,
        course_id: str,
        after_minutes: Annotated[str, Form()] = "",
    ) -> Response:
        """締切から成績を自動確定するまでの猶予（分）。**コースの既定である。**

        問題セットで指定があればそちらが勝つ（`grace_minutes`）。

        空なら自動確定しない。**既定はそれ**で、教員が明示的に入れて初めて
        自動確定が始まる。既定で自動確定させると、設定を知らない教員の
        コースで成績が勝手に閉じる。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        minutes = _parse_minutes(after_minutes)

        with console.database.unit_of_work() as uow:
            uow.identity.save_course(
                course.model_copy(update={"auto_finalize_after_minutes": minutes})
            )
            recorder_for(uow, request, me).record(
                AuditAction.COURSE_UPDATED,
                target_type="course",
                target_id=course_id,
                summary="自動確定までの猶予を変えた",
                detail={
                    "auto_finalize_after_minutes": {
                        "before": course.auto_finalize_after_minutes,
                        "after": minutes,
                    }
                },
            )
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}?saved=course_grace#saved", status_code=303
        )

    def _read_body(text: str, upload: UploadFile | None, payload: bytes | None) -> str:
        """本文を決める。ファイルが選ばれていればそちらを読む。

        ファイルからの読み取りは Markdown に均して返す（`syllabus.to_markdown`）。
        そのままだと行が細かく割れていて、教員が直すにも読みにくい。
        """
        if upload is not None and upload.filename and payload:
            if len(payload) > MAX_SYLLABUS_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"ファイルが大きすぎます（上限 {MAX_SYLLABUS_BYTES // 1024} KB）",
                )
            try:
                return read_document(payload, Path(upload.filename).suffix)
            except SyllabusError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
        return text.strip()

    def _propose(console, course, body: str):
        """本文から候補を作る。名前空間と既存の体系を添えて渡す。"""
        if len(body) < 40:
            raise HTTPException(
                status_code=400,
                detail="シラバスの本文を貼り付けるか、PDF を選んでください（40 文字以上）",
            )
        profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
        namespaces = allowed_namespaces(profile)
        existing = [
            kc.key
            for kc in list_for_namespaces(console.database, namespaces, include_deprecated=False)
        ]
        try:
            result = SyllabusReader().propose(
                body, namespaces=namespaces, existing_keys=tuple(existing)
            )
        except Exception as exc:  # 生成の失敗は運用の事象。理由を画面に返す。
            raise HTTPException(
                status_code=502,
                detail=f"候補を作れませんでした（S6 が止まっている可能性があります）: {exc}",
            ) from exc
        return result.proposal, namespaces, existing

    # -- コースの基本情報 --------------------------------------------------

    @router.get("/courses/{course_id}/basics", response_class=HTMLResponse)
    def basics(request: Request, course_id: str, saved: str = "") -> Response:
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        return templates.TemplateResponse(
            request,
            "manage_basics.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": "コースの基本情報",
                    "href": f"/manage/courses/{course.id}/basics",
                },
                "title_value": course.title,
                "description_value": course.description or "",
                "saved": SAVED_MESSAGES.get(saved),
            },
        )

    @router.post("/courses/{course_id}/basics/read", response_class=HTMLResponse)
    async def read_basics(
        request: Request,
        course_id: str,
        text: Annotated[str, Form()] = "",
        upload: UploadFile | None = None,
    ) -> Response:
        """シラバスを読んで、本文を Markdown に整えて欄に入れる。**保存はしない。**

        読み取りと登録を分ける ── 出てきたものを教員が確かめてから保存する
        （モデルが整えた文であって、シラバスそのものではない）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))

        payload = await upload.read() if upload is not None and upload.filename else None
        body = _read_body(text, upload, payload)
        if len(body) < 40:
            raise HTTPException(
                status_code=400,
                detail="PDF を選ぶか、本文を貼り付けてください（40 文字以上）",
            )
        note = "読み取りました"
        try:
            basics = SyllabusReader().read_basics(body)
            markdown = basics.markdown.strip() or body
            title = basics.title.strip()
        except Exception:
            # **モデルが使えなくても読み取りは終わらせる。** 体裁が
            # 整わないだけで、本文は取れている（規則での整形に落とす）。
            markdown, title = to_markdown(body), ""
            note = "読み取りました（本文の整形は簡易版です — S6 に繋がりませんでした）"

        return templates.TemplateResponse(
            request,
            "manage_basics.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": "コースの基本情報",
                    "href": f"/manage/courses/{course.id}/basics",
                },
                "title_value": title or course.title,
                "description_value": markdown,
                # 読み取りの結果は読み取りのボタンの隣に出す。**登録の合図とは
                # 別にする** ── どちらの操作が効いたのか分からなくなる。
                "read_note": note,
                "saved": None,
            },
        )

    @router.post("/courses/{course_id}/basics/apply")
    def apply_basics(
        request: Request,
        course_id: str,
        title: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
    ) -> Response:
        """基本情報を保存する。**コードと学期は変えない。**

        あの 2 つは（テナント・コード・学期）でコースの同一性を作っており、
        変えると別のコースになる。作り直しは新しいコースの追加で行う。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        name = title.strip()
        if not name:
            raise HTTPException(status_code=400, detail="コース名を入れてください")
        with console.database.unit_of_work() as uow:
            uow.identity.save_course(
                course.model_copy(
                    update={"title": name, "description": description.strip() or None}
                )
            )
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/basics?saved=basics#saved", status_code=303
        )

    # -- 知識要素の候補 ----------------------------------------------------

    @router.post("/courses/{course_id}/kc/candidates", response_class=HTMLResponse)
    def propose_kcs(request: Request, course_id: str) -> Response:
        """コースの基本情報から知識要素の候補を出す。**登録はしない。**

        **本文を貼り直させない。** 材料はコースが既に持っている
        （`Course.description` ── 基本情報のページでシラバスから読み取って
        保存したもの）。同じ本文をもう一度貼らせると、2 つの経路で入った
        別々のシラバスがコースの中に並ぶことになり、どちらが本当か分からない。

        候補は候補のまま知識要素のページに戻す。**ここから直接は登録しない**
        （`aijudge_admin.kc` の規則 4 ── AI には KC を作らせない）。教員が
        1 件ずつ追加フォームに取り込み、確かめてから登録する（`draft_candidate`）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        if not (course.description or "").strip():
            # 材料が無い。**候補を出せないことと、候補が無いことは違う。**
            raise HTTPException(
                status_code=400,
                detail=(
                    "コースの基本情報が空です。先に「基本情報」でシラバスを"
                    "読み取るか、概要・到達目標を書いてください。"
                ),
            )

        profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
        namespaces = allowed_namespaces(profile)
        vocabulary = list_for_namespaces(console.database, namespaces, include_deprecated=False)
        existing = [kc.key for kc in vocabulary]
        # 題名も渡す。「プログラミング及び実習 II」だけで分野が決まることは
        # ないが、本文が到達目標だけのときに科目の見当が付く。
        body = f"# {course.title}\n\n{course.description}"
        try:
            # **候補は登録済みの語彙から選ばれる**（2026-09-13 決定）。一覧に
            # 無いキーは `SyllabusReader` の関門が落とし、理由付きで返る。
            result = SyllabusReader().propose(
                body, namespaces=namespaces, existing_keys=tuple(existing)
            )
        except Exception as exc:  # 生成の失敗は運用の事象。理由を画面に返す。
            raise HTTPException(
                status_code=502,
                detail=f"候補を作れませんでした（S6 が止まっている可能性があります）: {exc}",
            ) from exc
        return _kc_page(request, me, course, proposal=result.proposal, discarded=result.discarded)

    @router.post("/courses/{course_id}/delete")
    def delete_course_route(request: Request, course_id: str) -> Response:
        """コースを消す。**学習者の提出が無いときだけ**（#156）。

        権限はコースの作成と同じ**テナント管理者**。担当教員には開けない
        ── コースを消すのは、そのコースの中の操作ではない。

        規則は `aijudge_admin.courses` に置いてある（画面と CLI の両方から
        使うので、どちらが正しいかを問わずに済むよう 1 か所にする）。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        with console.database.unit_of_work() as uow:
            course = uow.identity.get_course(CourseId(course_id))
        if course is None or course.tenant_id != me.tenant_id:
            raise HTTPException(status_code=404, detail="コースが見つかりません")

        try:
            delete_course(console.database, course_id=course.id, artifact_store=console.store)
        except AdminError as exc:
            # **なぜ消せないかをその場に出す**（提出が何件あるか）。
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # 消したコースの画面はもう無いので、担当コースの一覧へ戻す。
        return RedirectResponse("/?saved=course_deleted#saved", status_code=303)

    @router.post("/courses/{course_id}/duplicate")
    def duplicate_course_route(
        request: Request,
        course_id: str,
        code: Annotated[str, Form()],
        title: Annotated[str, Form()],
        term_year: Annotated[int, Form()],
        term_division: Annotated[str, Form()],
    ) -> Response:
        """コースを複製する（#170）。

        権限はコースの作成・削除と同じ**テナント管理者** ── コースを作る
        操作である。

        規則は `aijudge_admin.course_copy` に置いてある（何を引き継ぎ、何を
        引き継がないかは運用の判断で、画面の都合ではない）。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_admin(request, me)
        console = _console(request)

        with console.database.unit_of_work() as uow:
            course = uow.identity.get_course(CourseId(course_id))
        if course is None or course.tenant_id != me.tenant_id:
            raise HTTPException(status_code=404, detail="コースが見つかりません")

        try:
            term = format_term(term_year, term_division.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            copied = duplicate_course(
                console.database,
                source_id=course.id,
                code=code.strip(),
                title=title.strip(),
                term=term,
                authored_by=me.user_id,
                profiles_dir=console.profiles_dir,
                artifact_store=console.store,
            )
        except AdminError as exc:
            # **なぜ複製できないかをその場に出す**（同じコードと学期など）。
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        with console.database.unit_of_work() as uow:
            # 複製した本人を担当教員にする。受講登録は引き継がないので、
            # ここで入れないと**自分が作ったコースが自分に見えない**
            # （作成の経路と同じ理由・#130）。
            AuthService(uow.identity, audit=uow.audit).enroll(
                tenant_id=me.tenant_id,
                course_id=copied.course.id,
                user_id=me.user_id,
                role=Role.INSTRUCTOR,
            )
            recorder_for(uow, request, me).record(
                AuditAction.COURSE_UPDATED,
                target_type="course",
                target_id=str(copied.course.id),
                summary=f"コースを複製した（{course.code} → {copied.course.code}）",
                detail={"source_course_id": str(course.id), "term": term},
            )
            uow.commit()

        # 複製後にすることは日程の入力と設定の確認なので、その入口に落とす。
        return RedirectResponse(
            f"/manage/courses/{copied.course.id}"
            f"?saved=course_duplicated&tasks={copied.tasks}&skipped={len(copied.skipped)}#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/rubric")
    async def save_course_rubric(request: Request, course_id: str) -> Response:
        """コースの共通ルーブリック。**新しい課題がこれを引き継ぐ。**

        既にある課題は変わらない ── 出題済みの版は書き換えない（P8）。
        個別に直したい課題は、その課題の訂正から観点を宣言する。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        form = await request.form()
        try:
            criteria = rubric.parse(_rubric_from_form(form))
            aggregation = _aggregation_from_form(form) or Aggregation.OR
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        with console.database.unit_of_work() as uow:
            uow.identity.save_course(
                course.model_copy(
                    update={
                        "rubric": tuple(c.model_dump() for c in criteria),
                        "rubric_aggregation": aggregation,
                    }
                )
            )
            # ルーブリックは採点の基準そのもの。**観点の中身は書かない**
            # （長く、`detail` の上限に収まらない）── 何観点になったかと
            # 集約の仕方だけ残し、中身は課題の版が持つ（P8）。
            recorder_for(uow, request, me).record(
                AuditAction.COURSE_UPDATED,
                target_type="course",
                target_id=course_id,
                summary=f"共通ルーブリックを保存した（{len(criteria)} 観点）",
                detail={
                    "criteria": {"before": len(course.rubric), "after": len(criteria)},
                    "aggregation": {
                        "before": getattr(course.rubric_aggregation, "value", None),
                        "after": aggregation.value,
                    },
                },
            )
            uow.commit()
        return RedirectResponse(f"/manage/courses/{course_id}?saved=rubric#saved", status_code=303)

    @router.post("/courses/{course_id}/upload-formats")
    def set_upload_formats(
        request: Request,
        course_id: str,
        suffix: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI の複数値
    ) -> Response:
        """コースの既定の提出形式。**課題ごとの指定がこれを上書きする。**

        コースで一度決めておけば個々の課題では触らずに済み、レポート 1 問だけ
        PDF を許す、といった例外は課題側で足せる（`uploads.allowed_suffixes`）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        suffixes = normalize_suffixes(suffix)
        if not suffixes:
            raise HTTPException(status_code=400, detail="形式を 1 つ以上選んでください")
        with console.database.unit_of_work() as uow:
            uow.identity.save_course(course.model_copy(update={"upload_suffixes": suffixes}))
            uow.commit()
        return RedirectResponse(f"/manage/courses/{course_id}?saved=formats#saved", status_code=303)

    @router.post("/courses/{course_id}/tasks/{task_id}/finalize")
    def finalize_remaining(
        request: Request,
        course_id: str,
        task_id: str,
        justification: Annotated[str, Form()] = "",
    ) -> Response:
        """この課題の未確定分をまとめて確定する。

        **根拠説明を必須にする。** 学習者にそのまま表示される。個別に読んで
        いない成績を確定させる操作なので、何を根拠にそうしたのかが残らないと
        学習者は何も分からない（設計原則 P4 を一括操作にも適用する）。

        未対応の異議申立は確定しない。そこは 1 件ずつ読むべきものとして
        待ち行列に残す。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        text = justification.strip()
        if len(text) < MIN_JUSTIFICATION_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"確定の根拠を {MIN_JUSTIFICATION_LENGTH} 文字以上で書いてください"
                    "（学習者に表示されます）"
                ),
            )

        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            if task is None or task.course_id != CourseId(course_id):
                raise HTTPException(status_code=404, detail="課題が見つかりません")

        try:
            outcome = finalize_task(
                console.database,
                task_id=TaskId(task_id),
                actor_id=me.user_id,
                justification=text,
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # 表示のために保持する。**コースを添える**（Console は全利用者で共有で、
        # 添えないと別コースの教員に他コースの課題名が出る）。
        console.last_finalize = (str(course_id), outcome)
        return RedirectResponse(_unit_href(course_id, task), status_code=303)

    @router.post("/courses/{course_id}/units/{unit}/finalize")
    def finalize_unit(
        request: Request,
        course_id: str,
        unit: str,
        justification: Annotated[str, Form()] = "",
    ) -> Response:
        """問題セットの未確定分を、その中の全課題についてまとめて確定する。

        **根拠説明を必須にする。** 学習者にそのまま表示される。個別に読んで
        いない成績を確定させる操作なので、何を根拠にそうしたのかが残らないと
        学習者は何も分からない（設計原則 P4 を一括操作にも適用する）。

        未対応の異議申立は確定しない。そこは 1 件ずつ読むべきものとして
        「再確認の依頼」に残す。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        text = justification.strip()
        if len(text) < MIN_JUSTIFICATION_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"確定の根拠を {MIN_JUSTIFICATION_LENGTH} 文字以上で書いてください"
                    "（学習者に表示されます）"
                ),
            )

        key = _normalized_unit(unit)
        with console.database.unit_of_work() as uow:
            task_ids = [
                task.id
                for task in uow.tasks.list_for_course(CourseId(course_id))
                if unit_key(task) == key
            ]
        if not task_ids:
            raise HTTPException(status_code=404, detail="この問題セットには課題がありません")

        try:
            outcomes = finalize_tasks(
                console.database,
                task_ids=task_ids,
                actor_id=me.user_id,
                justification=text,
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # 表示のために保持する。**コースを添える**（Console は全利用者で共有で、
        # 添えないと別コースの教員に他コースの課題名が出る）。
        console.last_finalize = (str(course_id), _merged(outcomes))
        return RedirectResponse(f"/courses/{course_id}/finalize", status_code=303)

    @router.post("/courses/{course_id}/tasks")
    async def add_task(
        request: Request,
        course_id: str,
        statement: Annotated[str, Form()],
        key: Annotated[str, Form()] = "",
        key_suffix: Annotated[str, Form()] = "",
        unit: Annotated[str, Form()] = "",
        position: Annotated[str, Form()] = "",
        readability_weight: Annotated[str, Form()] = "0.3",
        suffix: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI の複数値
        formats: Annotated[str, Form()] = "",
        no_auto_tests: Annotated[str, Form()] = "",
        test_case_count: Annotated[str, Form()] = "5",
    ) -> Response:
        """課題を 1 件足す。

        **保存の中身は API と同じ経路を通る**（`aijudge_admin.save_task`）。
        経路ごとに組み立て方が分かれると、「画面から作った課題だけ観点が
        1 つ足りない」が起きる。実際に起きた ── 廃止した zip 取り込みは
        `readability_weight` が 0.0 固定で、画面から入れた課題には AI 観点が
        付かなかった。

        テストケースはここでは**貼らせない**。1 件ずつ入力させる画面にすると、
        実在する規模（1 課題 7 件 × 48 課題）で現実的でない。まとまった
        投入は API を使う。

        **科目が `code_test_runner` を宣言していれば、代わりに生成する。**
        テストケースの無い課題は正しさの観点が AI 判定に落ちる
        （`TaskSpec.auto_graded`）ので、テスト実行で確定できるはずの科目でも
        全課題が教員の確定待ちになる。生成物は**参照解答と一緒に作らせて門を
        通し、承認待ちで保存する**（`aijudge_admin.test_cases`）。

        「自動テストを使わない」を選べば従来どおり ── C の科目にも設計を問う
        記述課題はあり、そこに自動テストを強いる理由が無い。

        **日程は指定させない。** 問題セットの値をそのまま引き継ぐ ── 課題
        ごとに違う締切を持てると、同じセットの中で締切がずれる。変えたい
        ときはセットの日程を変える（`set_unit_schedule`）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        # **キーの前半は問題セットが決める。** 回のページから追加する限り
        # `ex02/p8` の `ex02/` は動かず、教員が打つのは `p8` だけである。
        # 打たせると `ex2/p8` のような取り違えが混ざり、鍵は同一性そのもの
        # なので、取り違えたぶんは別の課題として増える。
        full_key = key.strip() or _compose_key(unit.strip(), key_suffix.strip())
        if not full_key:
            raise HTTPException(status_code=400, detail="課題キーを入力してください")

        # 日程と回番号は問題セットから引き継ぐ。空のセット（最初の 1 問）は
        # まだ日程を持たないので、そのまま空で入る。
        with console.database.unit_of_work() as uow:
            siblings = [
                task
                for task in uow.tasks.list_for_course(course.id)
                if unit_key(task) == quote(unit.strip() or "_", safe="")
            ]
        head = siblings[0] if siblings else None

        # **テストで確定できる科目なら、テストケースを用意する。**
        profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
        wants_tests = CODE_TEST_RUNNER in profile.deterministic and not no_auto_tests.strip()
        generated = None
        generation_failed = False
        if wants_tests:
            try:
                generated = TestCaseWriter().write(
                    statement,
                    language=_language_of(profile),
                    count=int(test_case_count or 5),
                )
            except Exception:
                # **課題を作れなくしない**（設計原則 P2）。S6 が止まっている
                # あいだ作問が止まると、教員は授業の準備そのものができない。
                # テストケースの無い課題として保存し、**そうなったことを言う**
                # ── 黙って落とすと、テスト実行で確定する課題を作ったつもりの
                # まま学期を過ごすことになる。
                generation_failed = True

        # 知識要素（#292）。付けるものはコースの範囲にも入れる。
        components = _kcs_from_form(console, course, await request.form())

        try:
            spec = TaskSpec(
                key=full_key,
                statement=statement,
                unit=unit.strip() or None,
                position=int(position) if position.strip() else None,
                readability_weight=float(readability_weight or 0.0),
                knowledge_components=components,
                reference_solution=None if generated is None else generated.reference_solution,
                test_cases=(
                    ()
                    if generated is None
                    else tuple(
                        TestCaseSpec(name=case.name, input=case.input, expected=case.expected)
                        for case in generated.test_cases
                    )
                ),
            )
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"課題の指定が不正です: {exc}") from None

        try:
            saved = save_task(
                console.database,
                course_id=course.id,
                spec=spec,
                subject_profile=course.subject_profile,
                authored_by=me.user_id,
                course_rubric=course.rubric,
                # **生成したなら承認待ちにする**（P5）。門は「参照解答とテストが
                # 整合している」までしか言わず、問題文の意図と合っているかは
                # 見ていない。人が一度見るまで出題しない。
                generated_by=None if generated is None else generated.model,
                generation_prompt_version=None if generated is None else generated.prompt_id,
            )
        except AdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # 門 1・門 2 を通し、結果を残す（生成課題と同じ・ADR 0008）。
        if generated is not None:
            _record_gates(console, profile, saved.version)

        # 日程と回番号は問題セットから引き継ぐ。`TaskSpec` を通さないのは、
        # あれが API の語彙でもあり、日程を課題単位で受け取る口を増やすと
        # 「セットで揃える」という規則が守られない経路ができるため。
        # 提出形式は課題ごとの性質。日程と違い、最初の 1 問でも指定できる。
        accepted = _chosen_suffixes(suffix, formats, course)
        if head is None:
            with console.database.unit_of_work() as uow:
                uow.tasks.save_task(saved.task.model_copy(update={"accepted_suffixes": accepted}))
                uow.commit()
        else:
            with console.database.unit_of_work() as uow:
                uow.tasks.save_task(
                    saved.task.model_copy(
                        update={
                            "session": head.session,
                            "opens_at": head.opens_at,
                            "submissions_open_at": head.submissions_open_at,
                            "due_at": head.due_at,
                            "auto_finalize_after_minutes": head.auto_finalize_after_minutes,
                            "accepted_suffixes": accepted,
                        }
                    )
                )
                uow.commit()

        console.last_task = (str(course.id), saved)
        # テストで確定できる科目なのにテストケースが無いなら、そう言う。
        if saved.version.test_cases or CODE_TEST_RUNNER not in profile.deterministic:
            landed = "task"
        elif generation_failed:
            landed = "task_generation_failed"
        else:
            landed = "task_without_tests"
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{saved.task.id}/edit?saved={landed}#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/units/{unit}/generate")
    def generate_task(
        request: Request,
        course_id: str,
        unit: str,
        key_suffix: Annotated[str, Form()],
        kc: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI の複数値
        difficulty: Annotated[str, Form()] = "standard",
        instructions: Annotated[str, Form()] = "",
        test_cases: Annotated[str, Form()] = "5",
        readability_weight: Annotated[str, Form()] = "0.3",
    ) -> Response:
        """AI に課題を 1 つ作らせる。**承認するまで出題されない**（P5）。

        **KC は登録済みからの選択だけ。** モデルはもっともらしいキーを
        いくらでも作るので、自由入力にすると体系が静かに荒れる
        （`aijudge_admin.kc` の規則 4）。

        `avoid_similar_to` にはこのコースの既存課題を入れる ── 「似せない」
        材料が無いと、既存課題の言い換えが出てくる。

        生成物はここでは保存するだけで、門・解答可能性・重複の検査は
        `aijudge-authoring` が担う（ADR 0008）。ここが返すのは候補であって
        課題ではない。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        chosen = tuple(k.strip() for k in kc if k.strip())
        if not chosen:
            raise HTTPException(status_code=400, detail="知識要素を 1 つ以上選んでください")
        try:
            # 選択肢は登録済みから出しているが、直接叩かれる経路もある。
            assert_registered(console.database, chosen, course_keys=course.knowledge_components)
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        key = _normalized_unit(unit)
        profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
        with console.database.unit_of_work() as uow:
            siblings = [
                task
                for task in uow.tasks.list_for_course(CourseId(course_id))
                if unit_key(task) == key
            ]
            # **似せないための材料。** 既存課題の本文を渡す（学習者のデータは
            # 含まないので、外部モデルにも渡してよい・設計原則 P7）。
            avoid = []
            for task in uow.tasks.list_for_course(CourseId(course_id)):
                version = uow.tasks.latest_version(task.id)
                if version is not None:
                    avoid.append(version.statement)

        head = siblings[0] if siblings else None
        full_key = _compose_key(head.unit if head else unit, key_suffix.strip())
        if not full_key:
            raise HTTPException(status_code=400, detail="課題キーを入力してください")

        try:
            blueprint = Blueprint(
                knowledge_components=chosen,
                subject_profile=course.subject_profile,
                # **コースの範囲を渡す。** KC は「何を問うか」を決めるが、
                # 「どこまでを既習として書いてよいか」は決めない。空なら
                # 節ごと出さない（`aijudge_admin.drafting._course_section`）。
                course_title=course.title,
                course_outline=course.description or "",
                difficulty=Difficulty(difficulty),
                language=_language_of(profile),
                instructions=tuple(
                    line.strip() for line in instructions.splitlines() if line.strip()
                ),
                avoid_similar_to=tuple(avoid[:20]),
                test_case_count=int(test_cases or 5),
            )
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"生成の指定が不正です: {exc}") from None

        try:
            result = TaskDrafter().draft(blueprint, key=full_key)
        except Exception as exc:  # 生成の失敗は運用の事象。画面に理由を返す。
            raise HTTPException(
                status_code=502,
                detail=f"課題を生成できませんでした（S6 が止まっている可能性があります）: {exc}",
            ) from exc

        spec = result.spec.model_copy(
            update={"readability_weight": float(readability_weight or 0.0)}
        )
        # **課題にはしない。下書きとして置く**（#321）。課題にすると、そこで
        # 同一性（課題キー → 課題 ID）が決まってしまう ── 生成物は提案であって
        # 確定ではないので、名前を含めて承認のときに決められる必要がある（P5）。
        draft = TaskDraftRecord(
            id=new_id("dft"),
            course_id=course.id,
            kind=DraftKind.NEW,
            spec=spec,
            unit=unit.strip() or (head.unit if head is not None else ""),
            generated_by=result.model,
            generation_prompt_version=result.prompt_id,
            created_by=me.user_id,
            created_at=datetime.now(UTC),
            # **門を通して記録する**（ADR 0008・#267）。判断材料が無いまま
            # 承認を求めない。走らせられない環境では None のままになる。
            checks=_gate_report(profile, spec, course, me.user_id),
            subject_profile=course.subject_profile,
            readability_weight=float(readability_weight or 0.0),
        )
        with console.database.unit_of_work() as uow:
            uow.tasks.save_draft(draft)
            uow.commit()

        # **承認する場所へ送る。** 課題はまだ無いので、問題セットへ戻しても
        # そこには何も増えていない（増えるのは承認したとき・#84）。
        return RedirectResponse(
            f"/manage/courses/{course_id}/drafts?saved=generated#saved", status_code=303
        )

    def _kcs_from_form(console, course, form) -> tuple[str, ...]:
        """フォームの知識要素（#292）。**コースが使うものからしか選べない**（#322）。

        以前は、課題に付けた知識要素をコースの範囲にも足していた（付ける＝
        このコースが使う）。それは**コースの設計を課題の側から膨らませる**
        ことになる ── コースが何を教えるかは知識要素の画面で決めることで、
        1 問ずつの編集の副産物にしてよいものではない。範囲の外のキーが来たら
        断る（画面は範囲内しか出さないので、来るのは API か古い画面である）。

        キーは登録済みの語彙のものだけ（2026-09-13 決定: 画面から語彙は
        増やさない）。
        """
        chosen = tuple(dict.fromkeys(str(v).strip() for v in form.getlist("kc") if str(v).strip()))
        try:
            assert_registered(
                console.database,
                chosen,
                # **範囲の検査も同じ関門に通す**（`aijudge_admin.kc`）── 画面で
                # 絞るだけにすると、API 経由の投入が素通りする。
                course_keys=course.knowledge_components,
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return chosen

    def _save_revision(
        console,
        me,
        course,
        task,
        version,
        *,
        statement,
        criteria,
        position,
        accepted,
        aggregation=None,
        reference_solution=None,
        test_cases=(),
        generated_by=None,
        generation_prompt_version=None,
        knowledge_components=None,
        review_state=None,
    ):
        """課題を直して新しい版を作る。訂正と「共通に戻す」で共有する。

        課題キーは変えられない ── 同一性の鍵で、変えれば別の課題になる。
        保存済みの版から取り出す（`TaskVersion.source_key`）。

        `knowledge_components` が None なら**いまの版の知識要素を引き継ぐ**
        （#292）。渡さない経路（問題文だけ直す等）で空にすると、訂正のたびに
        Q-matrix が黙って消え、その課題の成績から習熟度が動かなくなる ──
        観点・テストと同じ形の取りこぼしが、知識要素にも残っていた。
        """
        if knowledge_components is None:
            with console.database.unit_of_work() as uow:
                knowledge_components = _kc_keys_of(uow, version)
        try:
            spec = TaskSpec(
                key=_key_of(task, version),
                statement=statement,
                unit=task.unit,
                session=task.session,
                position=position,
                # **画面で編集した観点が勝つ。** 観点を宣言する課題では
                # `readability_weight` は使わない（両方書けるとどちらが効くのか
                # 読めない）。
                criteria=criteria,
                # None なら「コースに従う」。**画面の「コースに従う」と、
                # コースと同じ値を選ぶのは別の状態**で、前者はコースを変えれば
                # 追随し、後者はしない。
                aggregation=aggregation,
                reference_solution=reference_solution,
                test_cases=test_cases,
                knowledge_components=tuple(knowledge_components),
            )
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"課題の指定が不正です: {exc}") from None

        try:
            saved = save_task(
                console.database,
                course_id=course.id,
                spec=spec,
                # **課題が自分のプロファイルを持つ**（#195・#264）。ここは
                # 既にある課題を直す経路なので、コースの値を渡すと採点の
                # され方が黙って変わる ── 混在コース（レポートとプログラム）
                # では、問題文を直しただけで C の課題が画像採点になった。
                # コースの値は**新しい課題の既定**であって、既存の課題の
                # 決定ではない。
                subject_profile=version.subject_profile,
                authored_by=me.user_id,
                revise=True,
                course_rubric=course.rubric,
                # **生成したなら承認待ちにする**（P5）。門は「参照解答とテストが
                # 整合している」までしか言わない。承認するまで学習者には
                # 1 つ前の承認済みが出続ける（#48）。
                generated_by=generated_by,
                generation_prompt_version=generation_prompt_version,
                # **出所と承認は別のこと**（#321）。下書きを採用した版は、
                # 書いたのはモデルだが承認は済んでいる ── `generated_by` だけで
                # 承認待ちにすると、採用した瞬間にまた承認待ちになる。
                review_state=review_state,
            )
        except AdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # 日程・提出形式は `TaskSpec` を通らないので、ここで書き戻す。
        with console.database.unit_of_work() as uow:
            uow.tasks.save_task(
                saved.task.model_copy(
                    update={
                        "opens_at": task.opens_at,
                        "submissions_open_at": task.submissions_open_at,
                        "due_at": task.due_at,
                        "auto_finalize_after_minutes": task.auto_finalize_after_minutes,
                        "accepted_suffixes": accepted,
                    }
                )
            )
            uow.commit()
        console.last_task = (str(course.id), saved)
        return saved

    def _course_rubric_rows(course):
        """このコースの既定の観点（共通ルーブリック、無ければ組み込み）。"""
        criteria = (
            rubric.from_stored(course.rubric) if course.rubric else _default_rubric_criteria()
        )
        return rubric.to_rows(criteria)

    def _task_page(
        request,
        me,
        course,
        *,
        unit_key_value,
        task=None,
        version=None,
        note=None,
        saved="",
        why="",
        statement=None,
        chosen_kcs=None,
        kc_candidates=None,
        reference_draft=None,
        proposed=None,
        io_draft=None,
    ):
        """課題の編集／追加の画面。**追加と訂正で同じ形を使う。**

        別々に作ると、片方にだけ項目が足りない状態が生まれる（実際に
        `readability_weight` でそうなった）。
        """
        registry = EvaluatorRegistry().load_installed()
        course_rows = _course_rubric_rows(course)
        rows = rubric.to_rows(version.criteria) if version is not None else course_rows
        # **学習者に出ている版。** 教員が見ているのは最新版（承認待ちを含む）
        # なので、採点し直す対象は別に引く（#48）。
        published = None
        if task is not None:
            with _console(request).database.unit_of_work() as uow:
                published = uow.tasks.latest_published_version(task.id)
        # 移動先の候補。**いまいるセットは出さない**（選べる先が「動かない」を
        # 含むと、押してから何も起きないことになる）。追加のときは移動できる
        # 課題がまだ無いので数えない。
        others: list[dict[str, object]] = []
        # この課題が属する問題セット。**日程を並べて見せるために要る**（#325）──
        # 課題の日程だけを出すと、それがセットと揃っているのかが読めない。
        own_unit = None
        if task is not None:
            console = _console(request)
            with console.database.unit_of_work() as uow:
                units = load_units(uow, course)
            others = [
                {"key": group.key, "unit": group.unit, "label": group.label, "due_at": group.due_at}
                for group in units
                if group.key != unit_key_value
            ]
            own_unit = next((g for g in units if g.key == unit_key_value), None)
        # 知識要素（#292）。候補はコースが使うもの、印はこの版が問うもの。
        # `chosen_kcs` は候補を出したときにフォームで選ばれていたもの（書き
        # かけを失わない）。
        console = _console(request)
        data_criteria = _data_driven_criteria(registry, version)
        cases_by_shape = _cases_by_shape(registry, version)
        # 版の履歴（#319）。**戻したい版を選ぶには、何があるかが見えていな
        # ければならない** ── 版は積まれているのに画面から読めなかった。
        with console.database.unit_of_work() as uow:
            history = uow.tasks.list_versions(task.id) if task is not None else ()
        course_kcs = _course_kcs(console, course)
        if chosen_kcs is None:
            with console.database.unit_of_work() as uow:
                chosen_kcs = _kc_keys_of(uow, version) if version is not None else ()
        return templates.TemplateResponse(
            request,
            "manage_task.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": task.title if task is not None else "課題を追加",
                    "href": f"/manage/courses/{course.id}/units/{unit_key_value}",
                },
                "unit_key": unit_key_value,
                "task": task,
                "version": version,
                # 書きかけの問題文（候補を出したあと）。無ければ保存済みの版。
                "statement_draft": statement,
                "course_kcs": course_kcs,
                "chosen_kcs": tuple(chosen_kcs),
                "kc_candidates": kc_candidates,
                "rubric_rows": rows,
                # 「共通ルーブリックに復元」が差し込む中身。**サーバが描く** ──
                # 欄の作り方を JavaScript にも持たせると、項目が増えたときに
                # 片方だけ古くなる（#58）。
                "course_rubric_rows": course_rows,
                # None なら「コースに従う」。**コースと同じ値を選ぶのとは違う**
                # 状態で、前者はコースを変えれば追随する。
                "task_aggregation": None if version is None else version.aggregation,
                # 共通ルーブリックのままか、この課題で変えてあるか。
                # **共通が設定されているときだけ言う** ── 組み込みの既定は
                # 課題の作られ方（テストケースの有無）で中身が変わるので、
                # 「同じ」と言い切れない。
                "course_has_rubric": bool(course.rubric),
                "rubric_is_course_default": bool(course.rubric) and rows == course_rows,
                "deterministic": _evaluator_rows(registry, EvaluatorKind.DETERMINISTIC),
                # 受付のときに書き起こされるもの（#351）。**課題のプロファイル
                # で見る** ── 混在コースではコースの値と食い違う（#195・#264
                # で `_graded_by_tests` が同じ理由でこうなっている）。
                "transcription": _transcription_note(
                    _effective_profile_of(_console(request), course, version),
                    (task.accepted_suffixes if task is not None else ())
                    or course.upload_suffixes
                    or DEFAULT_UPLOAD_SUFFIXES,
                ),
                # **AI 評価器も選べるようにする**（#315）。空（既定）は
                # `rubric_ai_judge` のことで、項目を積み上げる
                # `checklist_ai_judge` は指名しなければ走らない。
                "ai_evaluators": _evaluator_rows(registry, EvaluatorKind.AI),
                # 既定の選択肢に出す説明。**評価器から取る**（#318）── 画面に
                # 書き写すと、docstring を直した日にここだけが古くなる。
                "ai_default_about": next(
                    (
                        row["about"]
                        for row in _evaluator_rows(registry, EvaluatorKind.AI)
                        if row["name"] == "rubric_ai_judge"
                    ),
                    "",
                ),
                "suffix_groups": SUFFIX_GROUPS,
                "course_suffixes": (
                    (task.accepted_suffixes if task is not None else ())
                    or course.upload_suffixes
                    or DEFAULT_UPLOAD_SUFFIXES
                ),
                "note": note or SAVED_MESSAGES.get(saved),
                # 直前に何を保存したか（#309）。**その場所を開いて返す** ──
                # 観点の中の欄から保存したのに畳まれた画面が返ると、直した
                # ものがどこへ行ったのか分からない。JavaScript が無くても効く。
                "saved_key": saved,
                # 書き直せなかった理由（`?why=`）。知らせの隣に出す。
                "why": why,
                "other_units": others,
                # 属する問題セットと、そこと日程が揃っているか（#325）。
                "own_unit": own_unit,
                # **ばらつきの判定は問題セットのものを使う**（`UnitGroup.mixed`）。
                # ここで「代表値と較べる」を書いたところ、代表は最も早い公開と
                # 最も遅い締切の包絡線なので、**締切を後ろへ動かした課題自身は
                # 常に代表と一致する**（ずれているのは動かさなかった側になる）。
                # 2 つの画面が違う理屈でばらつきを言うと、片方が「ばらついて
                # いる」と言い、もう片方が「揃っている」と出る。
                "unit_schedule_mixed": bool(own_unit and own_unit.mixed),
                # テストで確定できる科目か。宣言していない科目（レポートなど）
                # には出さない ── 選べない選択肢を見せない。
                "wants_tests": _wants_tests(request, course, version),
                # この課題が検証データで採点する観点を、データの形ごとに
                # （#300・#302）。**科目が宣言していなくても、観点に割り当てた
                # なら欄を出す。** 形は評価器が名乗る（`test_case_shape`）。
                "data_criteria": data_criteria,
                # 評価器 → 検証データの形（#303）。**観点の欄がこれを見て、
                # 自分の採点材料をその場に出す** ── 入出力セットも項目表も
                # 「どの観点が何で判定されるか」に属する。
                "criterion_data": {
                    name: shape for shape, names in data_criteria.items() for name in names
                },
                # 形ごとの検証データ。**混ぜない** ── 1 つの課題が入出力と
                # 項目表の両方を持てる。
                # 書きかけの入出力セット（#305）。**生成や提案から戻った
                # ときは、保存済みではなく手元の内容を出す** ── 書きかけを
                # 捨てて保存済みを出すと、直しかけたものが黙って消える。
                "io_cases": io_draft if io_draft is not None else cases_by_shape.get("io", ()),
                "item_cases": cases_by_shape.get("items", ()),
                # AI に書かせた解答例（保存はしていない）。
                "reference_draft": reference_draft,
                # 走らせて期待出力を埋めた提案（採用は人が選ぶ・P5）。
                "proposed": proposed,
                # **既にある課題にも出す。** #15 より前に画面から作った課題は
                # テストケースを持てず、正しさが AI 判定のまま残っている。
                # 課題を開いたときに分からなければ、直す機会が無い。
                "falls_back_to_ai": (
                    task is not None
                    and version is not None
                    and not version.test_cases
                    and _wants_tests(request, course, version)
                ),
                # 訂正した版で採点し直せる件数（確定済みは数えない）。
                "regradable": _regradable(_console(request), task, published),
                # 直近の生成の失敗理由。**そのまま出す**（決めつけない・#52）。
                "test_case_error": _test_case_error(request, course, task),
                # 学習者に出ている版。教員が見ている版と違うことがある（#48）。
                "published": published,
                # 版の履歴（新しい順）。戻せる先を選ぶために出す（#319）。
                "history": history,
                # 提出の件数。**0 のときだけ削除を出す**（#51）。
                "submissions": _submission_count(_console(request), task),
                # 学習者に出る形（#105）。**保存済みの版を描いて出す。**
                # 書きかけの内容は「プレビューを更新」で同じ関数を通す ──
                # ブラウザ側で Markdown を描くと、普通の文章では一致し、
                # 間違いが起きるところ（数式・画像・生 HTML）でだけ食い違う。
                "statement_html": (
                    render_statement(version.statement) if version is not None else ""
                ),
                # 問題文に貼る画像（#64）。**課題の編集画面でも受け取る** ──
                # コースの設定画面まで往復させると、書きかけの問題文が失われる。
                "image_suffixes": sorted(images.SUFFIX_TYPES),
                "image_max_mb": images.MAX_BYTES // (1024 * 1024),
                # 貼るときの既定の表示幅。**画面で言う値と貼る値を 1 つにする。**
                "image_display_width": images.DISPLAY_WIDTH,
            },
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/test-cases")
    def add_test_cases(request: Request, course_id: str, task_id: str) -> Response:
        """既にある課題にテストケースを足す。**新しい版として。**

        以前はここに道が無く、画面は「付けるには課題を作り直してください」と
        書いていた。作り直せば**別の課題**になり、問題セットに同じ問題が 2 件
        並び、提出も採点もその 2 件に割れる ── 実際にそうなっていた（#43）。

        塞いであった理由は「訂正で作り直すと、出題済みの課題の採点基準が黙って
        変わる」。だが**新しい版を作れば P8 は保たれる** ── 過去の採点は自分の
        版を指したままになる。塞ぐべきだったのは「黙って変わる」ことであって、
        「変えられる」ことではない。**黙らせない**ために、生成した版は承認待ち
        にし（P5）、承認するまで学習者には 1 つ前の承認済みが出続ける（#48）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            version = uow.tasks.latest_version(TaskId(task_id))
        if task is None or version is None or task.course_id != CourseId(course_id):
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        if version.test_cases:
            raise HTTPException(status_code=409, detail="この課題には既にテストケースがあります")

        # **課題のプロファイルで判断する**（#195・#264）。コースの値で見ると、
        # 混在コースでは「この科目はテスト実行を使いません」と、対象と関係
        # のない科目名で断られる（実際に C の課題が demo_image で断られた）。
        profile = load_profile(console.profiles_dir / f"{version.subject_profile}.yaml")
        if CODE_TEST_RUNNER not in profile.deterministic:
            raise HTTPException(
                status_code=409,
                detail=f"この課題（{version.subject_profile}）はテスト実行を使いません",
            )

        try:
            generated = TestCaseWriter().write(
                version.statement, language=_language_of(profile), count=5
            )
        except Exception as exc:
            # **課題を壊さない**（設計原則 P2）。作れなかったことと、作らない
            # ことは別で、前者はもう一度押せば直ることがある。
            #
            # **理由をそのまま出す。** 「S6 が止まっている可能性があります」と
            # 決めつけていたが、実際に出たとき S6 は動いており、応答が長さで
            # 切れていた（#52）。根拠の無い原因を書くと、言われたとおり確かめた
            # 教員は何も見つけられない。
            console.last_test_case_error = (
                str(course.id),
                task_id,
                f"{type(exc).__name__}: {exc}",
            )
            return RedirectResponse(
                f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=tests_failed#saved",
                status_code=303,
            )

        saved = _save_revision(
            console,
            me,
            course,
            task,
            version,
            statement=version.statement,
            criteria=_criteria_graded_by_tests(version, profile),
            position=task.position,
            accepted=task.accepted_suffixes,
            aggregation=version.aggregation,
            reference_solution=generated.reference_solution,
            test_cases=tuple(
                TestCaseSpec(name=case.name, input=case.input, expected=case.expected)
                for case in generated.test_cases
            ),
            generated_by=generated.model,
            generation_prompt_version=generated.prompt_id,
        )
        # 門 1・門 2 を通し、結果を残す（生成課題と同じ・ADR 0008）。
        _record_gates(console, profile, saved.version)
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=tests_added#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/regrade")
    def regrade_task(request: Request, course_id: str, task_id: str) -> Response:
        """いまの版で採点し直す。**教員が押したときだけ動く。**

        実施中に課題を訂正すると、訂正前に出した提出は古い版の基準で付いた
        成績のまま残る。直す道が無ければ、誤った問題文で付いた点がそのまま
        成績になる ── かといって訂正のたびに自動で積み直すと、**誰も押して
        いない再採点で成績が動く**（設計原則 P5）。だから明示的な操作にする。

        **これはレビューの経路ではない。** ADR 0007 が切り離したのは
        「blind 採点や開示が採点を起動する」ことで、測定用の入力が採点の前提に
        戻るのを防ぐためだった。ここは作問・運用の操作で、積むのはジョブだけ
        ── 採点そのものはワーカーが走らせる（提出時と同じ経路）。

        **確定済みの提出は動かさない**（`_regradable`）。過去の採点も消えない
        ── 新しい採点が終わった時点で旧採点に `superseded_by` が入る（P8）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            # **学習者に出ている版で採点し直す。** 承認待ちの版で採点すると、
            # 誰も見ていない基準の点が成績になる（#48）。
            version = uow.tasks.latest_published_version(TaskId(task_id))
        if task is None or task.course_id != CourseId(course_id):
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        if version is None:
            raise HTTPException(
                status_code=409, detail="承認済みの版がありません（先に承認してください）"
            )

        service = SubmissionService(console.database.unit_of_work, console.store)
        with console.database.unit_of_work() as uow:
            rows = uow.reviews.unfinalized_for_task(task.id)
        queued = 0
        for submission, run, _request in rows:
            if run.context.task_version_id == version.id:
                continue
            service.request_regrade(
                tenant_id=course.tenant_id,
                submission_id=submission.id,
                # **再採点は、回す先の版の規則で走らせる**（#195・#264）。
                # コースの値を渡すと、混在コースで別の科目の規則で採点し直す
                # ことになる ── 再採点は「同じ課題を新しい版で見直す」操作で
                # あって、課題の種類を変える操作ではない。
                subject_profile=version.subject_profile,
                task_version_id=version.id,
            )
            queued += 1
        console.last_regrade = (str(course.id), queued)
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=regraded#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/withdraw")
    def withdraw_task_route(
        request: Request,
        course_id: str,
        task_id: str,
        restore: Annotated[str, Form()] = "",
    ) -> Response:
        """出題を取り下げる（`restore` で取り消す）。**消さない。**

        採点結果は課題版を指しているので（P8）、提出のある課題を消すと過去の
        成績の出所が失われる。知識要素で決めたのと同じ区別で
        （`aijudge_admin.kc`）、使われたものは取り下げ、一度も使われていない
        ものだけを消す。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        _task_of(console, course, task_id)

        try:
            withdraw_task(console.database, task_id=TaskId(task_id), restore=bool(restore.strip()))
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        saved = "restored" if restore.strip() else "withdrawn"
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved={saved}#saved", status_code=303
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/delete")
    def delete_task_route(request: Request, course_id: str, task_id: str) -> Response:
        """**提出が 1 件も無い課題だけを消す。** 判定は `aijudge_admin.tasks`。

        提出があれば断り、取り下げを案内する（規則の置き場所を 1 つにする）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)

        try:
            delete_task(console.database, task_id=TaskId(task_id))
        except AdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        unit = unit_key(task)
        return RedirectResponse(
            f"/manage/courses/{course_id}/units/{unit}?saved=task_deleted#saved", status_code=303
        )

    @router.get("/courses/{course_id}/tasks/{task_id}/edit", response_class=HTMLResponse)
    def edit_task(
        request: Request, course_id: str, task_id: str, saved: str = "", why: str = ""
    ) -> Response:
        """既にある課題を直す画面。**問題セットのページには展開しない。**

        ルーブリックと問題文は横幅いっぱいで読むものなので、一覧の中に
        畳んで置くと段階の説明が読めない。
        """
        from .app import require_principal

        me = require_principal(request)
        course, role = _require_reader(request, me, CourseId(course_id))
        console = _console(request)
        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            version = uow.tasks.latest_version(TaskId(task_id))
        if task is None or version is None or task.course_id != CourseId(course_id):
            raise HTTPException(status_code=404, detail="課題が見つかりません")
        # **TA は読むだけ**（#102）。課題文は描いて出す ── Markdown のまま
        # 出すと、採点しながら読む相手にとっては学習者に見えている画面と
        # 別物になる（`statement.py`）。
        if not _can_edit(role):
            readonly_registry = EvaluatorRegistry().load_installed()
            data_criteria = _data_driven_criteria(readonly_registry, version)
            cases_by_shape = _cases_by_shape(readonly_registry, version)
            return templates.TemplateResponse(
                request,
                "task_readonly.html",
                {
                    "me": me,
                    "course": course,
                    "section": {
                        "label": task.title,
                        "href": f"/manage/courses/{course.id}/units/{unit_key(task)}",
                    },
                    "task": task,
                    "version": version,
                    "unit_key": unit_key(task),
                    "statement_html": render_markdown(version.statement),
                    "rubric_rows": rubric.to_rows(version.criteria),
                    "accepted": task.accepted_suffixes
                    or course.upload_suffixes
                    or DEFAULT_UPLOAD_SUFFIXES,
                    # 検証データで採点する観点（#300・#302）。**TA には確認
                    # だけ。** 0 件のときに何が起きているかは教員と同じ言葉で
                    # 出す ── 「AI が判定します」と出していたので、実際には
                    # 誰も判定していないことが伝わらなかった。
                    "data_criteria": data_criteria,
                    "criterion_data": {
                        name: shape for shape, names in data_criteria.items() for name in names
                    },
                    "io_cases": cases_by_shape.get("io", ()),
                    "item_cases": cases_by_shape.get("items", ()),
                },
            )
        return _task_page(
            request,
            me,
            course,
            unit_key_value=unit_key(task),
            task=task,
            version=version,
            saved=saved,
            # **理由をそのまま出す**（決めつけない・#52）。以前は錨に載せて
            # いたので URL の欄にしか出ず、画面には「書き直せませんでした」
            # としか出なかった ── 錨は知らせの着地点に要る。
            why=why,
        )

    @router.get("/courses/{course_id}/units/{unit}/tasks/new", response_class=HTMLResponse)
    def new_task(request: Request, course_id: str, unit: str) -> Response:
        """課題を追加する画面。訂正と同じ形。"""
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        return _task_page(request, me, course, unit_key_value=_normalized_unit(unit))

    @router.post("/courses/{course_id}/tasks/{task_id}/move")
    def move_task(
        request: Request,
        course_id: str,
        task_id: str,
        direction: Annotated[str, Form()] = "up",
    ) -> Response:
        """課題の並びを 1 つ入れ替える。

        **数字を打たせない。** 出題順は「この問題セットの中で何番目か」で
        あって、教員が意識するのは前後関係だけである。数字で持たせると、
        1 問差し込むたびに全部を打ち直すことになる。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        with console.database.unit_of_work() as uow:
            tasks = [
                task
                for task in uow.tasks.list_for_course(CourseId(course_id))
                if unit_key(task) == unit_key(uow.tasks.get_task(TaskId(task_id)))
            ]
            ordered = sorted(tasks, key=lambda item: item.sort_key)
            index = next((i for i, task in enumerate(ordered) if str(task.id) == task_id), None)
            if index is None:
                raise HTTPException(status_code=404, detail="課題が見つかりません")
            swap = index - 1 if direction == "up" else index + 1
            if 0 <= swap < len(ordered):
                first, second = ordered[index], ordered[swap]
                # 位置を入れ替える。番号が無い課題には並び順から与える。
                first_position = first.position or index + 1
                second_position = second.position or swap + 1
                uow.tasks.save_task(first.model_copy(update={"position": second_position}))
                uow.tasks.save_task(second.model_copy(update={"position": first_position}))
                uow.commit()
            key = unit_key(ordered[index])
        return RedirectResponse(
            f"/manage/courses/{course_id}/units/{key}?saved=order#saved", status_code=303
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/unit")
    def move_task_to_unit(
        request: Request,
        course_id: str,
        task_id: str,
        unit: Annotated[str, Form()] = "",
    ) -> Response:
        """課題を別の問題セットへ移す。**日程は移った先に揃える。**

        セットの中で締切がずれると、学習者にも教員にも「この回はいつまでか」
        が言えなくなる（`set_unit_schedule` と同じ理由）。移した課題だけが
        元の締切を持ち続けると、まさにその状態になる。

        **提出済みでも移せる。** 日程はもともと学期の途中で動くもので
        （ADR 0013 の減点は run に記録済みなので、既に付いた成績は動かない）、
        セットの日程を変える操作は提出の有無を問わず既に許してある。移動だけ
        禁じると、同じことが遠回りにしかできない。

        **鍵は変えない。** `TaskId` が鍵から導かれる（`derived_id("tsk", key)`）
        ので、`ex02/p8` を `ex03/p8` にすることは移動ではなく**別の課題を作る
        こと**である。鍵の前半は「どこで作られたか」の記録として残る。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        target = unit.strip()
        if not target:
            raise HTTPException(status_code=400, detail="移動先の問題セットを選んでください")

        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            if task is None or task.course_id != course.id:
                raise HTTPException(status_code=404, detail="課題が見つかりません")
            if (task.unit or "") == target:
                raise HTTPException(status_code=400, detail="すでにその問題セットにあります")

            siblings = [
                other
                for other in uow.tasks.list_for_course(course.id)
                if other.id != task.id and unit_key(other) == quote(target, safe="")
            ]
            # **移動先の先頭から日程を引き継ぐ。** 空のセットへ移す場合は
            # 引き継ぐ相手が居ないので、いまの日程のまま入る（セットの日程を
            # あとから入れれば全課題に行き渡る）。
            head = sorted(siblings, key=lambda item: item.sort_key)[0] if siblings else None
            update: dict[str, object] = {"unit": target}
            if head is not None:
                update |= {
                    "session": head.session,
                    "opens_at": head.opens_at,
                    "submissions_open_at": head.submissions_open_at,
                    "due_at": head.due_at,
                    "auto_finalize_after_minutes": head.auto_finalize_after_minutes,
                }
            # 並びは移動先の末尾。差し込む位置まで選ばせると、移動 1 回に
            # 決めることが 2 つになる（並べ替えは移動後に前後で動かせる）。
            #
            # **番号を持たない課題も数に入れる。** 画面から足した課題は
            # `position` が空のままで、番号だけを見て 1 を振ると末尾どころか
            # 先頭に入る（`move_task` が並べ替えで番号を補うのと同じ事情）。
            positions = [other.position for other in siblings if other.position is not None]
            update["position"] = max(len(siblings), max(positions, default=0)) + 1
            uow.tasks.save_task(task.model_copy(update=update))
            uow.commit()

        return RedirectResponse(
            f"/manage/courses/{course_id}/units/{quote(target, safe='')}?saved=moved#saved",
            status_code=303,
        )

    def _kc_candidates_for(console, course, statement: str) -> dict:
        """問題文から、その課題が問う知識要素の候補を AI に出させる（#292）。

        **選ばせるのはコースが使っている知識要素だけ**（#318）。以前は科目の
        名前空間にある語彙すべてから選ばせ、「このコースでは未使用のもの」も
        候補に並べていた ── 課題に付けるとコースの範囲にも入るので、**課題を
        直すつもりの操作でコースの設定が変わる**。コースに何を置くかは
        `/manage/courses/{id}/kc` で決めることで、課題の編集の副作用にしない。

        シラバスから候補を出す経路（`SyllabusReader.propose`）をそのまま使う
        ── 関門を 1 つに保つため。候補は登録済みの語彙からだけ来る（2026-09-13
        決定）。
        """
        profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
        namespaces = allowed_namespaces(profile)
        vocabulary = list_for_namespaces(console.database, namespaces, include_deprecated=False)
        # **このコースが使うものだけを見せる。** 語彙の全体を渡すと、その中から
        # 選ばれてしまう。
        in_course = {
            kc.key: kc.label for kc in vocabulary if kc.key in set(course.knowledge_components)
        }
        try:
            result = SyllabusReader().propose(
                statement, namespaces=namespaces, existing_keys=tuple(in_course)
            )
        except Exception as exc:  # 生成の失敗は運用の事象。理由を画面に返す。
            raise HTTPException(status_code=502, detail=f"候補を作れませんでした: {exc}") from exc
        suggested = [
            {"key": hint.key, "label": in_course.get(hint.key, hint.label)}
            for hint in result.proposal.knowledge_components
            if hint.key in in_course
        ]
        # コースの外から出てきた候補は落とす。**黙って落とさない** ── 件数と
        # 理由を出し、足したいならコースの知識要素で足す、と言えるようにする。
        outside = [
            hint.key for hint in result.proposal.knowledge_components if hint.key not in in_course
        ]
        return {
            "suggested": suggested,
            "outside": outside,
            "discarded": result.discarded,
            "empty": not result.proposal.knowledge_components,
        }

    @router.post("/courses/{course_id}/tasks/{task_id}/kc-candidates", response_class=HTMLResponse)
    async def task_kc_candidates(request: Request, course_id: str, task_id: str) -> Response:
        """編集画面の「AI に候補を出させる」。**書きかけの問題文で出す** ──
        保存してからでないと出せないと、問題文を書く手が止まる。フォームを
        丸ごと受け取り、問題文と選択中の知識要素はそのまま画面に戻す。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            version = uow.tasks.latest_version(TaskId(task_id))
        if task is None or version is None or task.course_id != CourseId(course_id):
            raise HTTPException(status_code=404, detail="課題が見つかりません")

        form = await request.form()
        statement = str(form.get("statement") or "") or version.statement
        if len(statement.strip()) < 20:
            raise HTTPException(status_code=400, detail="問題文が短すぎます（20 文字以上）")
        chosen = tuple(str(v) for v in form.getlist("kc") if str(v).strip())
        return _task_page(
            request,
            me,
            course,
            unit_key_value=task.unit or "_",
            task=task,
            version=version,
            statement=statement,
            chosen_kcs=chosen,
            kc_candidates=_kc_candidates_for(console, course, statement),
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/test-cases/edit")
    async def edit_test_cases(request: Request, course_id: str, task_id: str) -> Response:
        """テストケースを直して新しい版にする（#284）。

        **既存の版は書き換えない**（P8）。出題済みの版のテストを書き換えると、
        過去の採点が何で判定されたのか辿れなくなる。同じ内容なら版は上がらない。

        **参照解答があれば門 1 を先に通す。** 参照解答が通らない入出力は
        保存しない ── 保存してから全員が落ちる形は、期待出力の誤字 1 つで
        起きる。参照解答が無い課題は確かめようが無いので、そのまま保存する
        （画面はそう言っている）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            version = uow.tasks.latest_version(TaskId(task_id))
        if task is None or version is None or task.course_id != CourseId(course_id):
            raise HTTPException(status_code=404, detail="課題が見つかりません")

        form = await request.form()
        names = [str(v) for v in form.getlist("case_name")]
        inputs = [str(v) for v in form.getlist("case_input")]
        expected = [str(v) for v in form.getlist("case_expected")]
        weights = [str(v) for v in form.getlist("case_weight")]
        hidden = [str(v) for v in form.getlist("case_hidden")]
        deleted = {str(v) for v in form.getlist("case_delete")}
        # 参照解答は入出力セットと一緒に保存する（#305）。**ひと組だから** ──
        # 門 1 は両方を突き合わせる検査で、片方だけ先に保存できると、教員が
        # 意図していない組み合わせを検査することになる。欄が無い経路（API や
        # 古い画面）から来たときは、いまの版のものをそのまま持ち越す。
        if "reference_solution" in form:
            typed = str(form["reference_solution"]).replace("\r\n", "\n")
            # **空白だけなら「無い」。** 消したいときに消せる。中身があるなら
            # 打たれたまま持つ ── 末尾の改行を落とすと、触っていないのに
            # 版が上がる（内容の同一性はそこも見る）。
            reference = typed if typed.strip() else None
        else:
            reference = version.reference_solution

        def at(values: list[str], index: int, default: str = "") -> str:
            return values[index] if index < len(values) else default

        cases: list[TestCaseSpec] = []
        seen: set[str] = set()
        for index in range(len(names)):
            if str(index) in deleted:
                continue
            name = at(names, index).strip()
            text_in = at(inputs, index).replace("\r\n", "\n")
            text_out = at(expected, index).replace("\r\n", "\n")
            if not name and not text_in.strip() and not text_out.strip():
                continue  # 追加用の空行
            if not name:
                raise HTTPException(status_code=400, detail=f"{index + 1} 行目: 名前が要ります")
            if name in seen:
                raise HTTPException(status_code=400, detail=f"名前 {name!r} が重複しています")
            seen.add(name)
            try:
                weight = float(at(weights, index, "1.0") or 1.0)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail=f"{name}: 重みが数値ではありません"
                ) from None
            cases.append(
                TestCaseSpec(
                    name=name,
                    input=text_in,
                    expected=text_out,
                    hidden=at(hidden, index, "1") != "0",
                    weight=weight,
                )
            )
        # **採用した提案だけを足す**（#305）。印を付けなかったものは消える ──
        # 生成物は提案であって確定ではない（P5）。期待出力は提案の時点で
        # 参照解答を走らせて埋めてある。
        prop_names = [str(v) for v in form.getlist("prop_name")]
        prop_inputs = [str(v) for v in form.getlist("prop_input")]
        prop_expected = [str(v) for v in form.getlist("prop_expected")]
        for raw in form.getlist("prop_adopt"):
            try:
                index = int(str(raw))
            except ValueError:
                continue
            if not (0 <= index < len(prop_names)):
                continue
            name = prop_names[index].strip()
            if not name or name in seen:
                # 同じ名前が既にあるなら足さない。**黙って上書きしない** ──
                # 直したばかりのケースが提案で消えるのは、押した人の意図ではない。
                continue
            seen.add(name)
            cases.append(
                TestCaseSpec(
                    name=name,
                    input=at(prop_inputs, index).replace("\r\n", "\n"),
                    expected=at(prop_expected, index).replace("\r\n", "\n"),
                    hidden=True,
                    weight=1.0,
                )
            )

        if not cases:
            raise HTTPException(
                status_code=400,
                detail="テストケースが 0 件になります。全部消すなら課題を取り下げてください",
            )

        # 門 1: 参照解答が全ケースを通るか。**通らなければ保存しない。**
        # 見るのは**いま欄にあるもの**（#305）── 保存済みで確かめると、
        # 教員が直した解答例ではない別のもので判定することになる。
        if reference:
            evaluator_id = next(
                (case.evaluator_id for case in version.test_cases), CODE_TEST_RUNNER
            )
            candidate = version.model_copy(
                update={
                    "test_cases": tuple(
                        TestCase(
                            name=case.name,
                            evaluator_id=evaluator_id,
                            payload={"input": case.input, "expected": case.expected},
                            hidden=case.hidden,
                            weight=case.weight,
                        )
                        for case in cases
                    )
                }
            )
            profile = load_profile(console.profiles_dir / f"{version.subject_profile}.yaml")
            try:
                verifier = TaskVerifier(EvaluatorRegistry().load_installed(), profile)
                passed, detail = verifier.passes(candidate, reference)
            except (
                Exception
            ) as exc:  # サンドボックス不在など。**保存しない**（確かめられていない）。
                raise HTTPException(
                    status_code=502, detail=f"参照解答を走らせられませんでした: {exc}"
                ) from exc
            if not passed:
                raise HTTPException(
                    status_code=400,
                    detail=f"参照解答が通らないテストケースがあるので保存しません: {detail}",
                )

        _save_revision(
            console,
            me,
            course,
            task,
            version,
            statement=version.statement,
            criteria=rubric.from_criteria(version.criteria),
            aggregation=version.aggregation,
            position=task.position,
            accepted=task.accepted_suffixes,
            reference_solution=reference,
            # **他の評価器あての検証データを巻き込まない**（#302）。ここが
            # 直しているのは入出力の組だけで、同じ課題が項目表を持っている
            # ことがある ── 全件を作り直していたので、入出力を 1 文字直すと
            # 項目表が黙って消えた。
            test_cases=tuple(cases)
            + _kept_cases(version, editing=_io_evaluator_ids(EvaluatorRegistry().load_installed())),
        )
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=tests_revised#saved",
            status_code=303,
        )

    async def _io_draft_from(form) -> tuple:
        """フォームに入っている入出力セットを、画面に出す形で読み直す（#305）。

        **書きかけを捨てない。** 生成や提案から戻ったときに保存済みを出すと、
        直しかけた入出力が黙って消える。`TestCase` の形にして返す（画面は
        保存済みと同じ部品で描く）。
        """
        names = [str(v) for v in form.getlist("case_name")]
        inputs = [str(v) for v in form.getlist("case_input")]
        expected = [str(v) for v in form.getlist("case_expected")]
        weights = [str(v) for v in form.getlist("case_weight")]
        hidden = [str(v) for v in form.getlist("case_hidden")]
        deleted = {str(v) for v in form.getlist("case_delete")}
        out = []
        for index, name in enumerate(names):
            if str(index) in deleted or not name.strip():
                continue
            try:
                weight = float(weights[index]) if index < len(weights) else 1.0
            except ValueError:
                weight = 1.0
            out.append(
                TestCase(
                    name=name.strip(),
                    evaluator_id=CODE_TEST_RUNNER,
                    payload={
                        "input": (inputs[index] if index < len(inputs) else "").replace(
                            "\r\n", "\n"
                        ),
                        "expected": (expected[index] if index < len(expected) else "").replace(
                            "\r\n", "\n"
                        ),
                    },
                    hidden=(hidden[index] if index < len(hidden) else "1") != "0",
                    weight=weight,
                )
            )
        return tuple(out)

    @router.post("/courses/{course_id}/tasks/{task_id}/restore")
    def restore_task_version(
        request: Request,
        course_id: str,
        task_id: str,
        version_id: Annotated[str, Form()],
    ) -> Response:
        """古い版の中身で**新しい版を作る**（#319）。

        **版は書き換えない**（P8）。戻すのは「あの中身をもう一度出す」ことで
        あって、履歴を巻き戻すことではない ── 過去の採点はそれぞれ自分の版を
        指したまま残り、どの版で付いた点かが辿れる。

        **却下した版からも戻せる。** 却下は「その版を出さない」という判断で、
        中身を二度と使えないという意味ではない ── 直して出し直すつもりの
        却下もある。写すのは教員が明示的に押したときだけである。

        できる版は**承認済み**。教員が版を選んで押した操作なので、そこに
        承認の段をもう 1 つ置く理由が無い（生成物とはそこが違う・P5）。

        **版の id は経路ではなくフォームで受け取る。** 3 つの id（コース・
        課題・版）を経路に並べると 140 字になり、運用ログが識別子として
        受け付ける長さ（128 字）を超える（`aijudge_telemetry.context`）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)
        with console.database.unit_of_work() as uow:
            source = uow.tasks.get_version(TaskVersionId(version_id))
            current = uow.tasks.latest_version(TaskId(task_id))
            kcs = _kc_keys_of(uow, source) if source is not None else ()
        if source is None or current is None or source.task_id != TaskId(task_id):
            raise HTTPException(status_code=404, detail="課題版が見つかりません")

        _save_revision(
            console,
            me,
            course,
            task,
            current,
            statement=source.statement,
            criteria=rubric.from_criteria(source.criteria),
            aggregation=source.aggregation,
            position=task.position,
            accepted=task.accepted_suffixes,
            reference_solution=source.reference_solution,
            test_cases=_kept_cases(source, editing=()),
            knowledge_components=kcs,
        )
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=restored_version#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/revise-with-ai")
    def revise_task_with_ai(
        request: Request,
        course_id: str,
        task_id: str,
        instructions: Annotated[str, Form()] = "",
    ) -> Response:
        """課題をいまの基準に合わせて書き直させ、**承認待ちの版**として積む（#306）。

        **必ず承認待ちである。** 教員はまだ 1 文字も読んでいない ── 生成物は
        提案であって確定ではない（P5・ADR 0008）。承認するまで学習者には
        いまの版が出続ける。

        書き直すのは**問題文と知識要素だけ**。観点はコースの共通ルーブリックが
        持っており（`_course_rubric_rows`）、段階の記述まで決まっている ──
        そこをモデルに書かせると、コースごとに決めた段階が課題ごとに割れる。
        入出力セットと参照解答も触らない（#305 の仕事で、期待出力は走らせて
        作る）── 混ぜると、どちらの都合で期待出力が変わったのかが辿れない。

        **直すところが無ければ積まない。** 差分の無い版を承認待ちに置くと、
        教員は中身の無い版を 1 件ずつ開いて確かめることになる。

        `instructions` は教員からの指示（1 行 1 件・任意）。**何を直してほしい
        かは、読んだ教員がいちばんよく知っている** ── 観点との食い違いは機械的に
        見付かるが、「毎年ここで質問が来る」は教員しか知らない。作問の指示と
        同じ扱いで、必須事項の列ではない（`aijudge_admin.revision` の冒頭）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)
        with console.database.unit_of_work() as uow:
            version = uow.tasks.latest_version(TaskId(task_id))
            current_kcs = _kc_keys_of(uow, version) if version is not None else ()
        if version is None:
            raise HTTPException(status_code=404, detail="課題が見つかりません")

        rows = rubric.to_rows(version.criteria)
        try:
            revised = TaskReviser().revise(
                version.statement,
                criteria=tuple((row["title"], row["description"]) for row in rows),
                vocabulary=tuple((kc.key, kc.label) for kc in _course_kcs(console, course)),
                current_kcs=tuple(current_kcs),
                instructions=tuple(
                    line.strip() for line in instructions.splitlines() if line.strip()
                ),
            )
        except Exception as exc:
            # **理由をそのまま出す**（決めつけない・#52）。
            return RedirectResponse(
                f"/manage/courses/{course_id}/tasks/{task_id}/edit"
                f"?saved=revision_failed&why={quote(str(exc)[:200], safe='')}#saved",
                status_code=303,
            )

        if revised.unchanged:
            return RedirectResponse(
                f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=revision_none#saved",
                status_code=303,
            )

        # **版にはしない。下書きとして置く**（#321）。承認するまで課題版を
        # 増やさない ── 却下しただけで「最新版が却下」になり、一覧が
        # 「出題されません」と嘘をつく形（#319）が根から消える。
        draft = TaskDraftRecord(
            id=new_id("dft"),
            course_id=course.id,
            kind=DraftKind.REVISION,
            task_id=task.id,
            spec=TaskSpec(
                key=_key_of(task, version),
                title=task.title,
                statement=revised.statement,
                unit=task.unit,
                session=task.session,
                position=task.position,
                # **観点はそのまま。** 変えるのは問題文と知識要素だけ。
                criteria=rubric.from_criteria(version.criteria),
                aggregation=version.aggregation,
                reference_solution=version.reference_solution,
                test_cases=_kept_cases(version, editing=()),
                knowledge_components=tuple(revised.knowledge_components or current_kcs),
                accepted_suffixes=task.accepted_suffixes,
            ),
            unit=task.unit or "",
            changes=revised.changes,
            generated_by=revised.model,
            generation_prompt_version=revised.prompt_id,
            created_by=me.user_id,
            created_at=datetime.now(UTC),
            subject_profile=version.subject_profile,
            accepted_suffixes=task.accepted_suffixes,
        )
        with console.database.unit_of_work() as uow:
            uow.tasks.save_draft(draft)
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/drafts?saved=revision_queued#saved", status_code=303
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/schedule")
    def set_task_schedule(
        request: Request,
        course_id: str,
        task_id: str,
        opens_at: Annotated[str, Form()] = "",
        submissions_open_at: Annotated[str, Form()] = "",
        due_at: Annotated[str, Form()] = "",
        grading_starts_at: Annotated[str, Form()] = "",
        accepts_until: Annotated[str, Form()] = "",
    ) -> Response:
        """**この課題 1 件の日程**を直す。

        日程を決めるのは問題セットである（`set_unit_schedule`）── ここは
        **揃っていないものを直すための口**であって、課題ごとに違う締切を
        置くための機能ではない。画面もそう書いてある。

        それでも 1 件ずつ直せる必要があるのは、ばらつきが実際に起きるから
        である ── 取り込んだ課題は 1 件ずつ日程を持っていることがあり、
        あとから足した課題はセットの日程を持っていない。「セットの日程を
        保存し直して全部に当てる」は、**当てたくない課題まで動かす**
        （試験の回を含むセットで採点開始が揃っていない、など）。

        版は上がらない。**日程は課題の内容ではない**ので、直しても過去の
        採点基準は変わらない（P8 の対象外）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)

        update = {
            "opens_at": _parse_when(opens_at),
            "submissions_open_at": _parse_when(submissions_open_at),
            "due_at": _parse_when(due_at),
            "grading_starts_at": _parse_when(grading_starts_at),
            "accepts_until": _parse_when(accepts_until),
        }
        try:
            # **`model_copy` を使わない。** 検証を走らせないので、締切が公開より
            # 前の課題がそのまま保存される（`_update_unit` と同じ理由）。
            updated = Task.model_validate(task.model_dump() | update)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=_first_error(exc)) from None

        with console.database.unit_of_work() as uow:
            uow.tasks.save_task(updated)
            # **締切は誰がいつ動かしたかを言えないといけない**（ADR 0013）。
            # セット単位の記録（`_update_unit`）と同じ理由で、1 件の操作も残す。
            recorder_for(uow, request, me).record(
                AuditAction.TASK_UPDATED,
                target_type="task",
                target_id=str(task.id),
                summary="課題の日程を変えた",
                detail={
                    "course_id": course_id,
                    "field": "task_schedule",
                    "changed": {
                        name: {"before": _plain(getattr(task, name, None)), "after": _plain(value)}
                        for name, value in update.items()
                    },
                },
            )
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=task_schedule#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/reference-solution")
    async def write_reference_solution(request: Request, course_id: str, task_id: str) -> Response:
        """AI に解答例を書かせて欄に入れる（#305）。**保存はしない。**

        版が上がるのは「保存」を押したときだけ（#58 と同じ作法）── 押した
        瞬間に承認待ちが増えて元に戻せない、を避ける。

        **書いたものは教員が読んで直す前提である。** 門が言えるのは「参照解答と
        テストケースが整合している」までで、両方が同じ勘違いをしていれば
        そのまま通る（`TaskVerifier`）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)
        with console.database.unit_of_work() as uow:
            version = uow.tasks.latest_version(TaskId(task_id))
        if version is None:
            raise HTTPException(status_code=404, detail="課題が見つかりません")

        form = await request.form()
        profile = load_profile(console.profiles_dir / f"{version.subject_profile}.yaml")
        try:
            written = SolutionWriter().write(version.statement, language=_language_of(profile))
        except Exception as exc:
            # **理由をそのまま出す**（決めつけない・#52）。モデルが落ちている
            # のか、応答が形式に合わないのかで、次にすることが違う。
            return _task_page(
                request,
                me,
                course,
                unit_key_value=unit_key(task),
                task=task,
                version=version,
                note=f"解答例を書けませんでした: {exc}",
                io_draft=await _io_draft_from(form),
            )
        return _task_page(
            request,
            me,
            course,
            unit_key_value=unit_key(task),
            task=task,
            version=version,
            note=(
                "解答例を書きました。**まだ保存していません** — 読んで直してから"
                "「テストケースを保存して新しい版にする」を押してください"
            ),
            reference_draft=written.solution,
            io_draft=await _io_draft_from(form),
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/test-cases/propose")
    async def propose_test_cases(request: Request, course_id: str, task_id: str) -> Response:
        """解答例からテストケースを提案する（#305）。**保存はしない。**

        **期待出力はモデルに書かせない。** 入力だけを提案させ、**いま欄にある
        解答例を実際に走らせて**埋める（`outputs_for`）── 誤った期待出力は
        「全員が落ちる」として現れ、原因は提出物の側に見える。決定的な結果は
        `conclusive` なので AI にも見直されない（P3）。

        **走らせるにはサンドボックスが要る。** 無い環境では作れないと言う ──
        黙ってモデルの書いた出力に落とすと、確かめていないものが確かめた顔で
        入る（`docs/RUNNING.md`・ADR 0006）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)
        with console.database.unit_of_work() as uow:
            version = uow.tasks.latest_version(TaskId(task_id))
        if version is None:
            raise HTTPException(status_code=404, detail="課題が見つかりません")

        form = await request.form()
        io_draft = await _io_draft_from(form)
        reference = str(form.get("reference_solution") or "").replace("\r\n", "\n")

        def page(note: str, proposed=None):
            return _task_page(
                request,
                me,
                course,
                unit_key_value=unit_key(task),
                task=task,
                version=version,
                note=note,
                reference_draft=reference or None,
                io_draft=io_draft,
                proposed=proposed,
            )

        if not reference.strip():
            # **解答例が無ければ提案しない。** 期待出力を埋める相手が無い。
            return page("解答例が空です。先に解答例を書く（または AI に書かせる）でください")

        profile = load_profile(console.profiles_dir / f"{version.subject_profile}.yaml")
        try:
            proposal = InputProposer().propose(
                version.statement, reference, language=_language_of(profile)
            )
        except Exception as exc:
            return page(f"テストケースを提案できませんでした: {exc}")

        # 既にある名前は避ける。**黙って上書きしない。**
        taken = {case.name for case in io_draft}
        wanted = [
            (case.name, case.input.replace("\r\n", "\n"))
            for case in proposal.cases
            if case.name not in taken
        ]
        try:
            runs = outputs_for(
                EvaluatorRegistry().load_installed(),
                profile,
                version,
                reference,
                wanted,
                evaluator_id=CODE_TEST_RUNNER,
            )
        except Exception as exc:  # サンドボックス不在など
            return page(f"解答例を走らせられませんでした（サンドボックスが要ります）: {exc}")

        why = {case.name: case.why for case in proposal.cases}
        proposed = [
            {
                "name": run.name,
                "input": run.input,
                "output": run.output,
                "ok": run.ok,
                "reason": run.reason,
                "why": why.get(run.name, ""),
            }
            for run in runs
        ]
        usable = sum(1 for row in proposed if row["ok"])
        return page(
            f"{len(proposed)} 件を提案しました（走ったのは {usable} 件）。"
            "採用するものに印を付けて保存してください。**印を付けないものは入りません**",
            proposed=proposed,
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/items/edit")
    async def edit_item_list(request: Request, course_id: str, task_id: str) -> Response:
        """項目表を直して新しい版にする（#302）。

        **既存の版は書き換えない**（P8）。出題済みの版の項目を書き換えると、
        過去の採点が何で判定されたのか辿れなくなる。同じ内容なら版は上がらない。

        入出力セットと違い、**保存前に確かめられることが無い** ── 項目が
        妥当かどうかは提出物を読まないと分からず、それは採点そのものである。
        だから門は無く、代わりに判定は確定させない（AI 評価器の側・P5）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            version = uow.tasks.latest_version(TaskId(task_id))
        if task is None or version is None or task.course_id != CourseId(course_id):
            raise HTTPException(status_code=404, detail="課題が見つかりません")

        registry = EvaluatorRegistry().load_installed()
        named = _data_driven_criteria(registry, version).get("items", ())
        if not named:
            # **観点が指名していない課題に項目表を持たせない。** 持たせると、
            # 誰も読まないデータが版に残り、画面にも出ない。
            raise HTTPException(
                status_code=400,
                detail="この課題の観点は項目表を読む評価器を指名していません",
            )

        form = await request.form()
        names = [str(v) for v in form.getlist("item_name")]
        descriptions = [str(v) for v in form.getlist("item_description")]
        aliases = [str(v) for v in form.getlist("item_aliases")]
        weights = [str(v) for v in form.getlist("item_weight")]
        hidden = [str(v) for v in form.getlist("item_hidden")]
        deleted = {str(v) for v in form.getlist("item_delete")}

        def at(values: list[str], index: int, default: str = "") -> str:
            return values[index] if index < len(values) else default

        items: list[TestCaseSpec] = []
        seen: set[str] = set()
        for index in range(len(names)):
            if str(index) in deleted:
                continue
            name = at(names, index).strip()
            if not name:
                continue  # 追加用の空行
            if name in seen:
                raise HTTPException(status_code=400, detail=f"項目 {name!r} が重複しています")
            seen.add(name)
            try:
                weight = float(at(weights, index, "1.0") or 1.0)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail=f"{name}: 重みが数値ではありません"
                ) from None
            if weight <= 0:
                raise HTTPException(status_code=400, detail=f"{name}: 重みは正の値にしてください")
            payload: dict[str, object] = {}
            description = at(descriptions, index).strip()
            if description:
                payload["description"] = description
            hints = _split_aliases(at(aliases, index))
            if hints:
                payload["aliases"] = list(hints)
            items.append(
                TestCaseSpec(
                    name=name,
                    # **この 1 件を読む評価器を明示する。** 課題の既定に倒すと
                    # `code_test_runner` あてになり、誰も読まないまま残る。
                    evaluator=named[0],
                    payload=payload,
                    hidden=at(hidden, index, "0") == "1",
                    weight=weight,
                )
            )
        if not items:
            # **0 件は「既定に従う」である。** 科目プロファイルの項目表が
            # 使われる ── 画面はそう言っている。全部消せることは残す。
            pass

        _save_revision(
            console,
            me,
            course,
            task,
            version,
            statement=version.statement,
            criteria=rubric.from_criteria(version.criteria),
            aggregation=version.aggregation,
            position=task.position,
            accepted=task.accepted_suffixes,
            reference_solution=version.reference_solution,
            test_cases=tuple(items) + _kept_cases(version, editing=named),
        )
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=items_revised#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/revise")
    async def revise_task(request: Request, course_id: str, task_id: str) -> Response:
        """既にある課題を直す。**出題済みの版は書き換えず、版を上げる**（P8）。

        過去の採点がどの基準で付いたのかを辿れなくなるので、上書きはしない。
        内容が同じなら版は上がらない（提出形式だけ変えたい場合がこれ）。

        観点が行数ぶん並ぶので、フォーム全体を読む。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        form = await request.form()
        statement = str(form.get("statement") or "")
        position = str(form.get("position") or "")
        suffix = [str(v) for v in form.getlist("suffix")]
        formats = str(form.get("formats") or "")

        try:
            criteria = rubric.parse(_rubric_from_form(form))
            aggregation = _aggregation_from_form(form)
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        with console.database.unit_of_work() as uow:
            task = uow.tasks.get_task(TaskId(task_id))
            version = uow.tasks.latest_version(TaskId(task_id))
        if task is None or version is None or task.course_id != CourseId(course_id):
            raise HTTPException(status_code=404, detail="課題が見つかりません")

        # 観点を送ってこない経路（問題文だけ直す等）では、**いまの観点を
        # そのまま引き継ぐ**。空で作り直すと、読みやすさの観点が黙って消えて
        # 次の版から採点されなくなる。
        criteria = criteria or rubric.from_criteria(version.criteria)

        # 知識要素（#292）。欄が送られてきたときだけ置き換える ── 欄を持たない
        # 経路（API 等）では None のまま渡し、いまの版のものを引き継ぐ。
        components = _kcs_from_form(console, course, form) if "kc_form" in form else None

        _save_revision(
            console,
            me,
            course,
            task,
            version,
            statement=statement,
            criteria=criteria,
            aggregation=aggregation,
            position=int(position) if position.strip() else task.position,
            accepted=_chosen_suffixes(suffix, formats, course),
            knowledge_components=components,
            # **テストと参照解答も引き継ぐ**（#262）。観点と同じ理由で、
            # 結果はもっと悪い ── 観点が消えれば採点されない観点が出るだけ
            # だが、テストが消えると決定的評価が何も採点できず、総合点が
            # 永久に保留になる。しかも画面には何も出ない。
            #
            # この経路に来るのは「問題文の誤字を直す」のような操作で、
            # テストを捨てる意図は無い。捨てたいときは、テストを作り直す
            # 経路（`/test-cases`）がある。
            reference_solution=version.reference_solution,
            # **版が持つのはドメインの `TestCase`**（`payload` の中に入力と
            # 期待出力がある）で、生成経路が渡す `TestCaseSpec` とは形が違う。
            # キー名は評価器が読むものと一致していなければならない
            # （`spec.build_task_version` の注記）── 違う名前で書くと既定値の
            # 空文字と比較され、**全ケースが黙って不合格になる**。
            test_cases=tuple(
                TestCaseSpec(
                    name=case.name,
                    input=str(case.payload.get("input", "")),
                    expected=str(case.payload.get("expected", "")),
                    hidden=case.hidden,
                    weight=case.weight,
                )
                for case in version.test_cases
            ),
        )
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=task#saved", status_code=303
        )

    @router.get("/courses/{course_id}/mastery", response_class=HTMLResponse)
    def mastery_overview(request: Request, course_id: str) -> Response:
        """コースの習熟度 ── 分布と推移（#328）。

        **教員とテナント管理者だけ**（`_require_enrolment_manager`）。TA は
        採点を分担するが履修の管理はしない ── 習熟度は成績から積み上がる値で、
        締切や受講の変更と同じ側にある。

        **母数を偽らない。** 受講者の数と、記録のある受講者の数は別に出す。
        まだ誰も問われていない KC は 0 点として数えない ── 数えると「この科目は
        全然できていない」という読みになる（`mastery.overview`）。

        **これは断面である。** 習熟度は学習者 × KC で 1 つの値で、コース別には
        持っていない。ここで「コースの分布」と呼んでいるのは、このコースの
        受講者を、このコースが使う KC で切ったものにすぎない ── 値そのものには
        担当外の科目で得た観測も入っている。画面にそう書く。
        """
        from .app import require_principal

        me = require_principal(request)
        course, _ = _require_enrolment_manager(request, me, CourseId(course_id))
        console = _console(request)

        with console.database.unit_of_work() as uow:
            learners = tuple(
                enrollment.user_id
                for enrollment in uow.identity.list_enrollments(course.id)
                if enrollment.role is Role.LEARNER
            )
            kcs = tuple((str(kc.id), kc.key, kc.label) for kc in _course_kcs(console, course))
            # **まとめて引く**（#328）。1 人ずつ引くと受講者数ぶんの往復になる。
            states = uow.skills.list_states_for(me.tenant_id, learners)
            points = uow.skills.history(me.tenant_id, learners)

        view = mastery.overview(states, points, kcs=kcs, learners=len(learners))
        return templates.TemplateResponse(
            request,
            "manage_mastery.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": "習熟度",
                    "href": f"/manage/courses/{course.id}/mastery",
                },
                "view": view,
                "band_label": mastery.band_label,
                # 図の寸法はテンプレートに書かない ── 点の座標を作るのが
                # サーバ側なので、同じ値を 2 か所に置くと片方だけずれる。
                "chart": {"width": 640, "height": 140},
                "line": mastery.polyline(view.trend, width=640, height=140),
            },
        )

    @router.get("/courses/{course_id}/mastery/{user_id}", response_class=HTMLResponse)
    def mastery_for_learner(request: Request, course_id: str, user_id: str) -> Response:
        """1 人の習熟度と、その根拠（#328）。

        **根拠はこのコースのものだけを開く。** 習熟度はコースを跨いで動くので、
        1 つの値に担当外の科目の観測が入っている ── 黙って混ぜると、教員は
        自分の課題では説明できない値を説明しようとすることになる。外から来た
        ぶんは**件数だけ**出す（担当していないコースの課題名は成績に近い）。
        """
        from .app import require_principal

        me = require_principal(request)
        course, _ = _require_enrolment_manager(request, me, CourseId(course_id))
        console = _console(request)

        learner_id = UserId(user_id)
        with console.database.unit_of_work() as uow:
            if uow.identity.find_enrollment(course.id, learner_id) is None:
                # 存在と権限を区別しない（他コースの受講者を列挙させない）。
                raise HTTPException(status_code=404, detail="この受講者は見つかりません")
            learner = uow.identity.get_user(learner_id)
            labels = {str(kc.id): (kc.key, kc.label) for kc in _course_kcs(console, course)}
            states = [
                state
                for state in uow.skills.list_states(me.tenant_id, learner_id)
                if str(state.kc_id) in labels
            ]
            course_of, title_of = _where_evidence_came_from(uow, states)

        rows = [
            mastery.split_evidence(
                state,
                course_of=course_of,
                title_of=title_of,
                course_id=str(course.id),
                key=labels[str(state.kc_id)][0],
                label=labels[str(state.kc_id)][1],
            )
            for state in states
        ]
        rows.sort(key=lambda row: row.key)
        return templates.TemplateResponse(
            request,
            "manage_mastery_learner.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": "習熟度",
                    "href": f"/manage/courses/{course.id}/mastery",
                },
                "learner": learner,
                "rows": rows,
            },
        )

    def _where_evidence_came_from(uow, states) -> tuple[dict, dict]:
        """根拠の採点がどのコースの、どの課題から来たかを解決する。

        **`packages/skill` にコースを持ち込まない。** 習熟度はテナント単位で
        積み上がる値で、コースを知る必要が無い（P6）── 知る必要があるのは
        この画面だけなので、composition root であるここで辿る。

        辿りは 採点 → 課題版 → 課題 → コース の 3 段。**同じ版を二度引かない**
        ── 根拠は 1 KC あたり最大 20 件あり、同じ課題から来ることが多い。
        """
        course_of: dict[str, str | None] = {}
        title_of: dict[str, str | None] = {}
        version_cache: dict[str, tuple[str | None, str | None]] = {}
        for state in states:
            for item in state.evidence:
                run_id = str(item.grading_run_id)
                if run_id in course_of:
                    continue
                run = uow.runs.get(item.grading_run_id)
                if run is None:
                    course_of[run_id] = None
                    title_of[run_id] = None
                    continue
                version_id = str(run.context.task_version_id)
                if version_id not in version_cache:
                    version = uow.tasks.get_version(run.context.task_version_id)
                    task = None if version is None else uow.tasks.get_task(version.task_id)
                    version_cache[version_id] = (
                        None if task is None else str(task.course_id),
                        None if task is None else task.title,
                    )
                course_of[run_id], title_of[run_id] = version_cache[version_id]
        return course_of, title_of

    @router.get("/courses/{course_id}/enrolments", response_class=HTMLResponse)
    def enrolments(
        request: Request,
        course_id: str,
        q: str = "",
        role: str = "",
        saved: str = "",
        enrolled: int = 0,
        already: int = 0,
        provisioned: int = 0,
        unknown: str = "",
    ) -> Response:
        """受講者の一覧。**コースの設定とは別の画面にする。**

        受講 100 名規模になると、設定を 1 つ直しに来た教員が毎回 100 行を
        めくることになる。絞り込みは前方一致 ── 選択肢に並べても選べない
        （提出の一覧と同じ理由）。

        `enrolled` / `already` / `unknown` は直前の受講登録の結果
        （`add_enrolments` がリダイレクトで渡す）。**何件入って何件が
        入らなかったかを、その場で言う。** 「保存しました」だけだと、
        名簿の半分が未登録だったことに学期が始まってから気づく。
        """
        from .app import require_principal

        me = require_principal(request)
        course, _ = _require_enrolment_manager(request, me, CourseId(course_id))
        console = _console(request)

        prefix = q.strip().lower()
        # 役割でも絞る。TA だけ・教員だけを見たいとき、100 名の学生の中から
        # 探すことになっていた。値は役割の語彙にあるものだけ（無ければ絞らない）。
        wanted = role.strip() if role.strip() in {r.value for r in Role} else ""
        with console.database.unit_of_work() as uow:
            oidc = uow.identity.get_oidc_settings(me.tenant_id)
            all_enrollments = uow.identity.list_enrollments(course.id)
            people = []
            for enrollment in all_enrollments:
                if wanted and enrollment.role.value != wanted:
                    continue
                user = uow.identity.get_user(enrollment.user_id)
                login = getattr(user, "login", "") or str(enrollment.user_id)
                if prefix and not login.lower().startswith(prefix):
                    continue
                people.append({"enrollment": enrollment, "user": user, "login": login})
        people.sort(key=lambda row: (row["enrollment"].role.value, row["login"]))
        return templates.TemplateResponse(
            request,
            "manage_enrolments.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": "受講者",
                    "href": f"/manage/courses/{course.id}/enrolments",
                },
                "people": people,
                # **内訳は絞り込みの前に数える。** 絞り込んだ結果の内訳を出すと、
                # 「TA が 0 名」が登録漏れなのか絞り込みの結果なのか分からない。
                "role_counts": _role_counts(all_enrollments),
                "total": len(all_enrollments),
                "q": q.strip(),
                "role_filter": wanted,
                "all_roles": [r.value for r in Role],
                # **画面から配れる役割だけを出す**（#100）。`admin` は出さない
                # ── コースをまたぐ権限なので、コースの受講者一覧からは配れない。
                "roles": [role.value for role in GRANTABLE_ROLES],
                "saved": SAVED_MESSAGES.get(saved),
                "saved_key": saved,
                # **アカウントの書き方は設定から出す。** 学内ログイン（OIDC）の
                # アカウントは `<学籍番号>@<許可ドメイン>` で、ローカルアカウント
                # は login そのもの。「学籍番号を並べる」と書いていたが、龍大では
                # 学籍番号＠ドメインがアカウントなので、番号だけを貼ると全員が
                # 未登録になる。
                "login_domains": list(oidc.allowed_domains) if oidc else [],
                "enrol_result": (
                    {
                        "enrolled": enrolled,
                        "already": already,
                        "provisioned": provisioned,
                        "unknown": [name for name in unquote(unknown).split(",") if name],
                    }
                    if saved == "enrolled"
                    else None
                ),
            },
        )

    @router.post("/courses/{course_id}/enrolments/{user_id}/role")
    def set_role(
        request: Request,
        course_id: str,
        user_id: str,
        role: Annotated[str, Form()] = "",
    ) -> Response:
        """受講者の役割を変える。**自分の役割は変えられない。**

        自分を学習者に落とすとそのコースが見えなくなり、戻す手段が無い
        （受講の取り消しと同じ理由）。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_enrolment_manager(request, me, CourseId(course_id))
        if UserId(user_id) == me.user_id:
            raise HTTPException(status_code=400, detail="自分の役割は変えられません")
        try:
            new_role = Role(role)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"役割が不正です: {role!r}") from None
        # **画面で塞ぐだけにしない。** 選択肢を減らしても、POST は手で作れる。
        _require_grantable(new_role)

        console = _console(request)
        with console.database.unit_of_work() as uow:
            existing = uow.identity.find_enrollment(CourseId(course_id), UserId(user_id))
            if existing is None:
                raise HTTPException(status_code=404, detail="この受講者は登録されていません")
            # **管理者の役割は画面から動かせない**（#100）。付けられない権限を
            # 外せるのはおかしい ── 担当教員が管理者を自分のコースから締め出せる
            # ことになる。
            if existing.role is Role.ADMIN:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "管理者の役割はこの画面からは変えられません"
                        "（`aijudge-admin` で行ってください）"
                    ),
                )
            uow.identity.save_enrollment(existing.model_copy(update={"role": new_role}))
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/enrolments?saved=role#saved", status_code=303
        )

    @router.post("/courses/{course_id}/enrolments")
    def add_enrolments(
        request: Request,
        course_id: str,
        roster: Annotated[str, Form()],
        role: Annotated[str, Form()] = Role.LEARNER.value,
    ) -> Response:
        """名簿を貼り付けて受講登録する。

        **既存利用者のパスワードは変えない。** 新規利用者が居る場合は
        パスワードの配布が必要なので、ここでは作らず CLI に回す
        （画面に平文を出すと端末の履歴や画面共有に残る）。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_enrolment_manager(request, me, CourseId(course_id))
        console = _console(request)
        try:
            entries = parse_roster(roster, default_role=Role(role))
        except (RosterError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # **名簿の行にも役割が書ける**（`parse_roster` の 4 列目）。既定の役割
        # だけを見ると、貼り付けた名簿の中の `admin` が通る（#100）。
        _require_grantable(Role(role))
        for entry in entries:
            _require_grantable(entry.role)

        # **学内ログインのアカウントは、まだ無ければここで作る**（#285）。
        # login が OIDC の許可ドメインのメールアドレスなら、パスワードを配る
        # 必要が無い ── 捨て値で作っておき、本人の初回 SSO ログインで結び付く。
        # 教員が名簿を貼るだけで学期を始められるのは、これがあってこそ。
        #
        # ローカルアカウントの新規作成だけは引き続き CLI に回す。パスワードの
        # 配布が伴い、画面に平文を出すと端末の履歴や画面共有に残る。
        #
        # **未登録が混ざっていても、残りは入れる。** 全部を断ると、100 行の
        # 名簿が 1 行の綴り違いで丸ごと弾かれ、教員はどの行かを探して貼り
        # 直すことになる。入った件数と入らなかったアカウントは戻り先の画面に
        # そのまま出す（`enrolments` の `enrol_result`）。
        provisioned = 0
        with console.database.unit_of_work() as uow:
            oidc = uow.identity.get_oidc_settings(me.tenant_id)
            domains = set(oidc.allowed_domains) if oidc else set()
            auth = AuthService(uow.identity, audit=uow.audit)
            known: list[RosterEntry] = []
            for entry in entries:
                if uow.identity.find_user_by_login(me.tenant_id, entry.login) is not None:
                    known.append(entry)
                elif _is_sso_login(entry.login, domains):
                    auth.provision_external(tenant_id=me.tenant_id, email=entry.login)
                    provisioned += 1
                    known.append(entry)
            uow.commit()
        unknown = [entry.login for entry in entries if entry not in known]
        enrolled = already = 0
        if known:
            report = enrol_roster(
                console.database,
                tenant_id=me.tenant_id,
                course_id=CourseId(course_id),
                entries=known,
            )
            enrolled, already = len(report.enrolled), len(report.already)
        # 未登録の一覧は URL に載せて戻す。**長さは切る** ── 名簿を丸ごと
        # 間違えた（番号だけを貼った）ときに、URL が数千文字になる。
        shown = ",".join(unknown[:MAX_UNKNOWN_SHOWN])
        if len(unknown) > MAX_UNKNOWN_SHOWN:
            shown += f",…ほか {len(unknown) - MAX_UNKNOWN_SHOWN} 件"
        return RedirectResponse(
            f"/manage/courses/{course_id}/enrolments?saved=enrolled"
            f"&enrolled={enrolled}&already={already}&provisioned={provisioned}"
            f"&unknown={quote(shown)}#result",
            status_code=303,
        )

    @router.post("/courses/{course_id}/enrolments/{user_id}/remove")
    def remove_enrolment(request: Request, course_id: str, user_id: str) -> Response:
        """受講を取り消す。**利用者は消さない。**"""
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        if UserId(user_id) == me.user_id:
            # 自分を外すとそのコースが見えなくなり、戻す手段が無い。
            raise HTTPException(status_code=400, detail="自分の受講は取り消せません")
        console = _console(request)
        with console.database.unit_of_work() as uow:
            uow.identity.remove_enrollment(CourseId(course_id), UserId(user_id))
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/enrolments?saved=removed#saved", status_code=303
        )

    # ------------------------------------------------------------------
    # 採点設定（コースごとの上書き）
    # ------------------------------------------------------------------

    @router.post("/courses/{course_id}/grading", response_class=HTMLResponse)
    async def save_grading(request: Request, course_id: str) -> Response:
        """保存、または試走。**試走は保存しない。**

        項目が多いので、フォーム全体を読む（宣言した引数では追いつかない）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        registry = EvaluatorRegistry().load_installed()

        form = await request.form()
        overrides = _collect_overrides(form)
        action = str(form.get("action") or "save")

        if action == "try":
            try:
                trial = try_settings(
                    console.database,
                    course,
                    overrides,
                    profiles_dir=console.profiles_dir,
                    registry=registry,
                )
            except AdminError as exc:
                return _course_page(request, me, course, note=str(exc), values=overrides)
            return _course_page(request, me, course, trial=trial, values=overrides)

        try:
            save_grading_settings(
                console.database,
                course,
                overrides,
                profiles_dir=console.profiles_dir,
                registry=registry,
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return RedirectResponse(f"/manage/courses/{course_id}?saved=grading#saved", status_code=303)

    # ------------------------------------------------------------------
    # 知識要素（KC）の体系（設計原則 P6）
    # ------------------------------------------------------------------

    @router.get("/courses/{course_id}/kc", response_class=HTMLResponse)
    def kc_index(request: Request, course_id: str, saved: str = "") -> Response:
        """このコースが使える KC の一覧。

        **コースをまたいで共有される語彙である。** 同じ名前空間を使う他の
        コースにも同じものが見えるので、どれだけ使われているかを添える。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        return _kc_page(request, me, course, saved=saved)

    def _campus_configured(console, me) -> bool:
        """テナントに学内の範囲が 1 件でも入っているか（#333）。"""
        with console.database.unit_of_work() as uow:
            settings = uow.identity.get_campus_networks(me.tenant_id)
        return bool(settings and parse_cidrs(settings.cidrs))

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

    def _scope_in(console, course, keys: tuple[str, ...]) -> int:
        """足した知識要素を、このコースが使う範囲にも入れる。**足した数を返す。**

        **「このコースに追加する」は、登録と範囲の両方を意味する。** 片方だけ
        だと、追加しても一覧に出てこない ── 範囲から外したものを戻す道が
        塞がる（外れたものは一覧から隠れるため、戻す道がここになる）。
        """
        before = set(course.knowledge_components)
        merged = tuple(sorted(before | {k for k in keys if k}))
        if merged == tuple(course.knowledge_components):
            return 0
        with console.database.unit_of_work() as uow:
            uow.identity.save_course(course.model_copy(update={"knowledge_components": merged}))
            uow.commit()
        return len(merged) - len(before)

    def _kc_use_in_course(console, course) -> dict[str, int]:
        """**このコースの課題**が使っている知識要素と、その件数。

        `kc_usage` はコースをまたいで数える（引退させてよいかの判断に要る）。
        こちらは「このコースの課題が何を問うているか」で、別の問いである。
        """
        counts: dict[str, int] = {}
        with console.database.unit_of_work() as uow:
            by_id = {str(kc.id): kc.key for kc in uow.skills.list_kcs(None)}
            for task in uow.tasks.list_for_course(course.id):
                version = uow.tasks.latest_version(task.id)
                if version is None:
                    continue
                for entry in version.q_matrix:
                    key = by_id.get(str(entry.kc_id))
                    if key is not None:
                        counts[key] = counts.get(key, 0) + 1
        return counts

    def _kc_rows(console, course, kcs):
        """一覧の行 ── **このコースが使うもの**と、**このコースの課題が使っているもの**。

        2 つは別物である。範囲から外しても、既に出題した課題の Q-matrix は
        動かない（追記のみ・P8）ので、課題が使っているものは範囲に無くても
        残す ── 消してしまうと、その課題が何を問うているのかを画面から辿る
        手段が無くなる。

        **範囲に無く、どの課題も使っていないものは出さない。** 同じ名前空間を
        複数のコースが共有するので、出し続けると「このコースが使わないと決めた
        もの」が一覧に残り、決めたこと自体が画面から読めなくなる。足すときは
        名前空間の一覧（`_vocabulary_groups`）から。

        **このコースの課題が使っているものは外せない**（#289）。外すと Q-matrix
        が課題の中身と食い違う。理由（課題の件数）を添えて残す。
        """
        chosen = set(course.knowledge_components)
        usage_rows = kc_usage(console.database, kcs)
        here = _kc_use_in_course(console, course)
        kcs = tuple(kc for kc in kcs if kc.key in chosen or here.get(kc.key))
        return [
            {
                "usage": usage_rows[kc.key],
                "kc": kc,
                "used": usage_rows[kc.key].used,
                "tasks": usage_rows[kc.key].tasks,
                "courses": usage_rows[kc.key].courses,
                "in_course": kc.key in chosen,
                "used_here": here.get(kc.key, 0),
                "removable": kc.key in chosen and not here.get(kc.key),
            }
            for kc in kcs
        ]

    def _is_component(key: str) -> bool:
        """知識要素そのもの（`名前空間.分野.単位.知識要素`）か。

        分野（`cs.loops`）と単位（`cs.loops.control`）は骨格の枝であって、
        課題が問うものではない。範囲に入れる対象にしない。
        """
        return len(key.split(".")) >= 4

    def _vocabulary_groups(kcs, chosen: set[str]) -> list[dict]:
        """名前空間の語彙を**階層ごと**にまとめる（#289）。

        987 件を平らに並べても選べない。分野（`cs.loops`）ごとに畳み、その
        階層をまとめて足す・外すための接頭辞と、コースに入っている数を添える。
        引退したものは足せないので出さない。
        """
        labels = {kc.key: kc.label for kc in kcs}
        groups: dict[str, list] = {}
        for kc in kcs:
            if kc.deprecated or not _is_component(kc.key):
                continue
            parts = kc.key.split(".")
            prefix = ".".join(parts[:2])
            groups.setdefault(prefix, []).append(kc)
        return [
            {
                "prefix": prefix,
                "label": labels.get(prefix, ""),
                "kcs": members,
                "total": len(members),
                "in_course": sum(1 for kc in members if kc.key in chosen),
            }
            for prefix, members in sorted(groups.items())
        ]

    def _kc_page(
        request: Request,
        me,
        course,
        *,
        saved: str = "",
        proposal=None,
        discarded: tuple[str, ...] = (),
    ) -> Response:
        """知識要素のページ。**候補が出ているかどうかだけが違う。**

        候補を別のページにすると、教員は「いま体系に何があるか」を見ずに
        候補を選ぶことになる。重複を作らせないための情報が、選ぶ画面に
        無いことになる。

        `draft` は候補から追加フォームに取り込んだ 1 件（キー・名前・説明）。
        **取り込んだだけでは何も登録されない。** `draft_exists` は、そのキーが
        既に体系にあるか ── あるなら登録は「このコースの範囲に入れる」だけを
        意味し、名前と説明は変わらない。
        """
        console = _console(request)

        profile = load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
        namespaces = allowed_namespaces(profile)
        kcs = list_for_namespaces(console.database, namespaces)
        rows = _kc_rows(console, course, kcs)
        chosen = set(course.knowledge_components)
        # 直前の足す・外すの結果（件数）。画面に出したら消す。
        scope_result = None
        if console.last_kc_scope and console.last_kc_scope[0] == str(course.id):
            _cid, action, changed, kept = console.last_kc_scope
            scope_result = {"action": action, "changed": changed, "kept": kept}
            console.last_kc_scope = None
        return templates.TemplateResponse(
            request,
            "manage_kc.html",
            {
                "me": me,
                "course": course,
                "section": {"label": "知識要素", "href": f"/manage/courses/{course.id}/kc"},
                "namespaces": namespaces,
                "rows": rows,
                "chosen_count": len(chosen),
                "chosen_keys": chosen,
                # 名前空間の語彙を階層ごとに。ここから足す・外す（#289）。
                "groups": _vocabulary_groups(kcs, chosen),
                "scope_result": scope_result,
                "saved": SAVED_MESSAGES.get(saved),
                "saved_key": saved,
                "is_admin": _is_admin(request, me),
                # 候補。**既にあるものは採用させない**ので、突き合わせる鍵を渡す。
                "proposal": proposal,
                # 形が正準キーになっていないので落とした候補（#157）。
                # **減った件数を黙らせない。**
                "discarded": discarded,
                # **「既にある」はこのコースの範囲にあるものを指す。** 語彙に
                # あっても範囲外なら採用できる（採用すれば範囲に入る）。候補は
                # 登録済みの語彙からしか来ない（2026-09-13 決定）ので、状態は
                # 「このコースで使用中」か「語彙にあり（範囲外）」の 2 つ。
                "existing": [row["kc"].key for row in rows if not row["kc"].deprecated],
                "has_basics": bool((course.description or "").strip()),
            },
        )

    @router.post("/courses/{course_id}/kc/retire")
    def retire_kc_route(
        request: Request,
        course_id: str,
        key: Annotated[str, Form()],
        superseded_by: Annotated[str, Form()] = "",
        restore: Annotated[str, Form()] = "",
    ) -> Response:
        """KC を引退させる（または引退を取り消す）。**消さない**（P8）。

        引退は管理者のみ。**コースをまたいで効く**操作で、1 コースの教員が
        他のコースの語彙を畳めてはいけない。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        if not _is_admin(request, me):
            raise HTTPException(
                status_code=403,
                detail="知識要素の引退には管理者権限が必要です（他のコースにも効きます）",
            )
        console = _console(request)
        try:
            if restore:
                restore_kc(console.database, key=key.strip())
            else:
                retire_kc(
                    console.database,
                    key=key.strip(),
                    superseded_by_key=superseded_by.strip() or None,
                )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        saved = "kc_restored" if restore else "kc_retired"
        return RedirectResponse(
            f"/manage/courses/{course_id}/kc?saved={saved}#saved", status_code=303
        )

    def _scope_targets(console, course, kc: list[str], prefix: str) -> tuple[str, ...]:
        """足す・外す対象のキー。個別のチェックと、階層の接頭辞の両方から。

        接頭辞は `cs.loops` のように**区切りまで一致**させる（`cs.loop` で
        `cs.loops` を巻き込まない）。引退した知識要素は対象にしない。
        """
        keys = {key.strip() for key in kc if key.strip()}
        prefix = prefix.strip()
        if prefix:
            namespaces = allowed_namespaces(
                load_profile(console.profiles_dir / f"{course.subject_profile}.yaml")
            )
            for item in list_for_namespaces(console.database, namespaces, include_deprecated=False):
                if _is_component(item.key) and (
                    item.key == prefix or item.key.startswith(prefix + ".")
                ):
                    keys.add(item.key)
        return tuple(sorted(keys))

    @router.get("/course-template.yaml")
    def course_template_download(request: Request) -> Response:
        """コース定義のひな形（#292 と同じ流れの入口）。

        教員が埋めて管理者に渡し、管理者が `course apply` で投入する。
        **教員にも出す** ── 管理者だけに出すと、依頼する側が形式を知る手段が
        画面に無い。中身は静的で、学習者のデータは含まない。
        """
        from .app import require_principal

        require_principal(request)
        return Response(
            course_template(),
            media_type="application/x-yaml; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="course.yaml"'},
        )

    @router.post("/courses/{course_id}/kc/adopt")
    async def adopt_candidates(request: Request, course_id: str) -> Response:
        """候補をまとめてこのコースの範囲に入れる（画面の「印を付けた候補をまとめて」）。

        **語彙への登録は行わない**（2026-09-13 決定）。候補は登録済みの語彙から
        選ばれたものだけなので、ここですることは範囲に入れることだけ。万一
        未登録のキーが来たら断る（画面を経ない POST）。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        form = await request.form()
        keys = tuple(dict.fromkeys(str(v).strip() for v in form.getlist("adopt") if str(v).strip()))
        if not keys:
            raise HTTPException(status_code=400, detail="採用する候補に印を付けてください")
        try:
            assert_registered(console.database, keys)
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        added = _scope_in(console, course, keys)
        console.last_kc_scope = (str(course.id), "added", added, 0)
        return RedirectResponse(
            f"/manage/courses/{course_id}/kc?saved=kc_scoped#saved", status_code=303
        )

    @router.post("/courses/{course_id}/kc/scope/add")
    def add_kc_scope(
        request: Request,
        course_id: str,
        kc: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI の複数値
        prefix: Annotated[str, Form()] = "",
    ) -> Response:
        """知識要素をこのコースが使う範囲に足す ── 個別（チェック）にも、
        階層ごと（`prefix`）にも（#289）。

        **語彙への登録ではない。** 名前空間に既にあるものを、このコースの
        作問候補に入れるだけ。無いキーは黙って落とさず断る。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        keys = _scope_targets(console, course, kc, prefix)
        if not keys:
            raise HTTPException(status_code=400, detail="足す知識要素が選ばれていません")
        try:
            assert_registered(console.database, keys)
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        added = _scope_in(console, course, keys)
        console.last_kc_scope = (str(course.id), "added", added, 0)
        return RedirectResponse(
            f"/manage/courses/{course_id}/kc?saved=kc_scoped#saved", status_code=303
        )

    @router.post("/courses/{course_id}/kc/scope/remove")
    def remove_kc_scope(
        request: Request,
        course_id: str,
        kc: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI の複数値
        prefix: Annotated[str, Form()] = "",
    ) -> Response:
        """知識要素をこのコースが使う範囲から外す（#289）。

        **共有の語彙からの削除ではない。** 外しても知識要素は残り、他のコースの
        Q-matrix は壊れない。**このコースの課題が使っているものは外さない**
        ── 外すと Q-matrix が課題の中身と食い違う。残した数は結果に出す。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        keys = set(_scope_targets(console, course, kc, prefix))
        if not keys:
            raise HTTPException(status_code=400, detail="外す知識要素が選ばれていません")
        here = _kc_use_in_course(console, course)
        kept = {key for key in keys if here.get(key)}
        remaining = tuple(
            key for key in course.knowledge_components if key not in keys or key in kept
        )
        removed = len(course.knowledge_components) - len(remaining)
        if removed:
            with console.database.unit_of_work() as uow:
                uow.identity.save_course(
                    course.model_copy(update={"knowledge_components": remaining})
                )
                uow.commit()
        console.last_kc_scope = (str(course.id), "removed", removed, len(kept))
        return RedirectResponse(
            f"/manage/courses/{course_id}/kc?saved=kc_scoped#saved", status_code=303
        )

    @router.post("/courses/{course_id}/kc/edit")
    def edit_kc_route(
        request: Request,
        course_id: str,
        key: Annotated[str, Form()],
        label: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
    ) -> Response:
        """名前と説明を直す。**キーは直せない。**

        **引退・削除と違って教員が直せる。** あちらは他のコースが使って
        いるものを取り上げる操作なので管理者に限るが、名前を直すのは
        取り上げる操作ではない。正しい名前を知っているのは科目の専門家で
        あり（`aijudge_admin.kc` の冒頭）、キーは動かないので壊れない。
        間違えても、もう一度直せる。
        """
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        try:
            edit_kc(
                console.database,
                key=key.strip(),
                label=label,
                description=description,
            )
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(
            f"/manage/courses/{course_id}/kc?saved=kc_edited#saved", status_code=303
        )

    @router.post("/courses/{course_id}/kc/delete")
    def delete_kc_route(
        request: Request,
        course_id: str,
        key: Annotated[str, Form()],
    ) -> Response:
        """**一度も使われていない知識要素だけを消す。**

        引退（`retire`）は「使っていたが今後は使わない」を表す記録で、
        打ち間違いの置き場所ではない。使われたことの無い `cs.c_langauge` を
        引退させて残すと、コースをまたいで共有される一覧に、誰の役にも
        立たない行が永久に並ぶ。

        使われている KC は消さない ── 判定は `aijudge_admin.kc.delete` が
        持つ（利用状況を数えられるのはあちら）。

        引退と同じく管理者のみ。**コースをまたいで効く。**
        """
        from .app import require_principal

        me = require_principal(request)
        _require_instructor(request, me, CourseId(course_id))
        if not _is_admin(request, me):
            raise HTTPException(
                status_code=403,
                detail="知識要素の削除には管理者権限が必要です（他のコースにも効きます）",
            )
        console = _console(request)
        try:
            delete_kc(console.database, key=key.strip())
        except AdminError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(
            f"/manage/courses/{course_id}/kc?saved=kc_deleted#saved", status_code=303
        )

    # ------------------------------------------------------------------
    # 生成された課題のレビュー（S2、設計方針 §5）
    # ------------------------------------------------------------------

    @router.get("/courses/{course_id}/drafts", response_class=HTMLResponse)
    def draft_queue(request: Request, course_id: str, saved: str = "") -> Response:
        """レビュー待ちの生成課題。

        **科目プロファイルと違い、ここはブラウザから触ってよい**（ADR 0002）。
        あちらは評価器の指名とタイムアウトを持つ採点の設定で、壊すと全員の
        採点が止まる。課題を承認するかどうかは、まさに教員が決めることである。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        rows = []
        with console.database.unit_of_work() as uow:
            for draft in uow.tasks.list_drafts(course.id):
                # 改訂なら、いま学習者に出ている版と比べる（#306）。
                published = (
                    uow.tasks.latest_published_version(draft.task_id)
                    if draft.task_id is not None
                    else None
                )
                rows.append(
                    {
                        "draft": draft,
                        "revision": draft.kind is DraftKind.REVISION,
                        "published": published,
                        # 改訂の差分。**承認は差分を見て決めるもの**で、
                        # 書き換わった問題文を頭から読み直させない。
                        "diff": (
                            _statement_diff(published.statement, draft.spec.statement)
                            if published is not None
                            else ()
                        ),
                        # **提出済みであることを言う。** 出題済みの課題の問題文を
                        # 書き換えると、既に提出した学習者と後から提出する学習者で
                        # 違う問題になる。過去の採点は自分の版を指したまま残る
                        # （P8）ので壊れないが、承認するかどうかの判断材料である。
                        "submissions": (
                            uow.tasks.submission_count(draft.task_id)
                            if draft.task_id is not None
                            else 0
                        ),
                        "kc_keys": draft.spec.knowledge_components,
                        # 検査していない下書きも並べる。**隠さない** ── 見えない
                        # ものは承認も却下もされず、待ち行列に溜まり続ける。
                        "checks": draft.checks,
                        "clean": bool(draft.checks and draft.checks.verification.usable),
                        # 学習者に出る形（#105 と同じ関数）。承認は「学生が読む
                        # 画面」を見て決めるもので、Markdown の生文だけでは
                        # 数式・コードの囲み・画像が意図どおりかが分からない。
                        "statement_html": render_statement(draft.spec.statement),
                    }
                )
        return templates.TemplateResponse(
            request,
            "manage_drafts.html",
            {
                "me": me,
                "course": course,
                "section": {
                    "label": "未承認の課題（AI 作問）",
                    "href": f"/manage/courses/{course.id}/drafts",
                },
                "rows": rows,
                "min_reason": MIN_JUSTIFICATION_LENGTH,
                # 承認のときに出題先を選ぶ（#84）。**作問の時点では決めない。**
                "units": [
                    {"key": group.key, "unit": group.unit, "label": group.label}
                    for group in _units_of(console, course)
                    if group.unit
                ],
                # ここからも作れる（#84）。**入口は 2 つ、作り方は 1 つ。**
                "kcs": _course_kcs(console, course),
                "difficulties": [d.value for d in Difficulty],
                "saved": SAVED_MESSAGES.get(saved),
                "saved_key": saved,
            },
        )

    @router.post("/courses/{course_id}/drafts/generate")
    def generate_without_unit(
        request: Request,
        course_id: str,
        key_suffix: Annotated[str, Form()],
        kc: Annotated[list[str], Form()] = [],  # noqa: B006 - FastAPI の複数値
        difficulty: Annotated[str, Form()] = "standard",
        instructions: Annotated[str, Form()] = "",
        test_cases: Annotated[str, Form()] = "5",
        readability_weight: Annotated[str, Form()] = "0.3",
    ) -> Response:
        """問題セットを決めずに作る（#84）。

        **出題先は承認のときに決める。** 使えるかどうかは作ってみないと
        分からないので、決めてから却下すると、そのセットの一覧に残骸が並ぶ。
        中身は問題セットからの生成と同じ経路を通る ── 入口が 2 つあっても、
        作り方は 1 つでなければならない。
        """
        return generate_task(
            request,
            course_id,
            "",
            key_suffix=key_suffix,
            kc=kc,
            difficulty=difficulty,
            instructions=instructions,
            test_cases=test_cases,
            readability_weight=readability_weight,
        )

    @router.post("/courses/{course_id}/drafts/{draft_id}")
    async def decide_draft(request: Request, course_id: str, draft_id: str) -> Response:
        """下書きを**採用**するか**捨てる**（#321）。

        採用したときに初めて課題になる ── それまで課題は存在しない
        （`aijudge_authoring.draft_store` の冒頭）。だから**採用の瞬間まで
        何でも直せる**: 課題キー、題名、問題文、出題する問題セット。

        **捨てるのは即時の削除である**（ADR 0019）。却下した下書きは残さない
        ── 承認率を測るための記録も残らないが、その数は「生成の質」ではなく
        「いまのプロンプト × この教員の好み」を測っており、単一の合格基準に
        する意味が無いと判断した（2026-09-15）。

        改訂の下書き（#306）は**キーを変えられない** ── 同じ課題の書き直しで
        あって別の課題ではない。採用すると新しい版になる。
        """
        from .app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)

        form = await request.form()
        decision = str(form.get("decision") or "")
        with console.database.unit_of_work() as uow:
            draft = uow.tasks.get_draft(draft_id)
        if draft is None or draft.course_id != course.id:
            # 存在と権限を区別しない（他コースの下書きを探らせない）。
            raise HTTPException(status_code=404, detail="下書きが見つかりません")

        if decision != "approve":
            # **捨てる。** 理由は聞かない（記録しないのだから聞く意味が無い）。
            with console.database.unit_of_work() as uow:
                uow.tasks.delete_draft(draft_id)
                uow.commit()
            return RedirectResponse(
                f"/manage/courses/{course_id}/drafts?saved=draft_dropped#saved", status_code=303
            )

        statement = str(form.get("statement") or draft.spec.statement)
        title = str(form.get("title") or draft.spec.title or "").strip() or None
        unit = str(form.get("unit") or draft.unit).strip()
        if draft.kind is DraftKind.REVISION:
            # 改訂はキーを変えない（同じ課題の書き直し）。
            key = draft.spec.key
        else:
            suffix = str(form.get("key_suffix") or "").strip()
            key = _compose_key(unit, suffix) or draft.spec.key
        if not key:
            raise HTTPException(status_code=400, detail="課題キーを入力してください")

        try:
            spec = draft.spec.model_copy(
                update={
                    "key": key,
                    "title": title,
                    "statement": statement,
                    "unit": unit or draft.spec.unit,
                }
            )
        except (ValidationError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"課題の指定が不正です: {exc}") from None

        if draft.kind is DraftKind.REVISION:
            # **既にある課題の新しい版にする。** `save_task` は第 1 版を作る
            # 経路なので、同じ鍵で呼ぶと「内容が違う」と断られる（P8）。
            task = _task_of(console, course, str(draft.task_id))
            with console.database.unit_of_work() as uow:
                current = uow.tasks.latest_version(draft.task_id)
            if current is None:
                raise HTTPException(status_code=404, detail="課題が見つかりません")
            saved = _save_revision(
                console,
                me,
                course,
                task,
                current,
                statement=statement,
                criteria=rubric.from_criteria(current.criteria),
                aggregation=current.aggregation,
                position=task.position,
                accepted=task.accepted_suffixes,
                reference_solution=current.reference_solution,
                test_cases=_kept_cases(current, editing=()),
                knowledge_components=draft.spec.knowledge_components or None,
                generated_by=draft.generated_by or None,
                generation_prompt_version=draft.generation_prompt_version or None,
                # **採用した時点で承認済み。** 承認の段はここ 1 つで足りる。
                review_state=ReviewState.APPROVED,
            )
        else:
            try:
                saved = save_task(
                    console.database,
                    course_id=course.id,
                    spec=spec,
                    subject_profile=draft.subject_profile or course.subject_profile,
                    authored_by=me.user_id,
                    # **出所は残す**（P8）。承認したのは人だが、書いたのは
                    # モデルである ── どのプロンプト版が出したものかを辿れる。
                    generated_by=draft.generated_by or None,
                    generation_prompt_version=draft.generation_prompt_version or None,
                    review_state=ReviewState.APPROVED,
                )
            except AdminError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

        # 検査の記録は課題版に紐づけ直す（下書きは消える）。
        if draft.checks is not None:
            with console.database.unit_of_work() as uow:
                uow.tasks.save_checks(saved.version.id, draft.checks)
                uow.commit()

        # 日程と提出形式は問題セットから引き継ぐ（手で足した課題と同じ）。
        if unit:
            _place_in_unit(console, course, saved.task, unit)

        with console.database.unit_of_work() as uow:
            uow.tasks.delete_draft(draft_id)
            uow.commit()

        console.last_task = (str(course.id), saved)
        return RedirectResponse(
            f"/manage/courses/{course_id}/drafts?saved=draft_approved#saved", status_code=303
        )

    return router
