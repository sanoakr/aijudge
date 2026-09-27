"""コース・課題・受講の管理（教員向け）。

**YAML の直接編集を運用の前提にしない**（設計方針 §9.2 Phase 2）。学期の頭に
やることは CLI（`aijudge-admin`）でもできるが、学期中に発生する作業
── 締切の設定、受講者の追加、課題の追加 ── を教員がターミナルで行うのは
現実的でない。

課題の追加は**画面と API の両方から**でき、保存は同じ経路を通る
（`aijudge_course_admin.authoring.save_task`）。zip での一括取り込みは廃止した ── 移行元
（Sharif Judge）の形式をサーバの入口の語彙にしてしまっており、移行が
終わったあとも一生ついて回る形だった。まとまった投入は API で行う
（`api.py`）。

**コースが使っている科目プロファイル（`*.yaml`）は読み取り専用。** 1 つの
プロファイルは複数のコースの雛形になりうるので、書き換えると自分が担当して
いないコースの採点まで変わる（ADR 0002 の「コードと同じ扱いでレビューを
通す」はこの範囲のこと）。

そこで #146 で編集できる範囲を絞った ── **未参照のものだけ直接編集・改名
でき、参照中のものへの唯一の操作は「複製して編集」**（`/manage/subjects`）。
判定は `aijudge_course_admin.profiles` が持ち、この層は画面と繋ぐだけ。判定を画面に
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

**このパッケージは画面の領域ごとに分けていく途中**（段階的な立て直しの段階 4、
地図は `docs/design/manage-split-map.md`）。移した領域のモジュールは
`register(router, templates)` を持ち、ここの `register()` がそれを**元のルートが
あった位置で**呼ぶ。FastAPI は登録順にパスを照合するので（`/tasks/new` と
`/tasks/{task_id}` など）、呼ぶ位置を変えると別のハンドラが応答しうる ── 横取りは
`test_manage_route_order.py` が見張る（ルートの写しは行を並べ替えて比べるので、
順序の変化は捕まえない）。
"""

from __future__ import annotations

import difflib
import json
import re
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from aijudge_audit import AuditAction
from aijudge_authoring import (
    DraftKind,
    TaskChecks,
    TaskDraftRecord,
    TaskSpec,
    images,
    render_statement,
)
from aijudge_authoring.drafting import Blueprint, Difficulty
from aijudge_authoring.spec import AI_EVALUATOR, TestCaseSpec
from aijudge_core import (
    MAX_TASK_CASE_TIMEOUT_SECONDS,
    MIN_JUSTIFICATION_LENGTH,
    ReviewState,
    Task,
    TestCase,
    new_id,
)
from aijudge_core.ids import CourseId, TaskId
from aijudge_course_admin import rubric
from aijudge_course_admin.authoring import save_task
from aijudge_course_admin.bundle_plan import plan_bundle
from aijudge_course_admin.bundles import read_bundle, template_bundle
from aijudge_course_admin.drafting import TaskDrafter
from aijudge_course_admin.errors import AdminError
from aijudge_course_admin.finalization import finalize_task
from aijudge_course_admin.kc import assert_registered
from aijudge_course_admin.task_relocation import compose_key
from aijudge_course_admin.task_relocation import copy_task as copy_task_as
from aijudge_course_admin.task_relocation import move_task as relocate_task
from aijudge_course_admin.task_verifier import TaskVerifier, outputs_for
from aijudge_course_admin.tasks import delete as delete_task
from aijudge_course_admin.tasks import withdraw as withdraw_task
from aijudge_course_admin.test_cases import InputProposer, SolutionWriter, TestCaseWriter
from aijudge_eval_code_test_runner import EVALUATOR_ID as CODE_TEST_RUNNER
from aijudge_grading import EvaluatorRegistry, load_profile, test_case_shape
from aijudge_submission import SubmissionService

from ..audit_context import recorder_for
from ..overview import empty_unit, find_unit, load_units, unit_key
from ..urls import RedirectResponse
from .common import (
    _console,
    _course_kcs,
    _first_error,
    _normalized_unit,
    _parse_when,
    _plain,
    _require_instructor,
)
from .course import register as register_course
from .groups import register as register_groups
from .kc import register as register_kc
from .learners import register as register_learners
from .messages import SAVED_MESSAGES
from .subjects import register as register_subjects
from .task_page import (
    _chosen_suffixes,
    _data_driven_criteria,
    _failed_without_run,
    _kept_cases,
    _language_of,
    _record_gates,
    _save_revision,
    _task_of,
    _task_page,
)
from .tasks import register as register_tasks
from .units import register as register_units
from .users import register as register_users

