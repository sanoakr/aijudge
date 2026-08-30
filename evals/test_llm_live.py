"""実モデル（既定はローカルの ollama、`AIJUDGE_LLM_BASE_URL` で差し替え）
に対する検証。

既定では走らない。CI はネットワークに出ないため。

    AIJUDGE_LIVE_LLM=1 uv run pytest evals/test_llm_live.py -v

ここで確かめるのは精度ではなく**契約が実モデルで成立するか**。
- スキーマに合う出力が実際に得られるか（何回の試行で）
- 根拠の行番号が実在する行を指すか
- 1 観点あたり何秒かかるか（PoC-1 の運用設計に効く）

採点精度（κ）の測定は教員採点データが揃ってからで、これはその前段。
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aijudge_authoring.importers import sharif_judge
from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    EvaluatorKind,
    Submission,
    SubmissionState,
    new_id,
)
from aijudge_core.ids import ArtifactId, SubmissionId, TaskVersionId, UserId
from aijudge_eval_rubric_ai_judge import RubricAiJudge
from aijudge_grading import EvaluatorRegistry, GradingPipeline, load_profile
from aijudge_llm_gateway import (
    DEFAULT_BASE_URL,
    LlmError,
    LlmGateway,
    OllamaProvider,
    default_model,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "evals" / "fixtures" / "prog2-2025-ex06-p3"
PROFILE_PATH = REPO_ROOT / "subjects" / "cs_intro_c.yaml"
NOW = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)

live = pytest.mark.skipif(
    os.environ.get("AIJUDGE_LIVE_LLM") != "1",
    reason="set AIJUDGE_LIVE_LLM=1 to run against the real LLM host",
)

pytestmark = live

# 読みにくいが正しく動く提出。テスト実行では満点、読みやすさでは低評価になるはず。
OBFUSCATED = """#include <stdio.h>
int main(){int a,b,c,d=0,e;do{scanf("%d",&a);}while(a<1);
for(e=0;e<a;e++){int f;scanf("%d",&f);if(e==0||f>b)b=f;if(e==0||f<c)c=f;d+=f;}
printf("%d %d %.3f\\n",b,c,(double)d/a);return 0;}
"""


@pytest.fixture(scope="module")
def provider() -> OllamaProvider:
    p = OllamaProvider(os.environ.get("AIJUDGE_LLM_BASE_URL", DEFAULT_BASE_URL))
    try:
        models = p.list_models()
    except LlmError as exc:
        pytest.skip(f"LLM host unreachable: {exc}")
    if default_model() not in models:
        pytest.skip(f"model {default_model()!r} not installed; available: {models}")
    return p


@pytest.fixture(scope="module")
def task_version():
    return sharif_judge.import_problem(
        FIXTURE,
        subject_profile="cs_intro_c",
        authored_by=UserId(new_id("usr")),
        readability_weight=0.3,
    )


def _wire(source: str):
    submission_id = SubmissionId(new_id("sub"))
    payload = source.encode()
    artifact = Artifact(
        id=ArtifactId(new_id("art")),
        submission_id=submission_id,
        role=ArtifactRole.ORIGINAL,
        kind=ArtifactKind.CODE,
        storage_key=f"memory://{submission_id}",
        content_hash=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        byte_size=len(payload),
        created_at=NOW,
    )
    submission = Submission(
        id=submission_id,
        task_version_id=TaskVersionId(new_id("tsv")),
        learner_id=UserId(new_id("usr")),
        state=SubmissionState.SUBMITTED,
        artifacts=(artifact,),
        created_at=NOW,
        submitted_at=NOW,
    )
    return submission, (lambda _: payload)


def _pipeline(provider: OllamaProvider, samples: int) -> GradingPipeline:
    registry = EvaluatorRegistry().load_installed()
    registry.replace(RubricAiJudge(LlmGateway(provider), model=default_model()))
    profile = load_profile(PROFILE_PATH, registry).model_copy(
        update={"evaluator_options": {"rubric_ai_judge": {"samples": samples}}}
    )
    return GradingPipeline(registry, profile)


def test_the_real_model_produces_a_usable_verdict(provider, task_version, capsys) -> None:
    run = _pipeline(provider, samples=1).run(task_version, *_wire(task_version.reference_solution))
    ai = next(s for s in run.criterion_scores if s.kind is EvaluatorKind.AI)
    raw = next(r for r in run.evaluator_results if r.kind is EvaluatorKind.AI).raw_output

    line_count = len(task_version.reference_solution.split("\n"))
    for evidence in ai.evidence:
        assert 1 <= evidence.span.start_line <= line_count
        assert evidence.span.start_line <= evidence.span.end_line <= line_count

    assert ai.level in [level.level for level in task_version.criteria[1].levels]
    assert ai.rationale.strip()

    with capsys.disabled():
        print(
            f"\n  model={run.context.model_ids}  prompt={run.context.prompt_versions}"
            f"\n  level={ai.level} 根拠{len(ai.evidence)}件 "
            f"attempts={raw['attempts']} {raw['duration_ms']}ms"
            f"\n  rationale={ai.rationale[:160]}"
        )


def test_the_model_distinguishes_readable_from_obfuscated(provider, task_version, capsys) -> None:
    """同じ「テスト全通過」でも、読みにくい提出は低く評価されること。

    これが成り立たないなら、この観点に AI を使う意味がない。
    """
    clean = _pipeline(provider, samples=3).run(
        task_version, *_wire(task_version.reference_solution)
    )
    messy = _pipeline(provider, samples=3).run(task_version, *_wire(OBFUSCATED))

    clean_ai = next(s for s in clean.criterion_scores if s.kind is EvaluatorKind.AI)
    messy_ai = next(s for s in messy.criterion_scores if s.kind is EvaluatorKind.AI)

    with capsys.disabled():
        print(
            f"\n  参照解答:   level={clean_ai.level} 一致度={clean_ai.confidence:.2f} "
            f"総合={clean.score_ratio:.3f} {clean.routing.value}"
            f"\n  難読化提出: level={messy_ai.level} 一致度={messy_ai.confidence:.2f} "
            f"総合={messy.score_ratio:.3f} {messy.routing.value}"
        )

    # どちらもテストは全通過するので、差が出るのは読みやすさの観点だけ。
    assert clean.criterion_scores[0].score_ratio == messy.criterion_scores[0].score_ratio == 1.0
    assert messy_ai.level < clean_ai.level


def test_a_settled_criterion_costs_no_llm_call(provider, task_version) -> None:
    """決定的評価が確定させた観点に AI を呼ばない（P3）。実機でも同じ。"""
    single = sharif_judge.import_problem(
        FIXTURE,
        subject_profile="cs_intro_c",
        authored_by=UserId(new_id("usr")),
        readability_weight=0.0,  # 正しさだけの課題
    )
    run = _pipeline(provider, samples=1).run(single, *_wire(single.reference_solution))

    assert [r.kind for r in run.evaluator_results] == [EvaluatorKind.DETERMINISTIC]
    assert run.context.model_ids == {}
