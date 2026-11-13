"""プログラムの**決まった 1 か所**のコメントを、教員の判定例と比べて判定する AI 評価器。

「`//` だけの行を、直後のコードが何をしているかの説明に書き換える」課題（prog2 exam08 p2:
選択ソートの 5 か所にコメントを書く）のためにある。記入箇所 1 つを観点 1 つにし、
観点ごとに「説明できている／いない」の 2 段で答える。

## なぜこの形か（2026-11-13〜14 の実験・exam08 p2）

モデル（gemma4:e4b）を替えずに、問いの形だけを変えて比べた。正解は教員が 25 件
（125 か所）に付けた判定。

- **全体を読ませて段階を選ばせる**（それまでの `rubric_ai_judge`）: 段階の正解 28%、
  平均で **+1.40 段甘い**。5 か所を 1 回で数えさせると、読み下しや違う内容も
  「説明できている」に数える。
- **箇所を切り出し、模範のコメントと同じ内容かを比べさせる**: 合成の提出では良かったが、
  実際の提出では 2 回の段階の一致が 67% と揺れ、教員より甘い。
- **箇所を切り出し、教員の判定例と比べさせる**（この評価器）: 場所の正解 80〜82%
  （甘い 9・辛い 14）、2 回の判定で場所の一致 119/125。揺れがほぼ消えた。
  問いに「教員の基準は厳しい」と書くと辛い誤りが 39〜41 に増えたので、書かない。

小さいモデルには「良いか」を自由に判断させるより、「この例とこの例のどちらに近いか」を
答えさせる方が安定する、というのが実験の結論である。

## 何をするか

1. **箇所のコメントを切り出す**（決定的）。対象のコードの先頭行（正規表現 `anchor`）の
   直前に続くコメント行と、その行の行末のコメント。`//` と `/* */`。
2. **空なら「できていない」**（モデルを呼ばない。確信度 1.0）。対象のコードが見つからない
   ときも同じ（コードを消した提出。コードを変えたこと自体は `template_diff_check` の
   全体比較の観点が見る）。
3. **判定例を選ぶ**。課題が宣言した判定例のうち、このコメントに意味が近いものを
   「できている」「できていない」から 3 個ずつ（埋め込みの類似度）。埋め込みが使えない
   ときは宣言の順に先頭から（採点を止めない・P2）。
4. **モデルに 3 回訊いて多数決**（自己一貫性）。一致率を確信度にし、割れたら人に回る（P5）。

## 課題が宣言するもの

観点 1 つにつき、この評価器あての検証データを 1 つ（`TaskVersion.test_cases`）。
AI 評価器には検証データが渡らないので、課題版から自分あての分を拾う
（`checklist_ai_judge` と同じ）。

    TestCase(
        name="s2",
        evaluator_id="comment_slot_judge",
        payload={
            "criterion": "explain_min",          # 観点の code
            "anchor": r"^\\s*int\\s+min\\s*=\\s*i\\s*;",  # 対象のコードの先頭行
            "code": "int min = i;",              # モデルに見せる対象のコード
            "reference": "a[i] 以降の最小値の位置を、まず i と仮定する",
            "context": "選択ソートのプログラム",  # 任意（既定「プログラム」）
            "examples": [{"text": "...", "ok": True}, {"text": "...", "ok": False}],
        },
    )

判定例は教員が判定したコメント。課題の定義（`course.yaml`）に書くので、学習者を
特定できる文を入れないこと（2026-11-14 の決定: 匿名化してばらばらにしたものを置く）。
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    EvaluatorStatus,
    Evidence,
    LineSpan,
    RubricCriterion,
    WholeSpan,
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
    instruction_lines,
    instruction_notice,
)

EVALUATOR_ID = "comment_slot_judge"

#: 判定例として見せる数（「できている」「できていない」それぞれ）。実験の値。
EXAMPLES_EACH = 3
ENV_SAMPLES = "AIJUDGE_JUDGE_SAMPLES"
DEFAULT_SAMPLES = 3
#: 埋め込みのモデル。評価器のオプション `embedding_model` が無ければ、類似度の検査と
#: 同じ設定（運用では qwen3-embedding:4b）を使う。どちらも無ければ宣言の順。
ENV_EMBEDDING_MODEL = "AIJUDGE_SIMILARITY_EMBEDDING_MODEL"
#: 理由の欄の上限（字）。モデルが長く書いても答え全体を捨てないよう、切り詰めて受け取る。
REASON_LIMIT = 300
MAX_QUOTE_CHARS = 500


class Example(BaseModel):
    """教員が判定したコメント 1 つ。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1)
    ok: bool


