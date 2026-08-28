"""レポートの体裁判定の規則を固定する。

固定したいのは 2 つ。

見出しを見つける **実データの見出しはこれだけ揺れる。**
                 `目的` / `1. 目的` / `１．[目的]` / `2.条件:` / `5．考察` /
                 `4. 測定結果`。行の完全一致で探すと、19 件の実提出のうち
                 7 件しか通らなかった。通らないのは体裁が悪いのではなく
                 探し方が硬いだけである。
本文に反応しない 「本実験の目的は…」のような本文行を見出しと見なさない。
                 見なすと、節が無いレポートが満点になる。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from aijudge_eval_report_structure import DEFAULT_SECTIONS, ReportStructure, found_sections

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

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
CRITERION = CriterionId("crt_" + "1" * 32)
VERSION = TaskVersionId("tsv_" + "2" * 32)
ARTIFACT = ArtifactId("art_" + "3" * 32)
SUBMISSION = SubmissionId("sub_" + "4" * 32)

BODY = "本実験ではHTTPサーバの応答性能を測定した。" * 40
NUMBERS = " ".join(str(n) for n in range(30))


@pytest.mark.parametrize(
    "heading",
    [
        "目的",
        "1. 目的",
        "1.  目的  ",
        "１．[目的]",
        "2.条件:",
        "5．考察",
        "第1章 目的",
        "【目的】",
    ],
)
def test_the_heading_forms_that_appear_in_real_reports_are_found(heading: str) -> None:
    """実データに現れた形。**どれも見出しとして通す。**"""
    keyword = "条件" if "条件" in heading else ("考察" if "考察" in heading else "目的")
    found = found_sections(heading + "\n本文が続く。", {keyword: DEFAULT_SECTIONS[keyword]})
    assert found[keyword], heading


def test_a_body_line_is_not_a_heading() -> None:
    """本文に反応すると、節が無いレポートが満点になる。"""
    text = "本実験の目的は、HTTPサーバの性能を評価することである。" * 3
    assert not found_sections(text, {"目的": DEFAULT_SECTIONS["目的"]})["目的"]


def test_the_synonyms_the_course_actually_used_are_found() -> None:
    text = "1. 目的\n2. 実験環境\n3. 実験方法\n4. 測定結果\n5. 考察\n"
    found = found_sections(text, DEFAULT_SECTIONS)
    assert all(found.values()), found


# --------------------------------------------------------------------------
# 評価
# --------------------------------------------------------------------------


def _criterion() -> RubricCriterion:
    return RubricCriterion(
        id=CRITERION,
        code="structure",
        title="構成",
        description="必須の節が揃い、分量と測定値の記載があるか。",
        weight=1.0,
        levels=(
            RubricLevel(level=0, label="未達", descriptor="節が揃わない", score_ratio=0.0),
            RubricLevel(level=1, label="一部", descriptor="一部欠ける", score_ratio=0.34),
            RubricLevel(level=2, label="概ね", descriptor="おおむね揃う", score_ratio=0.67),
            RubricLevel(level=3, label="達成", descriptor="すべて揃う", score_ratio=1.0),
        ),
        evaluator_id="report_structure",
    )


def _request(text: bytes | str, **options) -> EvaluationRequest:
    payload = text if isinstance(text, bytes) else text.encode("utf-8")
    artifact = Artifact(
        id=ARTIFACT,
        submission_id=SUBMISSION,
        role=ArtifactRole.ORIGINAL,
        kind=ArtifactKind.PDF,
        storage_key="k",
        content_hash="sha256:x",
        byte_size=len(payload),
        filename="report.pdf",
        created_at=NOW,
    )
    task_version = TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "5" * 32),
        version=1,
        subject_profile="report_ja",
        statement="## レポート課題 ##\n\n性能を評価しなさい。",
        criteria=(_criterion(),),
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
        task_version=task_version,
        submission=submission,
        artifact_contents={ARTIFACT: payload},
        options=options,
    )


def test_a_complete_report_reaches_the_top_level() -> None:
    text = f"1. 目的\n{BODY}\n2. 条件\n{NUMBERS}\n3. 方法\n4. 結果\n5. 考察\n{BODY}"
    outcome = ReportStructure().evaluate(_request(text))

    assert outcome.status is EvaluatorStatus.OK
    assert outcome.scores[0].level == 3
    # **体裁は確定させる。** 読めば分かることを AI に覆させない（P3）。
    assert outcome.scores[0].conclusive
    assert outcome.raw_output["missing"] == []


def test_a_missing_section_is_named_in_the_rationale() -> None:
    """何が足りないかを書く。書かないと学習者は直せない。"""
    text = f"1. 目的\n{BODY}\n2. 条件\n{NUMBERS}\n3. 結果\n"
    outcome = ReportStructure().evaluate(_request(text))

    assert outcome.scores[0].level < 3
    assert "方法" in outcome.scores[0].rationale
    assert "考察" in outcome.scores[0].rationale


def test_a_short_report_is_marked_down_but_not_zero() -> None:
    text = "1. 目的\n2. 条件\n3. 方法\n4. 結果\n5. 考察\n短い。" + NUMBERS
    outcome = ReportStructure().evaluate(_request(text))

    assert 0 < outcome.scores[0].level < 3, "節は揃っているので 0 ではない"
    assert "不足" in outcome.scores[0].rationale


def test_an_unreadable_submission_fails_rather_than_scoring_zero() -> None:
    """**0 点にしない。** 読めないのは学習者の責任とは限らない。

    失敗として記録すれば、その観点は未採点のまま人間に回る（P5）。
    """
    outcome = ReportStructure().evaluate(_request(b"\x89PNG\r\n\x1a\n binary"))

    assert outcome.status is EvaluatorStatus.FAILED
    assert outcome.scores == ()
    assert outcome.raw_output["readable"] is False


def test_the_task_can_declare_its_own_sections() -> None:
    """課題ごとに節を差し替えられる（科目プロファイルの evaluator_options）。"""
    text = f"1. 序論\n{BODY}\n2. 本論\n{NUMBERS}\n3. 結論\n"
    outcome = ReportStructure().evaluate(
        _request(text, sections=["序論", "本論", "結論"], min_numbers=5)
    )

    assert outcome.scores[0].level == 3
    assert outcome.raw_output["missing"] == []


def test_a_declared_format_becomes_a_fourth_check() -> None:
    """課題が提出形式を指定すると、体裁の条件が 1 本増える。

    実データの教員採点では、DOCX で出された 2 件がどちらも体裁で引かれて
    いた。課題文が「提出は PDF」と書いている以上、守ったかどうかは読めば
    分かることで、AI に聞く話ではない（P3）。
    """
    text = f"1. 目的\n{BODY}\n2. 条件\n{NUMBERS}\n3. 方法\n4. 結果\n5. 考察\n{BODY}"
    outcome = ReportStructure().evaluate(_request(text, required_kinds=["docx"]))

    assert outcome.status is EvaluatorStatus.OK
    assert outcome.raw_output["kind"] == "pdf"
    assert outcome.raw_output["required_kinds"] == ["docx"]
    # 節・分量・数値は満たすが形式だけ外れる → 4 本中 3 本。
    assert outcome.scores[0].level < 3
    assert "指定は docx" in outcome.scores[0].rationale


def test_no_declared_format_leaves_the_three_checks() -> None:
    """宣言が無ければ形式を問わない。既存の課題の段階が黙って下がらない。"""
    text = f"1. 目的\n{BODY}\n2. 条件\n{NUMBERS}\n3. 方法\n4. 結果\n5. 考察\n{BODY}"
    outcome = ReportStructure().evaluate(_request(text))

    assert outcome.raw_output["required_kinds"] == []
    assert outcome.scores[0].level == 3
