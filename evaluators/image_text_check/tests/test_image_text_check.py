"""画像の書き起こし＋決定的照合が守る約束を固定する。

固定したいのは 5 つ。

項目は課題が決める  `TaskVersion.test_cases` のうち自分あてのものだけを読む。
                    宣言が無ければ採点しない（当て推量の既定を持たない）。
段階は数えて決める  モデルは書き起こすだけ。**点は付けさせない。**
本人と照合する      学籍番号は提出者のものと突き合わせる。渡らなければ満たさない。
読めなければ人へ    PDF も、読めない画像も SKIP。**0 点にしない。**
確定させない        AI の判定は提案である（P5）。割れたら確信度が下がる。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from aijudge_eval_image_text_check import (
    EVALUATOR_ID,
    ImageTextCheck,
    normalise,
    reference_forms,
)

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
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
from aijudge_llm_gateway import LlmGateway
from aijudge_llm_gateway.provider import ScriptedProvider

NOW = datetime(2026, 9, 18, 9, 0, tzinfo=UTC)
CRITERION = CriterionId("crt_" + "1" * 32)
VERSION = TaskVersionId("tsv_" + "2" * 32)
ARTIFACT = ArtifactId("art_" + "3" * 32)
SUBMISSION = SubmissionId("sub_" + "4" * 32)

# 実物の PNG である必要は無い。base64 にして送る経路だけを見る。
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fake"


def _criterion(code: str = "nickname", levels: int = 2) -> RubricCriterion:
    ladder = [
        RubricLevel(level=0, label="未達", descriptor="書かれていない", score_ratio=0.0),
        RubricLevel(level=1, label="達成", descriptor="書かれている", score_ratio=1.0),
    ]
    if levels == 3:
        ladder = [
            RubricLevel(level=0, label="未達", descriptor="無い", score_ratio=0.0),
            RubricLevel(level=1, label="一部", descriptor="一部", score_ratio=0.5),
            RubricLevel(level=2, label="達成", descriptor="揃う", score_ratio=1.0),
        ]
    return RubricCriterion(
        id=CRITERION,
        code=code,
        title="学籍番号入りのニックネーム",
        description="認定証のニックネームに本人の学籍番号が含まれているか。",
        weight=1.0,
        levels=tuple(ladder),
        evaluator_id=EVALUATOR_ID,
    )


def _case(name: str, weight: float = 1.0, **payload: object) -> TestCase:
    return TestCase(
        name=name,
        evaluator_id=EVALUATOR_ID,
        payload=payload,
        hidden=False,
        weight=weight,
    )


def _request(
    *,
    cases: tuple[TestCase, ...],
    criterion: RubricCriterion | None = None,
    learner_reference: str | None = "y240040@mail.example.ac.jp",
    kind: ArtifactKind = ArtifactKind.IMAGE,
    contents: bytes | None = IMAGE_BYTES,
    **options: object,
) -> EvaluationRequest:
    criterion = criterion or _criterion()
    artifact = Artifact(
        id=ARTIFACT,
        submission_id=SUBMISSION,
        role=ArtifactRole.ORIGINAL,
        kind=kind,
        storage_key="k",
        content_hash="sha256:x",
        byte_size=len(IMAGE_BYTES),
        filename="認定証.png",
        created_at=NOW,
    )
    version = TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "5" * 32),
        version=1,
        subject_profile="cs_network_python",
        statement="## 認定証提出 ##\n\n認定証の画面キャプチャを提出してください。",
        criteria=(criterion,),
        test_cases=cases,
        max_score=100.0,
        provenance=Provenance(authored_by=UserId("usr_" + "6" * 32)),
        created_at=NOW,
    )
    submission = Submission(
        id=SUBMISSION,
        task_version_id=VERSION,
        learner_id=UserId("usr_" + "7" * 32),
        attempt=1,
        state=SubmissionState.SUBMITTED,
        artifacts=(artifact,),
        submitted_at=NOW,
        created_at=NOW,
    )
    return EvaluationRequest(
        task_version=version,
        submission=submission,
        artifact_contents={} if contents is None else {ARTIFACT: contents},
        criterion=criterion,
        learner_reference=learner_reference,
        options=options,
    )


def _judge(
    *replies: dict[str, object], samples: int = 1
) -> tuple[ImageTextCheck, ScriptedProvider]:
    provider = ScriptedProvider(
        [json.dumps(reply, ensure_ascii=False) for reply in replies],
        vision=True,
    )
    return (
        ImageTextCheck(LlmGateway(provider), model="vl", samples=samples),
        provider,
    )


def _read(**fields: str) -> dict[str, object]:
    return {
        "readable": True,
        "fields": [{"name": name, "text": text} for name, text in fields.items()],
    }


NICKNAME_CASE = _case(
    "受講者欄",
    where="認定証の中央、横線の上の 1 行をそのまま写す",
    criterion="nickname",
    expect="learner_reference",
)


# --------------------------------------------------------------------------
# 本人との照合 — ここに LLM は関与しない
# --------------------------------------------------------------------------


def test_the_learner_id_is_matched_against_the_submitter() -> None:
    """`Y240040naka` は y240040 のものとして通る。"""
    judge, _ = _judge(_read(受講者欄="Y240040naka"))
    outcome = judge.evaluate(_request(cases=(NICKNAME_CASE,)))
    assert outcome.status is EvaluatorStatus.OK
    assert outcome.scores[0].level == 1


def test_a_certificate_without_the_learner_id_does_not_pass() -> None:
    """氏名だけの認定証は、本人のものと確認できない。"""
    judge, _ = _judge(_read(受講者欄="森田隆聖"))
    outcome = judge.evaluate(_request(cases=(NICKNAME_CASE,)))
    assert outcome.scores[0].level == 0


def test_someone_elses_id_does_not_pass() -> None:
    """**他人の学籍番号では通らない。** ここが崩れると使い回しが通る。"""
    judge, _ = _judge(_read(受講者欄="y230009"))
    outcome = judge.evaluate(_request(cases=(NICKNAME_CASE,)))
    assert outcome.scores[0].level == 0


def test_without_a_learner_reference_nothing_passes() -> None:
    """**分からないまま通さない。** 通すと誰の認定証でも満点になる。"""
    judge, _ = _judge(_read(受講者欄="Y240040naka"))
    outcome = judge.evaluate(_request(cases=(NICKNAME_CASE,), learner_reference=None))
    assert outcome.scores[0].level == 0


def test_a_full_width_space_does_not_break_the_match() -> None:
    """認定証の実物は `Y230020　岡本秀和` のように全角で区切る。"""
    assert normalise("Y240040　中村") == "y240040中村"


def test_both_the_login_and_its_local_part_count_as_the_learner() -> None:
    """ログイン ID はメールのことがある（Google ログイン）。"""
    assert reference_forms("Y240040@mail.example.ac.jp") == (
        "y240040@mail.example.ac.jp",
        "y240040",
    )


# --------------------------------------------------------------------------
# 課題が宣言するもの
# --------------------------------------------------------------------------


def test_a_criterion_with_no_declared_fields_is_not_graded() -> None:
    """**当て推量の既定を持たない。** どこを見るかは課題ごとに違う。"""
    judge, provider = _judge(_read())
    outcome = judge.evaluate(_request(cases=()))
    assert outcome.status is EvaluatorStatus.SKIPPED
    assert provider.calls == [], "宣言が無いのにモデルを呼んでいる"


def test_fields_declared_for_another_criterion_are_left_alone() -> None:
    """`criterion` 違いの項目は混ざらない（観点ごとに 1 回・§04 step 3）。"""
    other = _case("講座名", where="上段の講座名", criterion="certificate", pattern="体験編1")
    judge, _ = _judge(_read(受講者欄="y240040"))
    outcome = judge.evaluate(_request(cases=(NICKNAME_CASE, other)))
    assert set(outcome.raw_output["fields"]) == {"受講者欄"}


def test_the_declared_wording_reaches_the_prompt() -> None:
    """**どこを見るかは課題が書く。** 文面が届かなければ宣言の意味が無い。"""
    judge, provider = _judge(_read(受講者欄="y240040"))
    judge.evaluate(_request(cases=(NICKNAME_CASE,)))
    sent = provider.calls[0].messages[-1].content
    assert "認定証の中央、横線の上の 1 行をそのまま写す" in sent


def test_the_image_is_attached_to_the_call() -> None:
    judge, provider = _judge(_read(受講者欄="y240040"))
    judge.evaluate(_request(cases=(NICKNAME_CASE,)))
    assert provider.calls[0].messages[-1].images, "画像が送られていない"


def test_a_criterion_naming_another_evaluator_is_skipped_before_the_call() -> None:
    """指名されていない観点に答えない（AI 評価器が 2 つある科目で二重に付く）。"""
    criterion = _criterion().model_copy(update={"evaluator_id": "rubric_ai_judge"})
    judge, provider = _judge(_read())
    outcome = judge.evaluate(_request(cases=(NICKNAME_CASE,), criterion=criterion))
    assert outcome.status is EvaluatorStatus.SKIPPED
    assert provider.calls == []


# --------------------------------------------------------------------------
# 同じ事実が 2 か所に書いてあるとき
# --------------------------------------------------------------------------


def test_a_pattern_can_be_found_in_another_field(monkeypatch) -> None:
    """**OCR が 1 文字落としても、もう片方で決まる。**

    実測で 1 件あった ── 題の「Python体験**編**1」から「編」が落ち、
    修了文の側に同じ講座名が残っていた。
    """
    course = _case(
        "講座名",
        where="上段の講座名",
        criterion="certificate",
        pattern=r"python\s*体験編\s*1",
        anywhere=True,
    )
    completion = _case("修了文", where="修了を証明する文", criterion="certificate")
    judge, _ = _judge(
        _read(
            講座名="Python体験1: Pythonをはじめよう (全 17 回)",
            修了文=(
                "Python体験編1: Pythonをはじめよう (全 17 回) "
                "の全チャプターを修了したことを証明します。"
            ),
        )
    )
    outcome = judge.evaluate(
        _request(cases=(course, completion), criterion=_criterion("certificate", levels=3))
    )
    assert outcome.scores[0].level == 2


def test_without_anywhere_the_pattern_only_sees_its_own_field() -> None:
    """既定では自分の欄しか見ない。広げるのは宣言した項目だけ。"""
    course = _case(
        "講座名",
        where="上段の講座名",
        criterion="certificate",
        pattern=r"python\s*体験編\s*1",
    )
    completion = _case("修了文", where="修了を証明する文", criterion="certificate")
    judge, _ = _judge(
        _read(講座名="Python体験1: ...", 修了文="Python体験編1: ... を修了したことを証明します。")
    )
    outcome = judge.evaluate(
        _request(cases=(course, completion), criterion=_criterion("certificate", levels=3))
    )
    # 2 項目のうち満たしたのは修了文だけ → 3 段の真ん中。
    assert outcome.scores[0].level == 1


# --------------------------------------------------------------------------
# 読めないとき — 人へ回す。0 点にしない
# --------------------------------------------------------------------------


def test_a_pdf_submission_is_left_to_a_human() -> None:
    """描画手段を持たない。**形式の選択を減点にしない。**"""
    judge, provider = _judge(_read())
    outcome = judge.evaluate(_request(cases=(NICKNAME_CASE,), kind=ArtifactKind.PDF))
    assert outcome.status is EvaluatorStatus.SKIPPED
    assert outcome.scores == ()
    assert provider.calls == []


def test_an_unreadable_image_is_left_to_a_human() -> None:
    judge, _ = _judge({"readable": False, "fields": []})
    outcome = judge.evaluate(_request(cases=(NICKNAME_CASE,)))
    assert outcome.status is EvaluatorStatus.SKIPPED
    assert outcome.scores == ()


def test_a_dead_model_does_not_fail_the_whole_run() -> None:
    """LLM が使えなくても採点全体は落とさない（P2）。"""
    provider = ScriptedProvider([], vision=True)
    judge = ImageTextCheck(LlmGateway(provider), model="vl", samples=1)
    outcome = judge.evaluate(_request(cases=(NICKNAME_CASE,)))
    assert outcome.status is EvaluatorStatus.FAILED
    assert outcome.scores == ()


# --------------------------------------------------------------------------
# 提案であって確定ではない（P5）
# --------------------------------------------------------------------------


def test_the_verdict_is_never_conclusive() -> None:
    judge, _ = _judge(_read(受講者欄="y240040"))
    outcome = judge.evaluate(_request(cases=(NICKNAME_CASE,)))
    assert outcome.scores[0].conclusive is False


def test_a_split_transcription_lowers_the_confidence() -> None:
    """書き起こしが割れたら人が見る。段階ではなく**文字列**で一致を見る。"""
    judge, _ = _judge(
        _read(受講者欄="y240040"),
        _read(受講者欄=""),
        _read(受講者欄="y240040"),
        samples=3,
    )
    outcome = judge.evaluate(_request(cases=(NICKNAME_CASE,)))
    assert outcome.scores[0].confidence < 1.0


def test_the_transcription_is_kept_as_evidence() -> None:
    """判定の根拠は、画像に書いてあった文字そのもの（P4）。"""
    judge, _ = _judge(_read(受講者欄="Y240040naka"))
    outcome = judge.evaluate(_request(cases=(NICKNAME_CASE,)))
    evidence = outcome.scores[0].evidence
    assert evidence and evidence[0].quote == "Y240040naka"
    assert "Y240040naka" in outcome.scores[0].rationale


# --------------------------------------------------------------------------
# 必須の項目 — 比例配分では「0 にすべき提出」を表せない
# --------------------------------------------------------------------------


def _certificate_cases() -> tuple[TestCase, ...]:
    """認定証の観点。段階 0 は「別の講座」と定義されている。"""
    return (
        _case(
            "講座名",
            where="上段の講座名",
            criterion="certificate",
            pattern=r"python\s*入門編",
            anywhere=True,
            required=True,
        ),
        _case("修了文", where="修了を証明する文", criterion="certificate", pattern="修了|証明"),
    )


def test_a_certificate_for_another_course_scores_zero() -> None:
    """**別の講座の認定証が半分の点を取らない。**

    修了文はどの認定証にも書いてあるので、比例配分だけだと 2 項目のうち
    1 つを満たして段階 1 になる ── 課題と無関係な提出が半分の点を取る。
    """
    judge, _ = _judge(
        _read(
            講座名="Java入門編 1: Javaをはじめよう",
            修了文="全チャプターを修了したことを証明します。",
        )
    )
    outcome = judge.evaluate(
        _request(cases=_certificate_cases(), criterion=_criterion("certificate", levels=3))
    )
    assert outcome.scores[0].level == 0
    assert outcome.raw_output["unmet_required"] == ["講座名"]
    assert "必須の項目" in outcome.scores[0].rationale


def test_the_right_course_without_the_completion_line_is_partial() -> None:
    """必須を満たしていれば、残りは今までどおり重みの比で決まる。"""
    judge, _ = _judge(_read(講座名="新・Python入門編 8: ...", 修了文=""))
    outcome = judge.evaluate(
        _request(cases=_certificate_cases(), criterion=_criterion("certificate", levels=3))
    )
    assert outcome.scores[0].level == 1


def test_everything_satisfied_reaches_the_top_level() -> None:
    judge, _ = _judge(_read(講座名="新・Python入門編 8: ...", 修了文="修了したことを証明します。"))
    outcome = judge.evaluate(
        _request(cases=_certificate_cases(), criterion=_criterion("certificate", levels=3))
    )
    assert outcome.scores[0].level == 2


# --------------------------------------------------------------------------
# 画面との関係
# --------------------------------------------------------------------------


def test_the_declaration_is_not_offered_to_the_item_set_editor() -> None:
    """**`items` を名乗らない。**

    項目表の編集欄は `description` と `aliases` しか持たず、保存のたびに
    payload を作り直す。そこで保存されると `where` も `pattern` も `expect`
    も消え、観点は「宣言が無い」として採点されなくなる ── 例外は出ない。
    """
    from aijudge_grading.protocol import test_case_shape

    judge = ImageTextCheck(LlmGateway(ScriptedProvider([], vision=True)), model="vl")
    assert test_case_shape(judge) == "fields"
    assert test_case_shape(judge) != "items"