class SlotSpec(BaseModel):
    """記入箇所 1 つの宣言。**課題が決める。**"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criterion: str = Field(min_length=1)
    anchor: str = Field(min_length=1)
    code: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    context: str = "プログラム"
    language: str = "c"
    # 対象のコードの先頭行が何回目に現れたものか（1 始まり）。同じ形の行が 2 度ある課題向け。
    occurrence: int = Field(default=1, ge=1)
    examples: tuple[Example, ...] = ()

    @field_validator("anchor")
    @classmethod
    def _compiles(cls, value: str) -> str:
        re.compile(value)
        return value


class SlotVerdict(BaseModel):
    """モデルの答え（P4 の構造化出力）。"""

    model_config = ConfigDict(extra="ignore")

    reason: str = ""
    ok: bool

    @field_validator("reason", mode="before")
    @classmethod
    def _shorten(cls, value: object) -> object:
        if isinstance(value, str) and len(value) > REASON_LIMIT:
            return value[:REASON_LIMIT]
        return value


PROMPT = PromptTemplate(
    name="comment_slot_judge_ja",
    # 文面を変えたら必ず版を上げること（P8）。版 1 は 2026-11-14 の実験の F1 v2 と同じ文面
    # （書き出しの「選択ソートのプログラム」を `context` にしただけ）。
    version="1",
    template=(
        "{context}の一部に学生が書いたコメントを、教員の基準で判定します。\n\n"
        "対象のコード:\n```{language}\n{code}\n```\n"
        "このコードの役割（模範のコメント）: 「{reference}」\n\n"
        "教員が「説明できている」と判定したコメントの例:\n{good}\n\n"
        "教員が「説明できていない」と判定したコメントの例:\n{bad}\n\n"
        "判定するコメント: 「{comment}」\n\n"
        "上の例と同じ基準で、このコメントは説明できていますか。"
        "\n"
        "reason に、どの例に近いかを 1 文で書き、ok に true か false を入れてください。"
    ),
)


# --------------------------------------------------------------------------
# コメントの切り出し（決定的）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """切り出した箇所。行番号は 1 始まり。対象のコードが無ければ anchor_line は 0。"""

    text: str
    anchor_line: int
    first_line: int
    last_line: int


#: 文字列の定数（エスケープを含む）。
_STRING = re.compile(r'"(?:\\.|[^"\\])*"')


def _comment_text(line: str) -> str | None:
    """行の中のコメントの本文（`//` か `/* */`）。無ければ None。"""
    m = re.search(r"//(.*)$", line)
    if m:
        return m.group(1).strip()
    m = re.search(r"/\*(.*?)(\*/|$)", line)
    if m:
        return m.group(1).strip()
    return None


def extract_slot(source: str, anchor: str, occurrence: int = 1) -> Slot:
    """対象のコードの直前に続くコメント行と、その行の行末のコメントを切り出す。"""
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    pattern = re.compile(anchor)
    hits = [k for k, line in enumerate(lines) if pattern.search(line)]
    if len(hits) < occurrence:
        return Slot(text="", anchor_line=0, first_line=0, last_line=0)
    idx = hits[occurrence - 1]
    parts: list[str] = []
    first = idx
    k = idx - 1
    while k >= 0:
        stripped = lines[k].strip()
        if stripped.startswith(("//", "/*")):
            text = _comment_text(lines[k])
        elif stripped.startswith("*"):
            # 複数行の /* */ の途中の行（先頭の * と閉じの */ を除く）
            text = stripped.rstrip("/").strip("* ").strip()
        else:
            break
        if text:
            parts.insert(0, text)
        first = k
        k -= 1
    # 行末のコメント。文字列の中の `//`（"http://..." など）をコメントと読まない。
    trailing = _comment_text(_STRING.sub('""', lines[idx]))
    if trailing:
        parts.append(trailing)
    return Slot(
        text=" ".join(parts).strip(),
        anchor_line=idx + 1,
        first_line=first + 1,
        last_line=idx + 1,
    )


# --------------------------------------------------------------------------
# 判定例の選び方
# --------------------------------------------------------------------------


def _cos(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def choose_examples(
    comment: str,
    examples: tuple[Example, ...],
    vectors: dict[str, tuple[float, ...]] | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """「できている」「できていない」から近い順に `EXAMPLES_EACH` 個ずつ。

    `vectors` が無ければ宣言の順（埋め込みが使えないときに採点を止めない）。
    """
    pool = list(examples)
    if vectors is not None and comment in vectors:
        target = vectors[comment]
        pool.sort(key=lambda e: -_cos(target, vectors[e.text]) if e.text in vectors else 1.0)
    good = tuple(e.text for e in pool if e.ok)[:EXAMPLES_EACH]
    bad = tuple(e.text for e in pool if not e.ok)[:EXAMPLES_EACH]
    return good, bad


def _bullets(texts: tuple[str, ...]) -> str:
    return "\n".join(f"- 「{t}」" for t in texts) or "- （なし）"


# --------------------------------------------------------------------------
# 評価器
# --------------------------------------------------------------------------


def spec_for(request: EvaluationRequest, criterion: RubricCriterion) -> SlotSpec | None:
    """この観点あての宣言。無ければ None、形が不正なら ValueError。"""
    for case in request.task_version.test_cases:
        if case.evaluator_id != EVALUATOR_ID:
            continue
        if str(case.payload.get("criterion") or "") != criterion.code:
            continue
        try:
            return SlotSpec.model_validate(case.payload)
        except (ValidationError, re.error) as exc:
            raise ValueError(f"観点「{criterion.code}」の宣言が不正です: {exc}") from exc
    return None


class CommentSlotJudge:
    """記入箇所 1 つのコメントを、教員の判定例と比べて判定する（AI）。"""

    evaluator_id = EVALUATOR_ID
    kind = EvaluatorKind.AI
    # 箇所の宣言を課題から読む。画面には編集欄の無い形として出る（壊さない）。
    uses_test_cases = True
    test_case_shape = "comment_slots"

    def __init__(
        self,
        gateway: LlmGateway | None = None,
        *,
        model: str | None = None,
        samples: int | None = None,
        embedding_model: str | None = None,
    ) -> None:
        self._gateway = gateway or default_gateway()
        self._model = model or default_model()
        self._samples = samples if samples is not None else _samples_from_env()
        self._embedding_model = embedding_model

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        criterion = request.criterion
        if criterion is None:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "this evaluator is called per criterion"},
            )
        # 指名された観点だけを見る（`checklist_ai_judge` と同じ理由）。
        if criterion.evaluator_id != EVALUATOR_ID:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "this criterion does not name comment_slot_judge"},
            )
        try:
            spec = spec_for(request, criterion)
        except ValueError as exc:
            return EvaluationOutcome(status=EvaluatorStatus.FAILED, error=str(exc))
        if spec is None:
            # 設定の誤り。**黙って 0 点にしない** ── 失敗なら観点は未採点で人に回る。
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED,
                error=f"観点「{criterion.code}」あての記入箇所の宣言（test_cases）がありません",
            )
        found = self._source(request)
        if found is None:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "no textual artifact to judge"},
            )
        artifact_id, content_hash, source = found
        slot = extract_slot(source, spec.anchor, spec.occurrence)
        if not slot.text:
            return self._blank(criterion, artifact_id, content_hash, slot)

        vectors, selection = self._embed(request, slot.text, spec.examples)
        good, bad = choose_examples(slot.text, spec.examples, vectors)
        samples = int(str(request.options.get("samples", self._samples)))
        model, fallback_model, num_ctx = request.model_choice(self._model)
        try:
            result = self._gateway.sample_structured(
                PROMPT,
                SlotVerdict,
                model=model,
                fallback_model=fallback_model,
                num_ctx=num_ctx,
                # 学習者のコメントは個人に紐づく。ローカルプロバイダ以外へは流れない（P7）。
                data_class=DataClass.PERSONAL,
                samples=samples,
                key="ok",
                timeout_seconds=request.timeout_seconds,
                max_tokens=300,
                context=spec.context,
                language=spec.language,
                code=spec.code,
                reference=spec.reference,
                good=_bullets(good),
                bad=_bullets(bad),
                comment=slot.text,
            )
        except LlmError as exc:
            # LLM が使えなくても採点全体は落とさない（P2）。
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED, error=f"{type(exc).__name__}: {exc}"
            )

        verdict = result.value
        level = _level(criterion, verdict.ok)
        confidence = float(result.agreement)
        judged = "説明できている" if verdict.ok else "説明できていない"
        rationale = f"{judged}と判定しました。{verdict.reason.strip()}".strip()
        # 採点への指示と読める記述があれば人に回す（#411・ほかの AI 評価器と同じ）。
        suspected = instruction_lines(slot.text)
        if suspected:
            confidence = 0.0
            rationale = f"{instruction_notice(suspected)}{rationale}"
        return EvaluationOutcome(
            status=EvaluatorStatus.OK,
            scores=(
                self._score(
                    criterion,
                    level,
                    confidence,
                    self._evidence(artifact_id, content_hash, slot),
                    rationale,
                ),
            ),
            model_id=result.model_id,
            prompt_id=result.prompt_id,
            raw_output={
                "ok": verdict.ok,
                "agreement": result.agreement,
                "samples": result.samples,
                "attempts": result.attempts,
                "provider": result.provider,
                "duration_ms": result.usage.duration_ms,
                "completion_tokens": result.usage.completion_tokens,
                "examples_shown": {"good": len(good), "bad": len(bad)},
                "example_selection": selection,
                "comment_lines": [slot.first_line, slot.last_line],
                "suspected_instruction_lines": list(suspected),
            },
        )

    # -- internals ---------------------------------------------------------

    def _blank(
        self, criterion: RubricCriterion, artifact_id: ArtifactId, content_hash: str, slot: Slot
    ) -> EvaluationOutcome:
        """空欄・対象のコードが無い。モデルを呼ばずに「できていない」。"""
        if slot.anchor_line:
            rationale = "この箇所にコメントが書かれていません。"
            span: LineSpan | WholeSpan = LineSpan(
                start_line=slot.anchor_line, end_line=slot.anchor_line
            )
            note = "コメントを書く箇所（対象のコードの行）"
        else:
            rationale = "この箇所の対象のコードが見つかりません（コードを変えている）。"
            span = WholeSpan()
            note = "対象のコードが見つからない"
        evidence = (
            Evidence(
                artifact_id=artifact_id, artifact_content_hash=content_hash, span=span, note=note
            ),
        )
        return EvaluationOutcome(
            status=EvaluatorStatus.OK,
            scores=(self._score(criterion, _level(criterion, False), 1.0, evidence, rationale),),
            raw_output={"ok": False, "blank": True, "anchor_found": bool(slot.anchor_line)},
        )

    @staticmethod
    def _score(
        criterion: RubricCriterion,
        level: int,
        confidence: float,
        evidence: tuple[Evidence, ...],
        rationale: str,
    ) -> CriterionScore:
        return CriterionScore(
            id=CriterionScoreId(new_id("cs")),
            criterion_id=criterion.id,
            evaluator_result_id=EvaluatorResultId(new_id("evr")),
            kind=EvaluatorKind.AI,
            level=level,
            score_ratio=criterion.level_for(level).score_ratio,
            weight=criterion.weight,
            confidence=confidence,
            # **確定させない。** AI の判定は提案である（P5）。
            conclusive=False,
            evidence=evidence,
            rationale=rationale,
        )

    @staticmethod
    def _evidence(artifact_id: ArtifactId, content_hash: str, slot: Slot) -> tuple[Evidence, ...]:
        return (
            Evidence(
                artifact_id=artifact_id,
                artifact_content_hash=content_hash,
                span=LineSpan(start_line=slot.first_line, end_line=slot.last_line),
                quote=slot.text[:MAX_QUOTE_CHARS] or None,
                note="判定したコメント",
            ),
        )

    def _embed(
        self, request: EvaluationRequest, comment: str, examples: tuple[Example, ...]
    ) -> tuple[dict[str, tuple[float, ...]] | None, str]:
        """判定例を選ぶための埋め込み。使えなければ None（宣言の順で選ぶ）。"""
        model = (
            str(request.options.get("embedding_model") or "")
            or self._embedding_model
            or os.environ.get(ENV_EMBEDDING_MODEL, "").strip()
        )
        # 片側が EXAMPLES_EACH 個以下なら、近さで選んでも全部見せることになる。
        oks = sum(1 for e in examples if e.ok)
        if not model or (oks <= EXAMPLES_EACH and len(examples) - oks <= EXAMPLES_EACH):
            return None, "declared"
        texts = tuple(dict.fromkeys((comment, *(e.text for e in examples))))
        try:
            vectors = self._gateway.embed(texts, model=model, data_class=DataClass.PERSONAL)
        except LlmError:
            return None, "declared"
        return dict(zip(texts, vectors, strict=True)), "embedding"

    def _source(self, request: EvaluationRequest) -> tuple[ArtifactId, str, str] | None:
        """判定するソース。テキストとして読める提出物の最初の 1 つ。"""
        for artifact in request.submission.gradable_artifacts:
            content = request.artifact_contents.get(artifact.id)
            if not content:
                continue
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if text.strip():
                return artifact.id, artifact.content_hash, text
        return None


def _level(criterion: RubricCriterion, ok: bool) -> int:
    """「できている」は最上段、「できていない」は最下段。"""
    levels = sorted(level.level for level in criterion.levels)
    return levels[-1] if ok else levels[0]


def _samples_from_env() -> int:
    raw = os.environ.get(ENV_SAMPLES)
    if not raw:
        return DEFAULT_SAMPLES
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_SAMPLES


def build() -> CommentSlotJudge:
    """entry point から呼ばれるファクトリ。"""
    return CommentSlotJudge()


__all__ = [
    "EVALUATOR_ID",
    "PROMPT",
    "CommentSlotJudge",
    "Example",
    "Slot",
    "SlotSpec",
    "SlotVerdict",
    "build",
    "choose_examples",
    "extract_slot",
    "spec_for",
]
