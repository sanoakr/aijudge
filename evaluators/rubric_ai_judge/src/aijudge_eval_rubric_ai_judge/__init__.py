"""ルーブリック観点 1 つを LLM に判定させる AI 評価器。

設計方針 §04 step 3 の実装。守る約束は 3 つ。

- **観点ごとに 1 回**呼ぶ。1 回のプロンプトで全観点をまとめて採点させない。
  観点が混ざると根拠が曖昧になり、どの観点で外したのかも分からなくなる。
- **決定的評価の結果を文脈として渡す**。テストが全部通っていると分かっていれば、
  モデルは正しさを再判定せず、担当観点だけを見られる。ここが精度の鍵。
- **根拠なしのスコアを返さない**（P4）。解答の行範囲を必ず引かせ、
  引けなかった判定は捨てる。

確信度は自己一貫性から作る。複数回サンプリングして段階が割れたら、
その割合をそのまま確信度にし、低ければ人間のレビューへ回る（P5）。
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    EvaluatorStatus,
    Evidence,
    LineSpan,
    RubricCriterion,
    new_id,
)
from aijudge_core.ids import ArtifactId, CriterionScoreId, EvaluatorResultId
from aijudge_grading.protocol import EvaluationOutcome, EvaluationRequest
from aijudge_llm_gateway import (
    DataClass,
    LlmError,
    LlmGateway,
    PromptTemplate,
    default_gateway,
    default_model,
)

EVALUATOR_ID = "rubric_ai_judge"

ENV_SAMPLES = "AIJUDGE_JUDGE_SAMPLES"
DEFAULT_SAMPLES = 3


class EvidenceSpan(BaseModel):
    """モデルに引かせる根拠。行番号は提示した番号つきコードのもの。"""

    model_config = ConfigDict(extra="ignore")

    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    quote: str = Field(default="", max_length=2000)


class Verdict(BaseModel):
    """AI 評価器の構造化出力（P4）。"""

    model_config = ConfigDict(extra="ignore")

    level: int = Field(ge=0)
    # 既定値を与えない。既定があると JSON スキーマ上「任意」になり、
    # モデルは根拠を本文に書いておきながら evidence を空で返す
    # （実測でそうなった）。必須にして、空なら Gateway が再試行する。
    evidence: list[EvidenceSpan] = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=4000)


PROMPT = PromptTemplate(
    name="rubric_criterion_judge_ja",
    # 文面を変えたら必ず版を上げること。版が同じで文面が違うと、
    # 過去の採点が何で出たのか追えなくなる（設計原則 P8）。
    version="1",
    system=(
        "あなたは大学のプログラミング演習の採点者です。"
        "指示された観点だけを評価し、根拠として解答の行範囲を必ず示します。"
        "JSON オブジェクトのみを出力し、それ以外の文字は書きません。"
    ),
    template="""# 問題
{statement}

# 今回評価する観点: {criterion_title}
{criterion_description}

## 段階
{levels}

# すでに確定していること
{prior}

# 学習者の解答（行番号つき）
```
{numbered_code}
```

# 指示
- 上の観点だけを評価し、段階の番号を 1 つ選ぶこと。
- すでに確定していることを再評価しないこと。
- `evidence` には解答の行範囲を 1 つ以上入れること。根拠を示せない判定はしない。
- `rationale` は日本語で、学習者が次に何をすればよいか分かるように書くこと。
- 行番号は上に示したものを使うこと。

出力する JSON の形:
{{"level": 整数,
  "evidence": [{{"start_line": 整数, "end_line": 整数, "quote": "該当箇所"}}],
  "rationale": "日本語の説明"}}
