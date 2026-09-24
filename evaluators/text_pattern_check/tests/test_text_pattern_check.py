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
    return _request_multi(
        cases=cases, criteria=criteria, bodies=(body,), learner_reference=learner_reference
    )


def _request_multi(
    *,
    cases: tuple[TestCase, ...],
    criteria: tuple[RubricCriterion, ...] | None = None,
    bodies: tuple[str, ...],
    learner_reference: str | None = "y240040@mail.example.ac.jp",
) -> EvaluationRequest:
    """`bodies` の本数だけ画像を提出したことにする（ex01-3 相当）。"""
    criteria = criteria or (_criterion(),)
    artifacts = []
    contents = {}
    for index, body in enumerate(bodies):
        artifact_id = ArtifactId(f"art_{index}" + "3" * 30)
        payload = body.encode("utf-8")
        artifacts.append(
            Artifact(
                id=artifact_id,
                submission_id=SUBMISSION,
                role=ArtifactRole.ORIGINAL,
                kind=ArtifactKind.IMAGE,
                storage_key="k",
                content_hash=f"sha256:{index}",
                byte_size=len(payload),
                filename=f"認定証{index}.png",
                created_at=NOW,
            )
        )
        contents[artifact_id] = payload
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
        artifacts=tuple(artifacts),
        submitted_at=NOW,
        created_at=NOW,
    )
    return EvaluationRequest(
        task_version=version,
        submission=submission,
        # **抽出器が起こした本文が渡る**（原本のバイト列ではない）。
        artifact_contents=contents,
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


# --------------------------------------------------------------------------
# 複数の提出物 — ex01-3 のようにまとめて提出する課題
# --------------------------------------------------------------------------


def test_a_match_on_the_second_image_is_not_missed() -> None:
    """**先頭の1本で打ち切らない。** 2枚目にしか書いてない受講者欄も見る。"""
    outcome = TextPatternCheck().evaluate(
        _request_multi(cases=(NICKNAME,), bodies=("よそのタブの文字列だけ", BODY))
    )
    assert outcome.scores[0].level == 1


def test_submitted_images_reports_how_many_bodies_were_readable() -> None:
    """**「いくつ提出されたか」に、パターンの一致不一致と関係なく答える。**"""
    outcome = TextPatternCheck().evaluate(
        _request_multi(cases=(NICKNAME,), bodies=(BODY, "Java入門編の認定証"))
    )
    assert outcome.raw_output["submitted_images"] == 2


def test_counts_report_how_many_images_matched_each_item() -> None:
    outcome = TextPatternCheck().evaluate(
        _request_multi(cases=(NICKNAME,), bodies=(BODY, BODY, "無関係な画像"))
    )
    assert outcome.raw_output["counts"]["nickname"]["受講者欄"] == 2


def test_expected_count_scores_proportionally_to_how_many_matched() -> None:
    """**3枚出すべき認定証のうち2枚しか無い。** 比例で2/3の重みにする。"""
    case = _case(
        "認定証",
        criterion="nickname",
        pattern=r"認定証",
        expected_count=3,
    )
    outcome = TextPatternCheck().evaluate(
        _request_multi(
            cases=(case,),
            criteria=(_criterion(levels=3),),
            bodies=("認定証その1", "認定証その2", "証明書ではない画像"),
        )
    )
    assert outcome.raw_output["counts"]["nickname"]["認定証"] == 2
    assert outcome.scores[0].level == 1


def test_expected_count_reaching_the_full_amount_scores_top() -> None:
    case = _case("認定証", criterion="nickname", pattern=r"認定証", expected_count=2)
    outcome = TextPatternCheck().evaluate(
        _request_multi(cases=(case,), bodies=("認定証A", "認定証B"))
    )
    assert outcome.scores[0].level == 1


def test_expected_count_can_still_be_required() -> None:
    """**必須のまま数を数えられる。** 本数が足りなければ最低段階にする。"""
    case = _case("認定証", criterion="nickname", pattern=r"認定証", expected_count=3, required=True)
    outcome = TextPatternCheck().evaluate(
        _request_multi(cases=(case,), bodies=("認定証A", "認定証B"))
    )
    assert outcome.scores[0].level == 0
    assert "必須の項目" in outcome.scores[0].rationale


def test_evidence_is_attributed_to_the_image_that_actually_matched() -> None:
    """**根拠の取り違えを起こさない。** 1枚目には無い文字列を1枚目の根拠にしない。"""
    outcome = TextPatternCheck().evaluate(
        _request_multi(cases=(NICKNAME,), bodies=("よそのタブの文字列だけ", BODY))
    )
    evidence = outcome.scores[0].evidence
    assert len(evidence) == 1
    # bodies=(はずれ, BODY) の2本目（index=1）が一致した本体。
    assert evidence[0].artifact_content_hash == "sha256:1"


# --------------------------------------------------------------------------
# 同じものを二重に数えない（distinct_by）
# --------------------------------------------------------------------------


#: 実物の認定証（3 枚のサンプルから）。**レッスン名と定型文は別の行**にある。
def _certificate(lesson: str) -> str:
    return "\n".join(
        [
            "認定証",
            lesson,
            "Y240040naka",
            "paizaラーニング",
            lesson,
            "の全チャプターを修了したことを証明します。",
            "2026年9月23日",
        ]
    )


LESSON_KEY = r"(.{5,80}?)\s*の全チャプターを修了したことを証明"
COUNT_CERTIFICATES = _case(
    "認定証の枚数",
    criterion="nickname",
    pattern=r"修了したことを証明",
    expected_count=3,
    distinct_by=LESSON_KEY,
)


def test_the_same_certificate_twice_counts_once() -> None:
    """**水増しを数えない。** 同じレッスンの認定証は何枚出しても 1 件。"""
    outcome = TextPatternCheck().evaluate(
        _request_multi(
            cases=(COUNT_CERTIFICATES,),
            criteria=(_criterion(levels=3),),
            bodies=(_certificate("新・Linux入門編1: Linuxを学習しよう (全 3 回)"),) * 3,
        )
    )
    assert outcome.raw_output["counts"]["nickname"]["認定証の枚数"] == 1


def test_different_lessons_each_count() -> None:
    """**別のレッスンは別と数える。** 講座を選べる課題なので名前は予告できない。"""
    outcome = TextPatternCheck().evaluate(
        _request_multi(
            cases=(COUNT_CERTIFICATES,),
            criteria=(_criterion(levels=3),),
            bodies=(
                _certificate("新・Linux入門編1: Linuxを学習しよう (全 3 回)"),
                _certificate("新・Linux入門編2: ファイル・ディレクトリの操作と管理 (全 18 回)"),
                _certificate("新・Linux入門編3: プロセス (全 6 回)"),
            ),
        )
    )
    assert outcome.raw_output["counts"]["nickname"]["認定証の枚数"] == 3
    assert outcome.scores[0].level == 2


def test_a_lesson_name_that_wraps_is_still_one_key() -> None:
    """**折り返した講座名でも鍵が取れる。** 実物は長い名前が 2 行に割れる。

    行単位で見ていると、レッスン名の行と定型文の行が別なので捕まらない。
    `distinct_by` は本文全体に当てる。
    """
    wrapped = _certificate("新・Linux入門編1(LinuC対策版): Linuxを学習しよう (全 3\n回)")
    outcome = TextPatternCheck().evaluate(
        _request_multi(
            cases=(COUNT_CERTIFICATES,),
            criteria=(_criterion(levels=3),),
            bodies=(wrapped, wrapped),
        )
    )
    assert outcome.raw_output["counts"]["nickname"]["認定証の枚数"] == 1


def test_an_unreadable_certificate_is_not_treated_as_a_duplicate() -> None:
    """**読めなかったものは数える。** 書き起こしの失敗を減点にしない。

    鍵が取れない提出物まで重複扱いにすると、抽出器が読み損ねただけの提出が
    「同じ認定証を出した」と見なされて学習者が損をする。
    """
    unreadable = "認定証\n修了したことを証明\n（レッスン名が読み取れなかった）"
    outcome = TextPatternCheck().evaluate(
        _request_multi(
            cases=(COUNT_CERTIFICATES,),
            criteria=(_criterion(levels=3),),
            bodies=(unreadable, unreadable),
        )
    )
    assert outcome.raw_output["counts"]["nickname"]["認定証の枚数"] == 2


def test_without_distinct_by_nothing_is_deduplicated() -> None:
    """**既定は今までどおり。** 宣言しない課題の数え方は変わらない。"""
    case = _case("認定証", criterion="nickname", pattern=r"認定証", expected_count=3)
    outcome = TextPatternCheck().evaluate(
        _request_multi(
            cases=(case,),
            criteria=(_criterion(levels=3),),
            bodies=("認定証", "認定証"),
        )
    )
    assert outcome.raw_output["counts"]["nickname"]["認定証"] == 2
