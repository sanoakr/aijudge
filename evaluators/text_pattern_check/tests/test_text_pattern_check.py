"""本文との決定的な照合が守る約束を固定する。

固定したいのは 5 つ。

AI を呼ばない      決定的評価器である。モデルは書き起こしのときにしか出ない。
課題が決める       宣言が無ければ採点しない（当て推量の既定を持たない）。
本人と照合する     学籍番号は提出者のものと突き合わせる。渡らなければ満たさない。
範囲を絞れる       写り込んだ文字列で当たらないようにする。
0 を表せる         比例配分だけでは「別の講座」を 0 にできない。
"""

from __future__ import annotations

from datetime import UTC, datetime

from aijudge_eval_text_pattern_check import (
    EVALUATOR_ID,
    TextPatternCheck,
    normalise,
    reference_forms,
)

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    EvaluatorKind,
    EvaluatorStatus,
    Provenance,
    RubricCriterion,
    RubricLevel,
    Submission,
    SubmissionState,
    TaskVersion,
    TestCase,
)
from aijudge_core.ids import (
    ArtifactId,
    CriterionId,
    SubmissionId,
    TaskId,
    TaskVersionId,
    UserId,
)
from aijudge_grading import EvaluationRequest

NOW = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)
ARTIFACT = ArtifactId("art_" + "3" * 32)
SUBMISSION = SubmissionId("sub_" + "4" * 32)
VERSION = TaskVersionId("tsv_" + "2" * 32)

# 実際の書き起こし（47 件の実データから。ブラウザ枠つきの提出を模す）。
BODY = "\n".join(
    [
        "paiza - 環境構築不要！初心者でも楽しく入門できるプログラミング学習サイト",
        "転職・キャリア",
        "認定証",
        "Python体験編1: Pythonをはじめよう (全 17 回)",
        "Y240040naka",
        "paizaラーニング",
        "Python体験編1: Pythonをはじめよう (全 17 回)",
        "の全チャプターを修了したことを証明します。",
        "2026年9月18日",
    ]
)


def _criterion(code: str = "nickname", levels: int = 2) -> RubricCriterion:
    ladder = [
        RubricLevel(level=0, label="未達", descriptor="無い", score_ratio=0.0),
        RubricLevel(level=1, label="達成", descriptor="ある", score_ratio=1.0),
    ]
    if levels == 3:
        ladder.insert(1, RubricLevel(level=1, label="一部", descriptor="一部", score_ratio=0.5))
        ladder[2] = RubricLevel(level=2, label="達成", descriptor="揃う", score_ratio=1.0)
    return RubricCriterion(
        id=CriterionId("crt_" + ("1" if code == "nickname" else "5") * 32),
        code=code,
        title=code,
        description="d",
        weight=1.0,
        levels=tuple(ladder),
        evaluator_id=EVALUATOR_ID,
    )


def _case(name: str, weight: float = 1.0, **payload: object) -> TestCase:
    return TestCase(
        name=name, evaluator_id=EVALUATOR_ID, payload=payload, hidden=False, weight=weight
    )


def _request(
    *,
    cases: tuple[TestCase, ...],
    criteria: tuple[RubricCriterion, ...] | None = None,
    body: str = BODY,
    learner_reference: str | None = "y240040@mail.example.ac.jp",
) -> EvaluationRequest:
    criteria = criteria or (_criterion(),)
    payload = body.encode("utf-8")
    artifact = Artifact(
        id=ARTIFACT,
        submission_id=SUBMISSION,
        role=ArtifactRole.ORIGINAL,
        kind=ArtifactKind.IMAGE,
        storage_key="k",
        content_hash="sha256:x",
        byte_size=len(payload),
        filename="認定証.png",
        created_at=NOW,
    )
    version = TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "5" * 32),
        version=1,
        subject_profile="cs_network_python",
        statement="認定証を提出してください。",
        criteria=criteria,
        test_cases=cases,
        max_score=100.0,
        provenance=Provenance(authored_by=UserId("usr_" + "6" * 32)),
        created_at=NOW,
    )
    submission = Submission(
        id=SUBMISSION,
        task_version_id=VERSION,
        learner_id=UserId("usr_" + "7" * 32),
        state=SubmissionState.SUBMITTED,
        artifacts=(artifact,),
        submitted_at=NOW,
        created_at=NOW,
    )
    return EvaluationRequest(
        task_version=version,
        submission=submission,
        # **抽出器が起こした本文が渡る**（原本のバイト列ではない）。
        artifact_contents={ARTIFACT: payload},
        test_cases=cases,
        learner_reference=learner_reference,
    )


