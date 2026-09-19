"""提出物の本文に、課題が宣言した文字列があるかを**決定的に**照合する評価器。

## AI を呼ばない

読めば機械的に分かることは AI に訊かない（P3）。「この行に学籍番号が含まれる
か」は正規表現と文字列一致で決まる。**訊く必要があるのは「画像に何と書いて
あるか」だけ**で、それは抽出器（`image_text`）が受付のときに済ませている。

対象が画像から起こした本文でも、PDF から抜いた本文でも、ここは同じである
── 抽出器が違うだけで、この評価器は「本文」しか見ない。

## 課題が宣言するもの（ADR 0018）

項目は `TaskVersion.test_cases` のうち自分あてのものから取る。

    TestCase(
        name="受講者欄",
        evaluator_id="text_pattern_check",
        payload={"criterion": "nickname", "expect": "learner_reference",
                 "line_matches": r"認定証|体験編"},
    )

- `criterion` — どの観点に効くか（観点の `code`）。省略すると、この評価器を
  指名したすべての観点に効く。
- `pattern` — 満たしたと見なす正規表現。本文全体から探す。
- `expect: learner_reference` — **提出者の学籍番号**が本文にあること。
  課題側に書けない値なので、ここだけは名前で指す。
- `line_matches` — `pattern` / `expect` を**この正規表現に一致する行の
  近くだけ**から探す。省略すると本文全体を見る。
- `required` — 満たさなければ**最低段階にする**。比例配分では「0 にすべき
  提出」を表せない ── 別の講座の認定証にも修了文は書いてあるので、項目の
  一部が満たされて段階 1 になってしまう。

## 段階はこちらが決める

満たした項目の重みを、その観点が持つ段階に比例で割り当てる（`level_for`）。
2 段でも 3 段でも同じ宣言で動く。

`conclusive` は立てる ── **決定的評価の判定は確定である**（P3）。ここで
確定した観点は AI 評価器に回らない。誤りの余地は照合ではなく書き起こしの側に
あり、そちらは抽出器の記録（`TranscriptionMeta`）に残っている。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    EvaluatorStatus,
    Evidence,
    RubricCriterion,
    WholeSpan,
    new_id,
)
from aijudge_core.ids import ArtifactId, CriterionScoreId, EvaluatorResultId
from aijudge_grading.protocol import EvaluationOutcome, EvaluationRequest

EVALUATOR_ID = "text_pattern_check"

#: `expect` に書ける唯一の名前。提出者の学籍番号と照合する。
EXPECT_LEARNER_REFERENCE = "learner_reference"


class PatternSpec(BaseModel):
    """照合する項目 1 つ。**課題が決める。**"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    criterion: str = ""
    pattern: str = ""
    expect: str = ""
    #: 探す範囲を、この正規表現に一致する行に絞る。
    line_matches: str = ""
    required: bool = False
    weight: float = 1.0


def normalise(text: str) -> str:
    """照合の前に空白を落とす。

    認定証の実物は `Y230020　岡本秀和` のように**全角空白**で区切る。
    半角だけを落としていると、区切りの違いだけで一致しなくなる。
    """
    return text.replace(" ", "").replace("　", "").lower()


def reference_forms(reference: str) -> tuple[str, ...]:
    """学籍番号の書かれ方。**ログイン ID はメールのこともある。**

    `User.login` は「学籍番号でもメールでもよい」と決めてある（用途を固定
    しない）。Google ログインの運用では実際に `y230009@mail.example.ac.jp`
    が入る一方、学生が認定証に書くのは `y230009` だけである。**どちらの
    書かれ方も本人として認める** ── ここを片方に決めると、認証方式を
    変えた学期に全員が 0 点になる。
    """
    flat = normalise(reference)
    local = flat.split("@", 1)[0]
    return (flat, local) if local != flat else (flat,)


def lines_to_search(body: str, spec: PatternSpec) -> list[str]:
    """この項目が見てよい行。

    `line_matches` を書くと、そこに一致する行だけを見る。**写り込みで
    当たるのを防ぐため** ── 認定証をブラウザ枠ごと撮った提出では、タブや
    URL の文字列も本文に入る（実データ 47 件のうち 3 件）。「受講者欄に
    学籍番号があるか」を本文全体で見ると、写り込んだ番号でも当たる。
    """
    lines = body.splitlines()
    if not spec.line_matches:
        return lines
    anchor = re.compile(spec.line_matches, re.IGNORECASE)
    return [line for line in lines if anchor.search(line)]


def satisfies(spec: PatternSpec, body: str, learner_reference: str | None) -> bool:
    """この項目は満たされたか。**ここに LLM は関与しない。**"""
    haystack = lines_to_search(body, spec)

    if spec.expect == EXPECT_LEARNER_REFERENCE:
        # 学籍番号が分からなければ**満たしたことにしない。** 分からないまま
        # 通すと、誰の認定証でも通る。
        if not learner_reference:
            return False
        flat = normalise("\n".join(haystack))
        return any(form and form in flat for form in reference_forms(learner_reference))

    if not spec.pattern:
        return any(line.strip() for line in haystack)

    expression = re.compile(spec.pattern, re.IGNORECASE)
    return any(expression.search(line) for line in haystack)


def level_for(satisfied: float, total: float, criterion: RubricCriterion) -> int:
    """満たした項目の重みを、その観点が持つ段階に割り当てる。

    段階数は課題が決める（2 段でも 4 段でもよい）ので比率で対応させる。
    """
    levels = sorted(level.level for level in criterion.levels)
    if total <= 0 or satisfied >= total:
        return levels[-1]
    if satisfied <= 0:
        return levels[0]
    index = round(satisfied / total * (len(levels) - 1))
    return levels[max(0, min(index, len(levels) - 1))]


