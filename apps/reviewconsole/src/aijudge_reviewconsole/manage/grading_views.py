"""評価器・観点の表示（段階 4-7）。

コース設定と課題の画面が**同じ規則で見せる**ために 1 か所に置く ── 片方だけ直る形を避ける
（地図 `docs/design/manage-split-map.md` の `grading_views.py`）。
"""

from __future__ import annotations

from fastapi import HTTPException

from aijudge_core import HUMAN_SCORED, Aggregation
from aijudge_course_admin import rubric
from aijudge_course_admin.errors import AdminError
from aijudge_grading import EvaluatorRegistry, OverrideError, effective_profile, load_profile


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


def _course_rubric_rows(course):
    """このコースの既定の観点（共通ルーブリック、無ければ組み込み）。"""
    criteria = rubric.from_stored(course.rubric) if course.rubric else _default_rubric_criteria()
    return rubric.to_rows(criteria)


def _declared_rows(rows: list[dict[str, str]], profile) -> list[dict[str, str]]:
    """観点に指名できる評価器を、**科目が宣言しているもの**に絞る。

    インストール済みの全部を出していたので、科目プロファイルに無い評価器を
    観点に付けられた。付けても採点パイプラインはその評価器を呼ばず（呼ぶのは
    プロファイルの `deterministic` / `ai_evaluators` だけ・ADR 0002）、観点は
    誰も担当しないまま「点が 1 つも出ない」で提出が落ちる（prog2 ex01-2、
    2026-09-22）。プロファイルが読めなければ絞らない（画面を止めない）。
    """
    if profile is None:
        return rows
    declared = set(profile.deterministic) | set(profile.ai_evaluators)
    return [row for row in rows if row["name"] in declared]


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


def _refuse_undeclared(profile, criteria, *, course_id: str) -> None:
    """科目が宣言していない評価器を観点に付けようとしたら**保存しない**。

    選択肢を絞る（`_declared_rows`）だけでは境界にならない（#146 と同じ
    理由）── 画面を経ない POST でも来る。呼ぶのは**観点を編集した保存**
    （課題の訂正で観点欄を送ったとき・共通ルーブリック）だけで、観点を引き
    継ぐだけの経路は止めない（`revise_task` の注記）。
    """
    missing = _undeclared_evaluators(profile, criteria)
    if not missing:
        return
    named = "、".join(f"観点「{code}」の {name}" for code, name in missing)
    raise HTTPException(
        status_code=400,
        detail=(
            f"{named} は科目プロファイル {profile.name} が宣言していない評価器です。"
            "このまま保存すると、その観点は誰も採点せず提出が失敗します。"
            f"決定的評価器はコースの採点設定（/manage/courses/{course_id}/grading）の "
            "deterministic で足せます。画像や PDF を本文に起こす抽出器は"
            "プロファイルの input.transcription に書きます。"
        ),
    )


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
                "judging_notes": at("criterion_judging_notes"),
            }
        )
    return rows


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


def _undeclared_evaluators(profile, criteria) -> tuple[tuple[str, str], ...]:
    """観点が指名した評価器のうち、科目が宣言していないもの（観点コード, 評価器）。

    空（既定の AI）と `HUMAN_SCORED` は評価器の指名ではないので除く。
    `CriterionSpec` と画面の行（`rubric.to_rows` の dict）の両方を受ける。
    """
    if profile is None:
        return ()
    declared = set(profile.deterministic) | set(profile.ai_evaluators)
    found = []
    for criterion in criteria:
        if isinstance(criterion, dict):
            code, name = str(criterion.get("code", "")), str(criterion.get("evaluator") or "")
        else:
            code, name = criterion.code, getattr(criterion, "evaluator", None) or ""
        if name and name != HUMAN_SCORED and name not in declared:
            found.append((code, name))
    return tuple(found)
