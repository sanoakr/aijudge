"""課題の検証データ（テストケース・項目表・参照解答・入力の提案）（段階 4-11）。

採点の根拠になる入出力・項目・参照解答。**変えると採点が変わる**ので、本文の編集とは
分けた。保存は `task_page._save_revision` を通り、評価器と payload ごと持ち越す
（`_kept_cases`・#302）。`manage/__init__.py` の `register()` が、元のルートがあった位置で
ここの `register` を呼ぶ。
"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.templating import Jinja2Templates

from aijudge_authoring.spec import AI_EVALUATOR, TestCaseSpec
from aijudge_core import TestCase
from aijudge_core.ids import CourseId, TaskId
from aijudge_course_admin import rubric
from aijudge_course_admin.task_verifier import TaskVerifier, outputs_for
from aijudge_course_admin.test_cases import InputProposer, SolutionWriter, TestCaseWriter
from aijudge_eval_code_test_runner import EVALUATOR_ID as CODE_TEST_RUNNER
from aijudge_grading import EvaluatorRegistry, load_profile, test_case_shape

from .. import notices
from ..companion_form import CompanionFormError, companion_cases_from_form
from ..overview import unit_key
from ..urls import RedirectResponse
from .common import _console, _require_instructor
from .task_page import (
    _data_driven_criteria,
    _kept_cases,
    _language_of,
    _record_gates,
    _save_revision,
    _task_of,
    _task_page,
)


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


def _test_evaluator_of(profile) -> str:
    """この科目でテスト実行を担う評価器。宣言の順に見て最初のもの。"""
    for evaluator_id in profile.deterministic:
        if evaluator_id == CODE_TEST_RUNNER:
            return evaluator_id
    return CODE_TEST_RUNNER


def register(router: APIRouter, templates: Jinja2Templates) -> None:
    """この領域のルートを `router` に登録する（登録の位置は呼ぶ側が決める）。"""

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
            console.notices.put(
                me.user_id,
                course.id,
                notices.TEST_CASE_ERROR,
                f"{type(exc).__name__}: {exc}",
                scope=task_id,
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

    @router.post("/courses/{course_id}/tasks/{task_id}/companion/edit")
    async def edit_companion_cases(request: Request, course_id: str, task_id: str) -> Response:
        """クライアント・サーバのケースを直して新しい版にする（2026-10-01）。

        以前は画面から直せず、course.yaml を書き換えて `course apply --revise` するしか
        なかった。**既存の版は書き換えない**（P8）── 項目表の編集と同じく新しい版を作る。

        伴走プロセスのソースは**名前で 1 つにまとめて**編集し、ケースはその名前で指す
        （取り込みはケースごとに写しを入れるが、同じものを何か所も直させない）。
        **画面に出していない値は捨てない** ── 付属ファイルなど、ケースの `payload` に
        ある他の値は元のケースから引き継ぐ。
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
        named = _data_driven_criteria(registry, version).get("companion", ())
        if not named:
            raise HTTPException(
                status_code=400,
                detail="この課題の観点はクライアント・サーバのケースを読む評価器を指名していません",
            )
        form = await request.form()
        try:
            cases = companion_cases_from_form(form, version, evaluator_id=named[0])
        except CompanionFormError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

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
            test_cases=cases + _kept_cases(version, editing=named),
        )
        return RedirectResponse(
            f"/manage/courses/{course_id}/tasks/{task_id}/edit?saved=companion_revised#saved",
            status_code=303,
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