NICKNAME = _case(
    "受講者欄",
    criterion="nickname",
    expect="learner_reference",
    line_matches=r"^[A-Za-z]\d{6}",
)


# --------------------------------------------------------------------------
# 決定的であること
# --------------------------------------------------------------------------


def test_it_is_a_deterministic_evaluator() -> None:
    """**AI を呼ばない。** 速いレーンで走り、判定は確定する（P3）。"""
    assert TextPatternCheck().kind is EvaluatorKind.DETERMINISTIC


def test_the_verdict_is_conclusive() -> None:
    outcome = TextPatternCheck().evaluate(_request(cases=(NICKNAME,)))
    assert outcome.scores[0].conclusive is True
    assert outcome.scores[0].confidence == 1.0


# --------------------------------------------------------------------------
# 本人との照合
# --------------------------------------------------------------------------


def test_the_learner_id_is_matched_against_the_submitter() -> None:
    outcome = TextPatternCheck().evaluate(_request(cases=(NICKNAME,)))
    assert outcome.status is EvaluatorStatus.OK
    assert outcome.scores[0].level == 1


def test_someone_elses_id_does_not_pass() -> None:
    """**他人の認定証では通らない。** ここが崩れると使い回しが通る。"""
    outcome = TextPatternCheck().evaluate(
        _request(cases=(NICKNAME,), learner_reference="y230009@mail.example.ac.jp")
    )
    assert outcome.scores[0].level == 0


def test_without_a_learner_reference_nothing_passes() -> None:
    """**分からないまま通さない。** 通すと誰の認定証でも満点になる。"""
    outcome = TextPatternCheck().evaluate(_request(cases=(NICKNAME,), learner_reference=None))
    assert outcome.scores[0].level == 0


def test_a_blank_nickname_does_not_pass() -> None:
    body = BODY.replace("Y240040naka", "")
    outcome = TextPatternCheck().evaluate(_request(cases=(NICKNAME,), body=body))
    assert outcome.scores[0].level == 0


def test_a_name_without_the_number_does_not_pass() -> None:
    body = BODY.replace("Y240040naka", "森田隆聖")
    outcome = TextPatternCheck().evaluate(_request(cases=(NICKNAME,), body=body))
    assert outcome.scores[0].level == 0


def test_a_full_width_space_does_not_break_the_match() -> None:
    assert normalise("Y240040　中村") == "y240040中村"


def test_both_the_login_and_its_local_part_count_as_the_learner() -> None:
    assert reference_forms("Y240040@mail.example.ac.jp") == (
        "y240040@mail.example.ac.jp",
        "y240040",
    )


# --------------------------------------------------------------------------
# 探す範囲を絞る — 写り込みで当たらないこと
# --------------------------------------------------------------------------


def test_a_number_printed_elsewhere_on_the_screen_does_not_count() -> None:
    """**ブラウザ枠ごと撮った提出**では、タブや URL も本文に入る（実データ 3 件）。

    本文全体を見ると、認定証に書いていない学籍番号でも当たる。
    """
    body = BODY.replace("Y240040naka", "").replace(
        "転職・キャリア", "paiza.jp/works/y240040/certificates"
    )
    outcome = TextPatternCheck().evaluate(_request(cases=(NICKNAME,), body=body))
    assert outcome.scores[0].level == 0, "受講者欄ではない行で当たっている"


