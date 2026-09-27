"""課題の画面の組み立てと保存（段階 4-9）。

課題の 5 つのモジュール（本文の編集・検証データ・運用・下書き・束）が共有する部分。
**保存の規則はここ 1 か所** ── 訂正は `_save_revision` を通り、検証データは `_kept_cases` で
評価器と payload ごと持ち越す（#302）。画面の組み立ては `_task_page`。キーの読み出し
（`_key_of`）もここに置く。モジュールごとに書き写すと、片方だけ直る。
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from aijudge_authoring import TaskChecks, TaskSpec, images, render_statement
from aijudge_authoring.spec import TestCaseSpec
from aijudge_core import (
    DEFAULT_UPLOAD_SUFFIXES,
    MAX_TASK_CASE_TIMEOUT_SECONDS,
    SUFFIX_GROUPS,
    Course,
    EvaluatorKind,
    normalize_suffixes,
)
from aijudge_core.ids import TaskId, derived_id
from aijudge_course_admin import rubric
from aijudge_course_admin.authoring import save_task
from aijudge_course_admin.errors import AdminError
from aijudge_course_admin.task_relocation import key_name, learner_submission_count
from aijudge_course_admin.task_verifier import TaskVerifier
from aijudge_eval_code_test_runner import DEFAULT_CASE_TIMEOUT_SECONDS
from aijudge_eval_code_test_runner import EVALUATOR_ID as CODE_TEST_RUNNER
from aijudge_grading import EvaluatorRegistry, load_profile, test_case_shape

from ..overview import load_units
from .common import _console, _course_kcs, _kc_keys_of
from .grading_views import (
    _course_rubric_rows,
    _declared_rows,
    _effective_profile_of,
    _evaluator_rows,
    _transcription_note,
    _undeclared_evaluators,
)
from .messages import SAVED_MESSAGES

#: 入出力を実際に走らせる評価器。課題ごとの実行上限（#491）はこれらにだけ意味がある。
_RUNS_CODE = (CODE_TEST_RUNNER, "network_test_runner")


def _case_timeout_view(effective, task) -> dict[str, object] | None:
    """課題ページの「実行時間の上限」欄に出す値（#491）。

    空欄のとき何秒になるか（科目・コースの既定）を並べて出す ── 出さないと、
    延ばすべきかどうかを教員が判断できない。評価器は科目の予算
    （`timeout_seconds`）で頭打ちにするので、それも出す。
    """
    if task is None or effective is None:
        return None
    runners = [name for name in _RUNS_CODE if name in effective.deterministic]
    if not runners:
        return None
    default = effective.evaluator_options.get(runners[0], {}).get(
        "case_timeout_seconds", DEFAULT_CASE_TIMEOUT_SECONDS
    )
    return {
        "value": task.case_timeout_seconds,
        "default": default,
        "budget": effective.timeout_seconds,
        "max": min(MAX_TASK_CASE_TIMEOUT_SECONDS, effective.timeout_seconds),
    }


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


def _failed_without_run(uow, task):
    """この課題で、**採点が上限まで落ちて結果を持たない**提出。

    `unfinalized_for_task` は採点結果（`GradingRun`）のある提出しか返さない
    ので、採点自体が失敗した提出は「いまの版で再採点」に数えられなかった。
    一方、問題セットの「流し直す」は同じジョブを再実行するだけで、提出が
    指す古い版に固定される ── 課題を訂正して直したケースがどちらからも
    届かなかった（prog2 ex01-2、2026-09-22）。訂正後の版で採点し直す対象に
    含める。結果を持つものは含めない（それは上の経路が扱う）。
    """
    version_ids = [version.id for version in uow.tasks.list_versions(task.id)]
    if not version_ids:
        return ()
    submissions = uow.submissions.list_for_versions(version_ids)
    failed = {job.submission_id for job in uow.jobs.failed_for([s.id for s in submissions])}
    return tuple(
        submission
        for submission in submissions
        if submission.id in failed and uow.runs.latest_for(submission.id) is None
    )


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
        # **ID の形は 2 つある。** 鍵を持つ版のある課題はコースを混ぜた ID に
        # 移したが（`a1c4e77b90d2`）、鍵を持たない課題は導き直せないので古い形の
        # まま残した。どちらでも当たりを確かめる。
        if str(task.id) in (
            derived_id("tsk", str(task.course_id), candidate),
            derived_id("tsk", candidate),
        ):
            return candidate
    raise HTTPException(
        status_code=409,
        detail=(
            "この課題は画面から直せません（取り込み時の課題キーが記録されて "
            "いない古い課題です）。API から同じキーで入れ直してください。"
        ),
    )


def _language_of(profile) -> str:
    """この科目の言語。`code_test_runner` の設定から取る。

    プロファイルが言語を持っているのに生成側で別に指定させると、
    「C の科目に Python の課題が生成される」が起きる。
    """
    options = profile.evaluator_options.get("code_test_runner", {})
    return str(options.get("language") or "c")


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
        failed = _failed_without_run(uow, task)
    on_old_version = sum(
        1 for _submission, run, _request in rows if run.context.task_version_id != version.id
    )
    return on_old_version + len(failed)


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
            # **配点を引き継ぐ**（`TaskVersion.points_declared`）。渡さないと既定の
            # 100 で版が作られ、教員が入れた配点が消える。
            **({"max_score": version.max_score} if version.points_declared else {}),
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


def _task_page(
    templates: Jinja2Templates,
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
    # この課題に効いている科目（上書き込み）。選択肢と警告の基準。
    effective = _effective_profile_of(_console(request), course, version)
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
            # **科目が宣言している評価器だけ出す**（`_declared_rows`）。
            "deterministic": _declared_rows(
                _evaluator_rows(registry, EvaluatorKind.DETERMINISTIC), effective
            ),
            # 受付のときに書き起こされるもの（#351）。**課題のプロファイル
            # で見る** ── 混在コースではコースの値と食い違う（#195・#264
            # で `_graded_by_tests` が同じ理由でこうなっている）。
            "transcription": _transcription_note(
                effective,
                (task.accepted_suffixes if task is not None else ())
                or course.upload_suffixes
                or DEFAULT_UPLOAD_SUFFIXES,
            ),
            # **AI 評価器も選べるようにする**（#315）。空（既定）は
            # `rubric_ai_judge` のことで、項目を積み上げる
            # `checklist_ai_judge` は指名しなければ走らない。
            "ai_evaluators": _declared_rows(_evaluator_rows(registry, EvaluatorKind.AI), effective),
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
            # いまの観点が指名しているのに科目が宣言していない評価器。
            # **選択肢から消すだけでは、保存した瞬間に黙って別のものに
            # 変わる**ので、選択中のまま出して警告する。
            "undeclared": dict(_undeclared_evaluators(effective, rows)),
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
            # 移動・名前の変更・コピー（2026-09-27）。**学生の提出があれば移せない**
            # ので、画面はそのときコピーを出す（判定は `move_task` がもう一度する）。
            "key_name": (key_name(version.source_key or "", task.unit) if task and version else ""),
            "learner_submissions": (
                learner_submission_count(_console(request).database, task.id) if task else 0
            ),
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
            # この課題の実行時間の上限（#491）。コードを走らせない課題には出さない。
            "case_timeout": _case_timeout_view(effective, task),
            # 提出の件数。**0 のときだけ削除を出す**（#51）。
            "submissions": _submission_count(_console(request), task),
            # 学習者に出る形（#105）。**保存済みの版を描いて出す。**
            # 書きかけの内容は「プレビューを更新」で同じ関数を通す ──
            # ブラウザ側で Markdown を描くと、普通の文章では一致し、
            # 間違いが起きるところ（数式・画像・生 HTML）でだけ食い違う。
            "statement_html": (render_statement(version.statement) if version is not None else ""),
            # 問題文に貼る画像（#64）。**課題の編集画面でも受け取る** ──
            # コースの設定画面まで往復させると、書きかけの問題文が失われる。
            "image_suffixes": sorted(images.SUFFIX_TYPES),
            "image_max_mb": images.MAX_BYTES // (1024 * 1024),
            # 貼るときの既定の表示幅。**画面で言う値と貼る値を 1 つにする。**
            "image_display_width": images.DISPLAY_WIDTH,
        },
    )


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
