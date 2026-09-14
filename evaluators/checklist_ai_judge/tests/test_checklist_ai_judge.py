"""項目表の AI 照合が守る約束を固定する（#302）。

固定したいのは 4 つ。

項目は課題が決める `TaskVersion.test_cases` のうち自分あてのものが項目表。
                   無ければ科目プロファイル、それも無ければ組み込みの既定。
段階は数えて決める モデルは「あるか」だけを答える。**点は付けさせない。**
根拠を持つ         あると答えた項目は行を引かせ、実在しない行は捨てる（P4）。
確定させない       AI の判定は提案であって確定ではない（P5）。割れたら人へ。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from aijudge_eval_checklist_ai_judge import DEFAULT_ITEMS, ChecklistAiJudge, items_of

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
from aijudge_llm_gateway import LlmGateway
from aijudge_llm_gateway.provider import ScriptedProvider

NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
CRITERION = CriterionId("crt_" + "1" * 32)
VERSION = TaskVersionId("tsv_" + "2" * 32)
ARTIFACT = ArtifactId("art_" + "3" * 32)
SUBMISSION = SubmissionId("sub_" + "4" * 32)

REPORT = "\n".join(
    [
        "1. 研究の動機",  # 「目的」の言い換え。例の表には無い書き方。
        "HTTP サーバの応答性能を測る。",
        "2. 実験環境",
        "Linux 6.8 / 100BASE-TX。",
        "3. 実験の流れ",
        "同時接続数を 1 から 100 まで変えた。",
        "4. 測定結果",
        "平均応答時間は 12ms だった。",
        "5. 考察",
        "接続数に対して線形に増えた。",
    ]
)


def _criterion() -> RubricCriterion:
    return RubricCriterion(
        id=CRITERION,
        code="structure",
        title="構成",
        description="課題が求める項目が揃っているか。",
        weight=1.0,
        levels=(
            RubricLevel(level=0, label="未達", descriptor="揃わない", score_ratio=0.0),
            RubricLevel(level=1, label="一部", descriptor="一部欠ける", score_ratio=0.34),
            RubricLevel(level=2, label="概ね", descriptor="おおむね揃う", score_ratio=0.67),
            RubricLevel(level=3, label="達成", descriptor="すべて揃う", score_ratio=1.0),
        ),
        evaluator_id="checklist_ai_judge",
    )


def _request(*, text: str = REPORT, cases: tuple[TestCase, ...] = (), **options):
    payload = text.encode("utf-8")
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
    version = TaskVersion(
        id=VERSION,
        task_id=TaskId("tsk_" + "5" * 32),
        version=1,
        subject_profile="report_ja",
        statement="## レポート課題 ##\n\n性能を評価しなさい。",
        criteria=(_criterion(),),
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
        artifact_contents={ARTIFACT: payload},
        criterion=_criterion(),
        options=options,
    )


def _item_case(name: str, *aliases: str, weight: float = 1.0, description: str = "") -> TestCase:
    payload: dict[str, object] = {}
    if aliases:
        payload["aliases"] = list(aliases)
    if description:
        payload["description"] = description
    return TestCase(
        name=name,
        evaluator_id="checklist_ai_judge",
        payload=payload,
        hidden=False,
        weight=weight,
    )


def _judge(*responses: dict) -> ChecklistAiJudge:
    provider = ScriptedProvider([json.dumps(r, ensure_ascii=False) for r in responses])
    return ChecklistAiJudge(LlmGateway(provider), model="stub", samples=len(responses))


def _found(*names: str) -> dict:
    return {
        "findings": [
            {"item": name, "present": True, "line": 1 + 2 * i, "quote": name}
            for i, name in enumerate(names)
        ]
    }


# --------------------------------------------------------------------------
# 項目は課題が決める
# --------------------------------------------------------------------------


def test_the_task_decides_which_items_are_required() -> None:
    """**1 問ごとに違う項目を要求できる**（#302）。

    科目プロファイルは複数のコースが共有する雛形なので、そこに 1 問の
    答えを書くと他のコースの採点まで変わる。
    """
    cases = (_item_case("序論"), _item_case("本論"), _item_case("結論"))
    items, source = items_of(_request(cases=cases))

    assert [i.name for i in items] == ["序論", "本論", "結論"]
    assert source == "task"


def test_the_profile_is_used_when_the_task_says_nothing() -> None:
    items, source = items_of(_request(items=["目的", "結果"]))
    assert [i.name for i in items] == ["目的", "結果"]
    assert source == "profile"


def test_the_profile_key_of_the_evaluator_this_replaces_is_still_read() -> None:
    """`report_structure` が使っていた `sections` も読む。

    後継なので、既存のプロファイルがその名前で宣言している。
    """
    items, source = items_of(_request(sections=["目的", "考察"]))
    assert [i.name for i in items] == ["目的", "考察"]
    assert source == "profile"


def test_the_builtin_default_is_the_last_resort() -> None:
    items, source = items_of(_request())
    assert items == DEFAULT_ITEMS
    assert source == "default"


def test_the_hints_reach_the_prompt_as_examples() -> None:
    """言い換えと判定条件は**助言であって一致の条件ではない**。

    例に無い書き方（「研究の動機」）も、求めた内容が書かれていれば通る、
    という判断をさせたいので例として渡す。
    """
    provider = ScriptedProvider([json.dumps(_found("目的"), ensure_ascii=False)])
    judge = ChecklistAiJudge(LlmGateway(provider), model="stub", samples=1)
    judge.evaluate(
        _request(
            cases=(
                _item_case("目的", "背景と目的", description="何を確かめる実験かが書かれている"),
            )
        )
    )

    prompt = "\n".join(m.content for m in provider.calls[0].messages)
    assert "背景と目的" in prompt
    assert "何を確かめる実験かが書かれている" in prompt
    assert "言葉ではなく中身で判断する" in prompt


# --------------------------------------------------------------------------
# 段階は数えて決める
# --------------------------------------------------------------------------


def test_every_required_item_found_reaches_the_top_level() -> None:
    cases = (_item_case("目的"), _item_case("方法"), _item_case("結果"))
    outcome = _judge(_found("目的", "方法", "結果")).evaluate(_request(cases=cases))

    assert outcome.status is EvaluatorStatus.OK
    assert outcome.scores[0].level == 3
    assert outcome.raw_output["missing"] == []


def test_a_missing_item_lowers_the_level_and_is_named() -> None:
    """何が足りないかを書く。書かないと学習者は直せない。"""
    cases = (_item_case("目的"), _item_case("方法"), _item_case("考察"))
    outcome = _judge(
        {
            "findings": [
                {"item": "目的", "present": True, "line": 1, "quote": "1. 研究の動機"},
                {"item": "方法", "present": True, "line": 5, "quote": "3. 実験の流れ"},
                {"item": "考察", "present": False, "line": 0, "quote": ""},
            ]
        }
    ).evaluate(_request(cases=cases))

    assert 0 < outcome.scores[0].level < 3
    assert "考察" in outcome.scores[0].rationale
    assert outcome.raw_output["missing"] == ["考察"]


def test_items_carry_their_own_weight() -> None:
    """**素な項目を積み上げる**ので、重い項目と軽い項目を混ぜられる（#302）。

    等しく数えるなら重みは 1.0 のまま（既定）。
    """
    cases = (_item_case("考察", weight=3.0), _item_case("謝辞", weight=1.0))
    heavy_only = {
        "findings": [
            {"item": "考察", "present": True, "line": 9, "quote": "5. 考察"},
            {"item": "謝辞", "present": False, "line": 0, "quote": ""},
        ]
    }
    light_only = {
        "findings": [
            {"item": "考察", "present": False, "line": 0, "quote": ""},
            {"item": "謝辞", "present": True, "line": 1, "quote": "謝辞"},
        ]
    }
    with_heavy = _judge(heavy_only).evaluate(_request(cases=cases))
    with_light = _judge(light_only).evaluate(_request(cases=cases))

    assert with_heavy.scores[0].level > with_light.scores[0].level
    assert with_heavy.raw_output["satisfied_weight"] == 3.0
    assert with_heavy.raw_output["total_weight"] == 4.0


def test_an_item_the_model_invented_is_ignored() -> None:
    """**課題が求めた項目で数える。**

    モデルが項目を増やすと、増えた分だけ段階が動く ── 採点の目盛りが
    モデルの出力の長さで変わることになる。
    """
    cases = (_item_case("目的"), _item_case("方法"))
    outcome = _judge(
        {
            "findings": [
                {"item": "目的", "present": True, "line": 1, "quote": "1. 研究の動機"},
                {"item": "方法", "present": False, "line": 0, "quote": ""},
                {"item": "謝辞", "present": True, "line": 9, "quote": "謝辞"},
            ]
        }
    ).evaluate(_request(cases=cases))

    assert outcome.raw_output["items"] == {"目的": True, "方法": False}
    assert outcome.scores[0].level < 3


def test_an_item_the_model_forgot_counts_as_missing() -> None:
    """答えなかった項目を「あった」に倒さない。"""
    cases = (_item_case("目的"), _item_case("方法"))
    outcome = _judge(
        {"findings": [{"item": "目的", "present": True, "line": 1, "quote": "動機"}]}
    ).evaluate(_request(cases=cases))

    assert outcome.raw_output["missing"] == ["方法"]


# --------------------------------------------------------------------------
# 根拠と確からしさ
# --------------------------------------------------------------------------


def test_the_evidence_points_at_the_lines_the_model_cited() -> None:
    outcome = _judge(
        {"findings": [{"item": "目的", "present": True, "line": 1, "quote": "1. 研究の動機"}]}
    ).evaluate(_request(cases=(_item_case("目的"),)))

    evidence = outcome.scores[0].evidence
    assert evidence[0].span.start_line == 1
    assert evidence[0].quote == "1. 研究の動機"


def test_a_line_that_does_not_exist_is_dropped() -> None:
    """**捏造された根拠を画面に出さない**（P4）。

    落としきって 1 つも残らなければ、本文全体を指す根拠を置く ── AI の
    判定は根拠を持たなければ保存できない（`GradingRun` の検証）。
    """
    outcome = _judge(
        {"findings": [{"item": "目的", "present": True, "line": 9999, "quote": "捏造"}]}
    ).evaluate(_request(cases=(_item_case("目的"),)))

    evidence = outcome.scores[0].evidence
    assert len(evidence) == 1
    assert evidence[0].span.kind == "whole"


def test_a_criterion_that_does_not_name_this_evaluator_is_skipped() -> None:
    """**指名された観点だけを見る**（#302）。

    パイプラインは AI 評価器を「評価器を指名していない観点」にも回すので、
    科目が AI 評価器を 2 つ宣言していると、指名の無い観点が両方に判定されて
    点が二重に付く。項目の有無を問う評価器は、質を問う観点に答えない。
    """
    request = _request(cases=(_item_case("目的"),))
    generic = request.model_copy(
        update={"criterion": _criterion().model_copy(update={"evaluator_id": None})}
    )
    judge = _judge(_found("目的"))
    outcome = judge.evaluate(generic)

    assert outcome.status is EvaluatorStatus.SKIPPED
    assert outcome.scores == ()


def test_the_judgement_is_never_conclusive() -> None:
    """**AI の判定は提案である**（P5）。確定させると人が覆せない。"""
    outcome = _judge(_found("目的")).evaluate(_request(cases=(_item_case("目的"),)))

    score = outcome.scores[0]
    assert score.kind is EvaluatorKind.AI
    assert not score.conclusive


def test_disagreement_between_samples_lowers_the_confidence() -> None:
    """自己一貫性をそのまま確信度にする。割れたら人が見る（P5）。

    照合先は段階ではなく「どの項目があると答えたか」の組 ── この評価器は
    段階を答えないので、割れはそこにしか出ない。
    """
    cases = (_item_case("目的"), _item_case("方法"))
    both = _found("目的", "方法")
    one = {
        "findings": [
            {"item": "目的", "present": True, "line": 1, "quote": "動機"},
            {"item": "方法", "present": False, "line": 0, "quote": ""},
        ]
    }
    outcome = _judge(both, one, both).evaluate(_request(cases=cases))

    assert outcome.scores[0].confidence < 1.0


def test_an_unreadable_submission_is_skipped_rather_than_scored_zero() -> None:
    """**0 点にしない。** 読めないのは学習者の責任とは限らない。"""
    outcome = _judge(_found("目的")).evaluate(_request(text=""))

    assert outcome.status is EvaluatorStatus.SKIPPED
    assert outcome.scores == ()