def test_without_line_matches_the_whole_body_is_searched() -> None:
    """既定は本文全体。絞るのは宣言した項目だけ。"""
    loose = _case("どこかに", criterion="nickname", expect="learner_reference")
    outcome = TextPatternCheck().evaluate(_request(cases=(loose,)))
    assert outcome.scores[0].level == 1


# --------------------------------------------------------------------------
# 課題が宣言するもの
# --------------------------------------------------------------------------


def test_a_criterion_with_no_declaration_is_not_scored() -> None:
    """**当て推量の既定を持たない。** 何を照合するかは課題ごとに違う。"""
    outcome = TextPatternCheck().evaluate(_request(cases=()))
    assert outcome.status is EvaluatorStatus.SKIPPED
    assert outcome.scores == ()


def test_declarations_for_another_criterion_are_left_alone() -> None:
    other = _case("講座名", criterion="certificate", pattern="体験編")
    outcome = TextPatternCheck().evaluate(_request(cases=(NICKNAME, other)))
    assert set(outcome.raw_output["matched"]) == {"nickname"}


def test_a_submission_with_no_text_is_left_to_a_human() -> None:
    """**0 点にしない** ── 読めなかったのか白紙なのかはここでは分からない。"""
    outcome = TextPatternCheck().evaluate(_request(cases=(NICKNAME,), body="   "))
    assert outcome.status is EvaluatorStatus.SKIPPED
    assert outcome.scores == ()


# --------------------------------------------------------------------------
# 必須の項目 — 比例配分では 0 を表せない
# --------------------------------------------------------------------------


def _certificate_cases() -> tuple[TestCase, ...]:
    return (
        _case(
            "講座名",
            criterion="certificate",
            pattern=r"python\s*体験\s*編?\s*1",
            required=True,
        ),
        _case("修了文", criterion="certificate", pattern="修了|証明"),
    )


def test_a_certificate_for_another_course_scores_zero() -> None:
    """**別の講座の認定証が半分の点を取らない。**

    修了文はどの認定証にも書いてあるので、比例配分だけだと 2 項目のうち
    1 つを満たして段階 1 になる。
    """
    body = "認定証\nJava入門編 1\n全チャプターを修了したことを証明します。"
    outcome = TextPatternCheck().evaluate(
        _request(
            cases=_certificate_cases(),
            criteria=(_criterion("certificate", levels=3),),
            body=body,
        )
    )
    assert outcome.scores[0].level == 0
    assert "必須の項目" in outcome.scores[0].rationale


def test_the_right_course_without_the_completion_line_is_partial() -> None:
    body = "認定証\nPython体験編1: Pythonをはじめよう (全 17 回)"
    outcome = TextPatternCheck().evaluate(
        _request(
            cases=_certificate_cases(),
            criteria=(_criterion("certificate", levels=3),),
            body=body,
        )
    )
    assert outcome.scores[0].level == 1


def test_everything_satisfied_reaches_the_top_level() -> None:
    outcome = TextPatternCheck().evaluate(
        _request(
            cases=_certificate_cases(),
            criteria=(_criterion("certificate", levels=3),),
        )
    )
    assert outcome.scores[0].level == 2


# --------------------------------------------------------------------------
# 画面との関係
# --------------------------------------------------------------------------


def test_the_declaration_is_not_offered_to_the_item_set_editor() -> None:
    """**`items` を名乗らない。**

    項目表の編集欄は `description` と `aliases` しか持たず、保存のたびに
    payload を作り直す。そこで保存されると `pattern` も `expect` も消え、
    観点は「宣言が無い」として採点されなくなる ── 例外は出ない。
    """
    from aijudge_grading.protocol import test_case_shape

    assert test_case_shape(TextPatternCheck()) == "patterns"