""",
)


def number_lines(source: str) -> str:
    """モデルに行範囲を引かせるため、行番号を振って渡す。"""
    lines = source.replace("\r\n", "\n").split("\n")
    width = len(str(len(lines)))
    return "\n".join(f"{index:>{width}} | {line}" for index, line in enumerate(lines, 1))


def describe_levels(criterion: RubricCriterion) -> str:
    return "\n".join(
        f"- {level.level}: {level.label} — {level.descriptor}" for level in criterion.levels
    )


def describe_prior(request: EvaluationRequest) -> str:
    """決定的評価の結果を、モデルが読める形にする。

    これを渡すのが精度の鍵（設計方針 §04 step 3）。
    """
    if not request.prior_results:
        return "（まだ何も確定していません。）"
    lines: list[str] = []
    for score in request.prior_results:
        try:
            title = request.task_version.criterion(score.criterion_id).title
        except KeyError:
            title = score.criterion_id
        state = "確定" if score.conclusive else "参考"
        lines.append(f"- [{state}] {title}: {score.score_ratio:.0%} — {score.rationale}")
    return "\n".join(lines)


class RubricAiJudge:
    """ルーブリック観点を LLM に判定させる。"""

    evaluator_id = EVALUATOR_ID
    kind = EvaluatorKind.AI

    def __init__(
        self,
        gateway: LlmGateway | None = None,
        *,
        model: str | None = None,
        samples: int | None = None,
    ) -> None:
        self._gateway = gateway or default_gateway()
        self._model = model or default_model()
        self._samples = samples if samples is not None else _samples_from_env()

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        criterion = request.criterion
        if criterion is None:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "this evaluator is called per criterion"},
            )

        source_id, source = self._source(request)
        if source is None or source_id is None:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "no textual artifact to judge"},
            )

        samples = int(request.options.get("samples", self._samples))
        try:
            result = self._gateway.sample_structured(
                PROMPT,
                Verdict,
                model=self._model,
                # 学習者の解答は個人に紐づく。ローカルプロバイダ以外へは流れない（P7）。
                data_class=DataClass.PERSONAL,
                samples=samples,
                key="level",
                timeout_seconds=request.timeout_seconds,
                max_tokens=1200,
                statement=request.task_version.statement,
                criterion_title=criterion.title,
                criterion_description=criterion.description,
                levels=describe_levels(criterion),
                prior=describe_prior(request),
                numbered_code=number_lines(source),
            )
        except LlmError as exc:
            # LLM が使えなくても採点全体は落とさない（設計原則 P2 / §04 step 2）。
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )

        verdict = result.value
        line_count = len(source.replace("\r\n", "\n").split("\n"))
        evidence = self._to_evidence(verdict, request, source_id, line_count)
        if not evidence:
            # 根拠を示せない判定は採用しない（P4）。人間に回す材料としては残す。
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED,
                error="the model returned no usable evidence spans",
                raw_output={"verdict": verdict.model_dump(), "prompt_id": result.prompt_id},
            )

        level = self._clamp_level(criterion, verdict.level)
        return EvaluationOutcome(
            status=EvaluatorStatus.OK,
            scores=(
                CriterionScore(
                    id=CriterionScoreId(new_id("cs")),
                    criterion_id=criterion.id,
                    evaluator_result_id=EvaluatorResultId(new_id("evr")),
                    kind=EvaluatorKind.AI,
                    level=level,
                    score_ratio=criterion.level_for(level).score_ratio,
                    weight=criterion.weight,
                    # 一致度をそのまま確信度にする。割れたら人間が見る（P5）。
                    confidence=result.agreement,
                    conclusive=False,
                    evidence=evidence,
                    rationale=verdict.rationale,
                ),
            ),
            model_id=result.model_id,
            prompt_id=result.prompt_id,
            raw_output={
                "verdict": verdict.model_dump(),
                "agreement": result.agreement,
                "samples": result.samples,
                "attempts": result.attempts,
                "provider": result.provider,
                "duration_ms": result.usage.duration_ms,
                "completion_tokens": result.usage.completion_tokens,
            },
        )

    # -- internals ---------------------------------------------------------

    def _source(self, request: EvaluationRequest) -> tuple[ArtifactId | None, str | None]:
        for artifact in request.submission.gradable_artifacts:
            content = request.artifact_contents.get(artifact.id)
            if content is None or artifact.kind.value not in ("code", "latex", "markdown"):
                continue
            return artifact.id, content.decode("utf-8", errors="replace")
        return None, None

    def _clamp_level(self, criterion: RubricCriterion, level: int) -> int:
        """モデルが存在しない段階を返しても落とさず、最も近い段階に寄せる。"""
        available = [item.level for item in criterion.levels]
        if level in available:
            return level
        return min(available, key=lambda candidate: abs(candidate - level))

    def _to_evidence(
        self,
        verdict: Verdict,
        request: EvaluationRequest,
        artifact_id: ArtifactId,
        line_count: int,
    ) -> tuple[Evidence, ...]:
        """モデルが挙げた行範囲を Evidence にする。

        存在しない行を指す根拠は捨てる。捏造された根拠を UI に出さないため。
        """
        content_hash = next(
            (a.content_hash for a in request.submission.artifacts if a.id == artifact_id),
            "unknown",
        )
        evidence: list[Evidence] = []
        for span in verdict.evidence:
            start = span.start_line
            end = max(span.end_line, start)
            if start > line_count:
                continue
            end = min(end, line_count)
            evidence.append(
                Evidence(
                    artifact_id=artifact_id,
                    artifact_content_hash=content_hash,
                    span=LineSpan(start_line=start, end_line=end),
                    quote=span.quote[:2000] or None,
                )
            )
        return tuple(evidence)


def _samples_from_env() -> int:
    raw = os.environ.get(ENV_SAMPLES)
    if not raw:
        return DEFAULT_SAMPLES
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_SAMPLES


def build() -> RubricAiJudge:
    """entry point から呼ばれるファクトリ。"""
    return RubricAiJudge()