# ルータは `register()` の中で毎回作る。モジュール階層に置くと、
# `create_app` を 2 回呼んだときに同じ経路が二重に登録される
# （テストで複数のアプリを作ると起きる。FastAPI が Duplicate Operation ID を
# 警告して気づいた）。


#: 入出力の組を読む評価器（この画面が編集している側）。**名前で書かない** ──
#: 評価器が形を名乗るので、そこから引く（`_io_evaluator_ids`）。
def _io_evaluator_ids(registry) -> tuple[str, ...]:
    return tuple(name for name in registry.ids() if test_case_shape(registry.get(name)) == "io")


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


#: 実行時間の上限（#491）が範囲外のときの知らせ。
_CASE_TIMEOUT_REFUSED = (
    f"実行時間の上限は 0 より大きく {MAX_TASK_CASE_TIMEOUT_SECONDS:g} 秒以下の数で入れてください"
)


def _unit_href(course_id: str, task) -> str:
    """その課題が属する回のページ。

    課題を触る操作（締切・一括確定・追加）は**その回のページから来る**ので、
    そこへ戻す。コースのトップに返すと、教員は毎回同じ回を開き直すことになる。
    """
    return f"/manage/courses/{course_id}/units/{unit_key(task)}"


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

    **入口は作問ページ 1 つ**（`POST /drafts/generate`・#522）。以前は問題
    セットの画面にも生成フォームがあり、入口が 2 つに分かれていた（#84 で作問の
    区分を作ったあとも残っていた）。`unit` は出題先の**候補**で、承認のときに
    変えられる。

    **KC は登録済みからの選択だけ。** モデルはもっともらしいキーを
    いくらでも作るので、自由入力にすると体系が静かに荒れる
    （`aijudge_course_admin.kc` の規則 4）。

    `avoid_similar_to` にはこのコースの既存課題を入れる ── 「似せない」
    材料が無いと、既存課題の言い換えが出てくる。

    生成物はここでは保存するだけで、門・解答可能性・重複の検査は
    `aijudge-authoring` が担う（ADR 0008）。ここが返すのは候補であって
    課題ではない。
    """
    from ..app import require_principal

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
            task for task in uow.tasks.list_for_course(CourseId(course_id)) if unit_key(task) == key
        ]
        # **似せないための材料。** 既存課題の本文を渡す（学習者のデータは
        # 含まないので、外部モデルにも渡してよい・設計原則 P7）。
        avoid = []
        for task in uow.tasks.list_for_course(CourseId(course_id)):
            version = uow.tasks.latest_version(task.id)
            if version is not None:
                avoid.append(version.statement)

    head = siblings[0] if siblings else None
    full_key = compose_key(head.unit if head else unit, key_suffix.strip())
    if not full_key:
        raise HTTPException(status_code=400, detail="課題キーを入力してください")

    try:
        blueprint = Blueprint(
            knowledge_components=chosen,
            subject_profile=course.subject_profile,
            # **コースの範囲を渡す。** KC は「何を問うか」を決めるが、
            # 「どこまでを既習として書いてよいか」は決めない。空なら
            # 節ごと出さない（`aijudge_course_admin.drafting._course_section`）。
            course_title=course.title,
            course_outline=course.description or "",
            difficulty=Difficulty(difficulty),
            language=_language_of(profile),
            instructions=tuple(line.strip() for line in instructions.splitlines() if line.strip()),
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

    spec = result.spec.model_copy(update={"readability_weight": float(readability_weight or 0.0)})
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
                    "input": (inputs[index] if index < len(inputs) else "").replace("\r\n", "\n"),
                    "expected": (expected[index] if index < len(expected) else "").replace(
                        "\r\n", "\n"
                    ),
                },
                hidden=(hidden[index] if index < len(hidden) else "1") != "0",
                weight=weight,
            )
        )
    return tuple(out)


def register(templates: Jinja2Templates) -> APIRouter:
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

    register_users(router, templates)

    register_subjects(router, templates)

    register_course(router, templates)

    register_units(router, templates)

    # -- 束（zip）で課題を入れる（#161）------------------------------------
    #
    # **読み取り → 確認 → 人が保存**（シラバス読み取りと同じ作法）。押した
    # 瞬間に何十件も入る操作にしない ── まとまった投入で怖いのは「押したら
    # 何件変わったか分からない」ことである。
    #
    # 受け取る構造はこのシステム自身の語彙（`aijudge_course_admin.bundles`）。
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
        from ..app import require_principal

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
        from ..app import require_principal

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
                        "key": compose_key(group.unit or "", spec.key),
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
    ) -> Response:
        """確認した束を保存する。**保存は既存の経路（`save_task`）を通す。**

        **承認済みで入る**（#522）。束は教員が問題セットのひな形を埋めて
        アップロードするもので、入れる本人が書き、確認画面で読んでいる。
        以前は既定を未承認にしていたが（#161「他所で書かれたもの」）、同じ中身を
        `course apply` やディレクトリの取り込みで入れると承認済みになる不整合と、
        未承認の版を画面から承認する経路が無い穴があった。**承認が要るのは
        AI の生成物だけ**（設計原則 P5）。
        """
        from ..app import require_principal

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
                )
            except AdminError as exc:
                raise HTTPException(status_code=409, detail=f"{spec.key}: {exc}") from None

        return RedirectResponse(
            f"/manage/courses/{course.id}/units/{group.key}?saved=bundle_saved#saved",
            status_code=303,
        )

    register_groups(router, templates)

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
        from ..app import require_principal

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

    register_tasks(router, templates)

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
        from ..app import require_principal

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
            # 生成したのは入出力の組だけ。**他の評価器あてのデータは持ち越す**
            # （#302・#402）── 作り直すと、同じ課題の項目表や伴走プロセスの
            # ケースが黙って消える。
            test_cases=tuple(
                TestCaseSpec(name=case.name, input=case.input, expected=case.expected)
                for case in generated.test_cases
            )
            + _kept_cases(version, editing=_io_evaluator_ids(EvaluatorRegistry().load_installed())),
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
        from ..app import require_principal

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
            failed = _failed_without_run(uow, task)
        queued = 0
        targets = [
            submission
            for submission, run, _request in rows
            if run.context.task_version_id != version.id
        ] + list(failed)
        for submission in targets:
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
        （`aijudge_course_admin.kc`）、使われたものは取り下げ、一度も使われていない
        ものだけを消す。
        """
        from ..app import require_principal

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
        """**提出が 1 件も無い課題だけを消す。** 判定は `aijudge_course_admin.tasks`。

        提出があれば断り、取り下げを案内する（規則の置き場所を 1 つにする）。
        """
        from ..app import require_principal

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
        from ..app import require_principal

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
        name: Annotated[str, Form()] = "",
    ) -> Response:
        """課題を別の問題セットへ移す（`name` を変えれば名前の変更も）。

        **キーを付け替える**（`<問題セット>/<名前>`・2026-09-27）。頭は問題セットを
        表すので、付け替えないと test5 にある課題のキーが `test4/…` のままになる。
        課題 ID はキーから導かれるので、**学生の提出がある課題は移せない**
        ── 規則と付け替えは `aijudge_course_admin.task_relocation` が持つ。
        お試しの提出は妨げず、付け替えのときに消える。

        **日程は移った先に揃える。** セットの中で締切がずれると、学習者にも
        教員にも「この回はいつまでか」が言えなくなる（`set_unit_schedule` と
        同じ理由）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)

        try:
            moved = relocate_task(
                console.database,
                task_id=task.id,
                unit=unit,
                name=name,
                artifact_store=console.store,
            )
        except AdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # 同じセットの中で名前だけ変えたなら課題の画面へ、移したなら移動先へ。
        # **元の URL はもう無い**（課題 ID が変わった）。
        if moved.task.unit == task.unit:
            return RedirectResponse(
                f"/manage/courses/{course_id}/tasks/{moved.task.id}/edit?saved=renamed#saved",
                status_code=303,
            )
        return RedirectResponse(
            f"/manage/courses/{course_id}/units/{unit_key(moved.task)}?saved=moved#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/copy")
    def copy_task_to_unit(
        request: Request,
        course_id: str,
        task_id: str,
        unit: Annotated[str, Form()] = "",
        name: Annotated[str, Form()] = "",
    ) -> Response:
        """課題を名前を付けて別の問題セットへ**コピー**する。元は残る。

        学生の提出がある課題は移せないので、別のセットで使うにはこちらを通る
        （2026-09-27）。写すのは中身（全版）と設定で、提出と採点は写さない。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)

        try:
            copied = copy_task_as(console.database, task_id=task.id, unit=unit, name=name)
        except AdminError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{copied.task.id}/edit?saved=copied#saved",
            status_code=303,
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
        from ..app import require_principal

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

        # **この欄が直している評価器**（#402）。いまの版で入出力の形を読む
        # 評価器のデータから取る。既定（`code_test_runner`）に倒すのは入出力の
        # データがまだ無いときだけ ── 倒すと、別の入出力評価器あてのケースが
        # 保存のたびに書き換わる。
        io_ids = _io_evaluator_ids(EvaluatorRegistry().load_installed())
        io_evaluator = next(
            (case.evaluator_id for case in version.test_cases if case.evaluator_id in io_ids),
            CODE_TEST_RUNNER,
        )

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
                    evaluator=io_evaluator,
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
                    evaluator=io_evaluator,
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
            candidate = version.model_copy(
                update={
                    "test_cases": tuple(
                        TestCase(
                            name=case.name,
                            evaluator_id=io_evaluator,
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
            test_cases=tuple(cases) + _kept_cases(version, editing=io_ids),
        )
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=tests_revised#saved",
            status_code=303,
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
        from ..app import require_principal

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

    @router.post("/courses/{course_id}/tasks/{task_id}/settings")
    async def save_task_settings(request: Request, course_id: str, task_id: str) -> Response:
        """「この問題の設定」をまとめて保存する（日程・提出形式・実行時間の上限）。

        問題のページの操作のタブを詰めたとき（2026-09-27）、版を上げない 3 つの値を
        1 枚のフォームにした。**版は上がらない** ── どれも課題の内容ではない。

        **監査は変わったものごとに、今までと同じ文言で残す**（`set_task_schedule`・
        `set_task_case_timeout` と同じ `summary`・`field`）。締切は誰がいつ動かしたかを
        言えないといけない（ADR 0013）ので、まとめて保存しても日程の記録は日程の記録。
        変わっていないものは記録しない（触っていない値の行を積まない）。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)
        form = await request.form()

        def text(name: str) -> str:
            return str(form.get(name) or "")

        schedule = {
            name: _parse_when(text(name))
            for name in (
                "opens_at",
                "submissions_open_at",
                "due_at",
                "grading_starts_at",
                "accepts_until",
            )
        }
        update: dict[str, object] = dict(schedule)
        # 提出形式。印（`formats`）付きで来たときだけ置き換え、空は断る（`_chosen_suffixes`）。
        if form.get("formats"):
            update["accepted_suffixes"] = _chosen_suffixes(
                [str(v) for v in form.getlist("suffix")], "1", course
            )
        # 実行時間の上限は**欄があるときだけ**（コードを走らせない課題には出さない）。
        if "case_timeout_seconds" in form:
            raw = text("case_timeout_seconds").strip()
            try:
                update["case_timeout_seconds"] = float(raw) if raw else None
            except ValueError:
                raise HTTPException(status_code=400, detail=_CASE_TIMEOUT_REFUSED) from None
        try:
            # **`model_copy` を使わない。** 検証を走らせないので、締切が公開より
            # 前の課題がそのまま保存される（`set_task_schedule` と同じ理由）。
            updated = Task.model_validate(task.model_dump() | update)
        except ValidationError as exc:
            if any(e["loc"] == ("case_timeout_seconds",) for e in exc.errors()):
                raise HTTPException(status_code=400, detail=_CASE_TIMEOUT_REFUSED) from None
            raise HTTPException(status_code=400, detail=_first_error(exc)) from None

        def changed(names) -> dict[str, dict[str, object]]:
            return {
                name: {
                    "before": _plain(getattr(task, name)),
                    "after": _plain(getattr(updated, name)),
                }
                for name in names
                if getattr(task, name) != getattr(updated, name)
            }

        records = [
            ("課題の日程を変えた", "task_schedule", changed(schedule)),
            ("課題の提出形式を変えた", "accepted_suffixes", changed(["accepted_suffixes"])),
            (
                "課題の実行時間の上限を変えた",
                "case_timeout_seconds",
                changed(["case_timeout_seconds"]),
            ),
        ]
        with console.database.unit_of_work() as uow:
            uow.tasks.save_task(updated)
            recorder = recorder_for(uow, request, me)
            for summary, field, diff in records:
                if not diff:
                    continue
                recorder.record(
                    AuditAction.TASK_UPDATED,
                    target_type="task",
                    target_id=str(task.id),
                    summary=summary,
                    detail={"course_id": course_id, "field": field, "changed": diff},
                )
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=task_settings#saved",
            status_code=303,
        )

    @router.post("/courses/{course_id}/tasks/{task_id}/case-timeout")
    def set_task_case_timeout(
        request: Request,
        course_id: str,
        task_id: str,
        case_timeout_seconds: Annotated[str, Form()] = "",
    ) -> Response:
        """**この課題だけ**テストケース 1 件の実行時間の上限を変える（#491）。

        数値計算のように、正しい解でも時間のかかる問題のため。空欄は既定
        （科目・コースの値）に戻す。版は上がらない ── 上限はコースの採点設定でも
        版を作らずに変えられ、それを課題単位に細かくしたもの。適用した値は採点の
        記録（`model_params`）に残る。
        """
        from ..app import require_principal

        me = require_principal(request)
        course = _require_instructor(request, me, CourseId(course_id))
        console = _console(request)
        task = _task_of(console, course, task_id)

        raw = case_timeout_seconds.strip()
        try:
            seconds = float(raw) if raw else None
            updated = Task.model_validate(task.model_dump() | {"case_timeout_seconds": seconds})
        except ValueError:
            raise HTTPException(status_code=400, detail=_CASE_TIMEOUT_REFUSED) from None

        with console.database.unit_of_work() as uow:
            uow.tasks.save_task(updated)
            recorder_for(uow, request, me).record(
                AuditAction.TASK_UPDATED,
                target_type="task",
                target_id=str(task.id),
                summary="課題の実行時間の上限を変えた",
                detail={
                    "course_id": course_id,
                    "field": "case_timeout_seconds",
                    "changed": {
                        "case_timeout_seconds": {
                            "before": task.case_timeout_seconds,
                            "after": seconds,
                        }
                    },
                },
            )
            uow.commit()
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=task_case_timeout#saved",
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
        from ..app import require_principal

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
                templates,
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
            templates,
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
        from ..app import require_principal

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
                templates,
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
        from ..app import require_principal

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

    register_learners(router, templates)

    # ------------------------------------------------------------------
    # 知識要素（KC）の体系（設計原則 P6）
    # ------------------------------------------------------------------

    register_kc(router, templates)

    # ------------------------------------------------------------------
    # 生成された課題のレビュー（S2、設計方針 §5）
    # ------------------------------------------------------------------

    @router.get("/courses/{course_id}/drafts", response_class=HTMLResponse)
    def draft_queue(request: Request, course_id: str, saved: str = "", unit: str = "") -> Response:
        """レビュー待ちの生成課題。

        **科目プロファイルと違い、ここはブラウザから触ってよい**（ADR 0002）。
        あちらは評価器の指名とタイムアウトを持つ採点の設定で、壊すと全員の
        採点が止まる。課題を承認するかどうかは、まさに教員が決めることである。
        """
        from ..app import require_principal

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
                # AI 作問の入口はここだけ（#522）。問題セットの画面から来たら、
                # そのセットを出題先の候補に選んでおく（`?unit=`）。
                "kcs": _course_kcs(console, course),
                "candidate_unit": unit,
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
        unit: Annotated[str, Form()] = "",
    ) -> Response:
        """AI に課題を作らせる。**AI 作問の入口はここだけ**（#522）。

        **出題先は承認のときに決める**（#84）。`unit` は候補で、空なら決めない。
        使えるかどうかは作ってみないと分からないので、先に決めて却下すると、
        そのセットの一覧に残骸が並ぶ。問題セットの画面からは、そのセットを候補に
        選んだ状態でここへ来る（`?unit=`）。
        """
        return generate_task(
            request,
            course_id,
            unit,
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
        from ..app import require_principal

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
            key = compose_key(unit, suffix) or draft.spec.key
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
