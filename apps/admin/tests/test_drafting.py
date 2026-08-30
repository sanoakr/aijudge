"""生成 → 門 → 教員レビュー待ち、の一巡を固定する（S2、設計方針 §5）。

固定したいのは 4 つ。

同じ型に載る   生成物は手で作った課題と完全に同じ `TaskSpec` になる（P1/P2）。
門を通す       生成しただけでは採点に使わない。弱い下書きは門 2 が落とす。
出所が残る     `Provenance` に生成物であることと、どのモデル・どの版かが残る。
個人データを渡さない 作問のプロンプトは NON_PERSONAL（P7）。

モデルは台本つきプロバイダで、ネットワークには出ない。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from aijudge_admin import TaskVerifier
from aijudge_admin.drafting import TaskDrafter
from aijudge_authoring.drafting import Blueprint, Difficulty
from aijudge_authoring.spec import build_task_version
from aijudge_authoring.verification import GateOutcome
from aijudge_core import ReviewState
from aijudge_core.ids import UserId
from aijudge_grading import EvaluatorRegistry, load_profile
from aijudge_llm_gateway import LlmGateway, ScriptedProvider

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILE = load_profile(REPO_ROOT / "subjects" / "cs_intro_c.yaml")
AUTHOR = UserId("usr_" + "1" * 32)

needs_c_compiler = pytest.mark.skipif(
    shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="no C compiler available",
)

BLUEPRINT = Blueprint(
    knowledge_components=("cs.io.formatted_input", "cs.arithmetic.sum"),
    subject_profile="cs_intro_c",
    difficulty=Difficulty.INTRODUCTORY,
    constraints=("標準入力から読むこと",),
    test_case_count=3,
)

GOOD = json.dumps(
    {
        "title": "2 数の和",
        "statement": "2 つの整数を読み、その和を出力しなさい。",
        "reference_solution": (
            "#include <stdio.h>\n"
            "int main(void) {\n"
            "    int a, b;\n"
            '    scanf("%d %d", &a, &b);\n'
            '    printf("%d\\n", a + b);\n'
            "    return 0;\n"
            "}\n"
        ),
        "test_cases": [
            {"name": "c1", "input": "2 3\n", "expected": "5\n"},
            {"name": "c2", "input": "10 1\n", "expected": "11\n"},
            {"name": "c3", "input": "0 0\n", "expected": "0\n"},
        ],
    },
    ensure_ascii=False,
)

# 出力が入力に依存しない下書き。**門 2 が落とすべきもの。**
WEAK = json.dumps(
    {
        "title": "固定出力",
        "statement": "fixed と出力しなさい。",
        "reference_solution": (
            "#include <stdio.h>\n"
            "int main(void) {\n"
            "    int unused = 42;\n"
            "    int spare = 7;\n"
            "    int extra = 13;\n"
            "    int more = 99;\n"
            '    printf("fixed\\n");\n'
            "    return 0;\n"
            "}\n"
        ),
        "test_cases": [
            {"name": "c1", "input": "", "expected": "fixed\n"},
            {"name": "c2", "input": "", "expected": "fixed\n"},
        ],
    },
    ensure_ascii=False,
)


def _drafter(payload: str) -> tuple[TaskDrafter, ScriptedProvider]:
    provider = ScriptedProvider([payload])
    return TaskDrafter(LlmGateway(provider), model="stub"), provider


def _verifier(**kwargs) -> TaskVerifier:
    return TaskVerifier(EvaluatorRegistry().load_installed(), PROFILE, **kwargs)


def test_a_draft_becomes_an_ordinary_task_spec() -> None:
    """**生成専用の経路を作らない**（P1/P2）。採点側は出所を知らない。"""
    drafter, _ = _drafter(GOOD)
    result = drafter.draft(BLUEPRINT, key="gen/sum")

    assert result.spec.key == "gen/sum"
    assert len(result.spec.test_cases) == 3
    # Blueprint の KC がそのまま Q-matrix の入口になる（設計原則 P6）。
    assert result.spec.knowledge_components == BLUEPRINT.knowledge_components

    version = build_task_version(result.spec, subject_profile="cs_intro_c", authored_by=AUTHOR)
    assert len(version.q_matrix) == 2
    assert version.reference_solution


def test_the_authoring_prompt_carries_no_personal_data() -> None:
    """作問は学習者のデータを含まない（P7）。

    含まないからこそクラウドのモデルに出せる。ここが PERSONAL に変わったら
    ゲートウェイのポリシーがローカル限定に落とすので、**気づかず外に出る
    ことはない**が、区分そのものを固定しておく。
    """
    drafter, provider = _drafter(GOOD)
    drafter.draft(BLUEPRINT, key="gen/sum")

    sent = "\n".join(m.content for m in provider.calls[0].messages)
    assert "cs.io.formatted_input" in sent
    assert "usr_" not in sent, "利用者 ID がプロンプトに入っている"


def test_the_result_records_which_prompt_and_model_made_it() -> None:
    """再現性（P8）。どの版が出したものか分からない生成物は追跡できない。"""
    drafter, _ = _drafter(GOOD)
    result = drafter.draft(BLUEPRINT, key="gen/sum")
    assert result.prompt_id == "task_draft_ja@2"
    assert result.model == "stub"


@needs_c_compiler
def test_a_good_draft_passes_both_gates() -> None:
    drafter, _ = _drafter(GOOD)
    result = drafter.draft(BLUEPRINT, key="gen/sum")
    version = build_task_version(result.spec, subject_profile="cs_intro_c", authored_by=AUTHOR)

    report = _verifier(mutation_limit=8).verify(version)
    assert report.usable, report.summary()


@needs_c_compiler
def test_a_draft_whose_tests_see_nothing_is_refused() -> None:
    """**生成しただけでは採点に使わない**（ADR 0008）。

    参照解答は通る（門 1 は合格）。だがテストケースは固定の出力しか見て
    いないので、門 2 が落とす。生成の品質を測っているのは門である。
    """
    drafter, _ = _drafter(WEAK)
    result = drafter.draft(BLUEPRINT, key="gen/fixed")
    version = build_task_version(result.spec, subject_profile="cs_intro_c", authored_by=AUTHOR)

    report = _verifier(mutation_limit=10).verify(version)
    assert report.reference_passes is GateOutcome.PASSED
    assert report.gate_two is GateOutcome.FAILED, report.summary()
    assert not report.usable


def test_a_generated_task_is_not_approved_by_being_generated() -> None:
    """**教員レビューを経ていない課題を承認済みにしない**（設計原則 P5）。

    ここが緩むと、生成された課題がレビューを経ずにそのまま出題されうる。
    AI の出力は提案であって確定ではない、という規則は採点だけのものではない。
    """
    drafter, _ = _drafter(GOOD)
    result = drafter.draft(BLUEPRINT, key="gen/sum")
    version = build_task_version(
        result.spec,
        subject_profile="cs_intro_c",
        authored_by=AUTHOR,
        generated_by=result.model,
        generation_prompt_version=result.prompt_id,
    )

    assert version.provenance.review_state is ReviewState.IN_REVIEW
    assert version.provenance.generated_by == "stub"
    # どの版のプロンプトが出したか（P8、承認率の測定に要る）。
    assert version.provenance.generation_prompt_version == "task_draft_ja@2"


def test_a_hand_written_task_is_still_approved_on_the_spot() -> None:
    """生成物でなければ従来どおり。**既存の取り込みを壊さない。**"""
    drafter, _ = _drafter(GOOD)
    result = drafter.draft(BLUEPRINT, key="gen/sum")
    version = build_task_version(result.spec, subject_profile="cs_intro_c", authored_by=AUTHOR)
    assert version.provenance.review_state is ReviewState.APPROVED
    assert version.provenance.generated_by is None


def test_the_course_outline_reaches_the_prompt() -> None:
    """KC は「何を問うか」を決めるが、「どこまでを既習として書いてよいか」は
    決めない。到達目標を渡すと、その範囲の外に出た課題文が減る。
    """
    from aijudge_admin.drafting import _course_section

    section = _course_section(
        Blueprint(
            knowledge_components=("cs.loops",),
            subject_profile="cs_intro_c",
            course_title="プログラミング及び実習 II",
            course_outline="配列と繰り返しを扱う。ポインタは扱わない。",
        )
    )
    assert "プログラミング及び実習 II" in section
    assert "ポインタは扱わない" in section


def test_a_course_without_an_outline_gets_no_section() -> None:
    """**空の節を渡さない。** モデルは「範囲の指定が無い」ではなく
    「範囲は空」と読む余地がある。書かれていない条件は書かないことで伝える。
    """
    from aijudge_admin.drafting import _course_section

    assert (
        _course_section(Blueprint(knowledge_components=("cs.loops",), subject_profile="cs_intro_c"))
        == ""
    )