def specs_of(request: EvaluationRequest, criterion: RubricCriterion) -> tuple[PatternSpec, ...]:
    """この観点のために照合する項目。

    **決定的評価器には `EvaluationRequest.test_cases` が渡る**ので、そちらを
    読む（AI 評価器が `task_version` から拾うのとは経路が違う）。
    """
    specs = []
    for case in request.test_cases:
        if case.evaluator_id != EVALUATOR_ID:
            continue
        spec = _spec_of(case)
        if spec.criterion and spec.criterion != criterion.code:
            continue
        specs.append(spec)
    return tuple(specs)


def _spec_of(case: object) -> PatternSpec:
    payload = dict(getattr(case, "payload", {}) or {})
    return PatternSpec(
        name=str(getattr(case, "name", "")),
        criterion=str(payload.get("criterion") or ""),
        pattern=str(payload.get("pattern") or ""),
        expect=str(payload.get("expect") or ""),
        line_matches=str(payload.get("line_matches") or ""),
        required=bool(payload.get("required") or False),
        weight=float(getattr(case, "weight", 1.0)),
    )


class TextPatternCheck:
    """本文と宣言された文字列を照合する（決定的）。"""

    evaluator_id = EVALUATOR_ID
    kind = EvaluatorKind.DETERMINISTIC
    uses_test_cases = True
    # 入出力の組ではなく項目の並び。**`items` を名乗らない** ── 項目表の
    # 編集欄（`checklist_ai_judge` 用）は `description` と `aliases` しか
    # 持たず、保存のたびに payload を作り直すので、ここの宣言が消える。
    test_case_shape = "patterns"

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        body, artifact_id = self._body(request)
        if body is None or artifact_id is None:
            # 本文が無い。**0 点にしない** ── 読めなかったのか白紙なのかは
            # ここでは分からない（抽出器の記録が理由を持っている）。
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "no extracted text to match against"},
            )

        scores: list[CriterionScore] = []
        matched: dict[str, dict[str, bool]] = {}
        for criterion in request.task_version.criteria:
            if criterion.evaluator_id != EVALUATOR_ID:
                continue
            specs = specs_of(request, criterion)
            if not specs:
                # **当て推量の既定を持たない。** 何を照合するかは課題ごとに違う。
                continue
            met = {spec.name: satisfies(spec, body, request.learner_reference) for spec in specs}
            matched[criterion.code] = met
            unmet_required = [spec.name for spec in specs if spec.required and not met[spec.name]]
            if unmet_required:
                level = min(level.level for level in criterion.levels)
            else:
                level = level_for(
                    sum(spec.weight for spec in specs if met[spec.name]),
                    sum(spec.weight for spec in specs),
                    criterion,
                )
            scores.append(
                CriterionScore(
                    id=CriterionScoreId(new_id("cs")),
                    criterion_id=criterion.id,
                    evaluator_result_id=EvaluatorResultId(new_id("evr")),
                    kind=EvaluatorKind.DETERMINISTIC,
                    level=level,
                    score_ratio=criterion.level_for(level).score_ratio,
                    weight=criterion.weight,
                    confidence=1.0,
                    # **確定させる。** 決定的な照合に迷いは無い（P3）。
                    conclusive=True,
                    evidence=self._evidence(request, artifact_id, specs, body),
                    rationale=self._rationale(met, unmet_required),
                )
            )

        if not scores:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "no criterion names text_pattern_check"},
            )
        return EvaluationOutcome(
            status=EvaluatorStatus.OK,
            scores=tuple(scores),
            raw_output={"matched": matched, "characters": len(body)},
        )

    # -- internals ---------------------------------------------------------

    def _body(self, request: EvaluationRequest) -> tuple[str | None, ArtifactId | None]:
        """照合する本文を選ぶ。

        **提出時の種類では選ばない。** 抽出器が既に本文へ直しているので、
        `kind` は「学習者が何を出したか」の記録であって「いま何が渡っている
        か」ではない（`checklist_ai_judge` と同じ判断）。
        """
        for artifact in request.submission.gradable_artifacts:
            content = request.artifact_contents.get(artifact.id)
            if not content:
                continue
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if text.strip():
                return text, artifact.id
        return None, None

    def _evidence(
        self,
        request: EvaluationRequest,
        artifact_id: ArtifactId,
        specs: tuple[PatternSpec, ...],
        body: str,
    ) -> tuple[Evidence, ...]:
        """照合に使った行を根拠にする（P4）。"""
        content_hash = next(
            (a.content_hash for a in request.submission.artifacts if a.id == artifact_id),
            "unknown",
        )
        return tuple(
            Evidence(
                artifact_id=artifact_id,
                artifact_content_hash=content_hash,
                span=WholeSpan(),
                quote="\n".join(lines_to_search(body, spec))[:500] or None,
                note=f"「{spec.name}」",
            )
            for spec in specs
        )

    def _rationale(self, met: dict[str, bool], unmet_required: list[str]) -> str:
        parts = [
            f"{name}は{'見つかりました' if hit else '見つかりませんでした'}"
            for name, hit in met.items()
        ]
        if unmet_required:
            parts.append(f"必須の項目を満たしていません: {'・'.join(unmet_required)}")
        return "。".join(parts) + "。"


def build() -> TextPatternCheck:
    """entry point から呼ばれるファクトリ。"""
    return TextPatternCheck()


__all__ = [
    "EVALUATOR_ID",
    "EXPECT_LEARNER_REFERENCE",
    "PatternSpec",
    "TextPatternCheck",
    "build",
    "level_for",
    "lines_to_search",
    "normalise",
    "reference_forms",
    "satisfies",
    "specs_of",
]
