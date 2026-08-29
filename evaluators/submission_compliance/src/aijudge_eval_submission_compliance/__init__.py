"""提出そのものを決定的に採点する ── 出したか、間に合ったか、名前は規則どおりか。

**「体裁」は文章の質ではなく事務的な遵守の記録である。** 教員の実採点を
見て分かったことで、採点表の備考欄に残っていたのは `遅延14h` と
`ファイル名` だけだった。段階も 3 つしか使われていない（2023 年度は
比率 1.0 が 20 件・0.5 が 6 件・0.1 が 2 件）。連続的な質の尺度ではない。

**そして任意提出の課題では、出したこと自体に基礎点がある。** 教員の言葉で
「任意課題を提出しただけでも意欲があると見做してある程度の点数を与えて
いる」。実際、提出された 47 件のどの品質観点にも 0 は付いていない。
基礎点は実質的に体裁に与えられている ── だから**既定は満点**で、
不備があれば段ごとに下げる。

## なぜ本文を見ないのか

`report_structure` は必須節・字数・測定値の記載を見て段階を決めていた。
それは教員の体裁とは別物だった。2023 年度の 28 件で比べると:

    いまの規則（節・字数・数値・形式）  完全一致 42.9%
    「提出があれば満点」固定            完全一致 71.4%

**節が無いことを理由に 12 件を減点していたが、教員はその多くに満点を
付けている。** 本文の構造を見る検査は捨てないが、それは採点の観点ではなく
**受理の判定**（読めるか、採点に足るか）に回すものである。

## 締切を見ない理由

**遅延はここで見ない。** 評価は遅延と独立に行い、遅延は評価の結果に対する
減点として別に持つ（ADR 0013）。混ぜると、この観点の κ が「提出の遵守」と
「事務上の遅れ」の混合を測ることになり、しかも遅延の減点を教員が免除した
ときに観点の段階まで動かす羽目になる。

締切は `Task` が持ち、減点は採点ワーカーが採点のあとに当てる。だから
この評価器は `Task` を知らないままでよい。
"""

from __future__ import annotations

import re

from aijudge_core import (
    Artifact,
    CriterionScore,
    EvaluatorKind,
    EvaluatorStatus,
    RubricCriterion,
)
from aijudge_core.ids import CriterionScoreId, EvaluatorResultId, new_id
from aijudge_core.spans import Evidence, WholeSpan
from aijudge_grading import EvaluationOutcome, EvaluationRequest

EVALUATOR_ID = "submission_compliance"


class SubmissionCompliance:
    evaluator_id = EVALUATOR_ID
    kind = EvaluatorKind.DETERMINISTIC

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        criterion = request.criterion or _own_criterion(request)
        if criterion is None:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "この課題に submission_compliance の観点がない"},
            )

        options = request.options or {}
        artifacts = request.submission.gradable_artifacts
        levels = sorted(level.level for level in criterion.levels)

        if not artifacts:
            # 提出物が無い。**基礎点も無い。** ここだけは最低段階にする。
            return _score(
                criterion,
                levels[0],
                artifact=None,
                reasons=["採点できる提出物がありません"],
                raw={"submitted": False},
            )

        reasons = ["提出あり（任意提出の課題なので、これが基礎点になる）"]
        faults = 0

        naming, naming_reason = _filename(artifacts, options)
        reasons.append(naming_reason)
        faults += naming

        kind_fault, kind_reason = _kind(artifacts, options)
        if kind_reason is not None:
            reasons.append(kind_reason)
            faults += kind_fault

        # 不備 1 つにつき 1 段下げる。**下限は最低段階**（提出はあるので
        # そこで止める ── 提出が無い場合と同じ扱いにはならない）。
        index = max(0, len(levels) - 1 - faults)
        return _score(
            criterion,
            levels[index],
            artifact=artifacts[0],
            reasons=reasons,
            raw={"submitted": True, "faults": faults},
        )


def _own_criterion(request: EvaluationRequest) -> RubricCriterion | None:
    """決定的評価器は観点を指定せずに呼ばれる。自分の観点を課題版から探す。"""
    for candidate in request.task_version.criteria:
        if candidate.evaluator_id == EVALUATOR_ID:
            return candidate
    return None


def _filename(artifacts: tuple[Artifact, ...], options: dict[str, object]) -> tuple[int, str]:
    pattern = options.get("filename_pattern")
    if not pattern:
        return 0, "ファイル名の規則が渡されていないので見ていません"
    compiled = re.compile(str(pattern))
    names = [a.filename for a in artifacts]
    if any(compiled.match(name) for name in names):
        return 0, "ファイル名は規則どおりです"
    return 1, f"ファイル名が規則に合いません（{'・'.join(names)}）"


def _kind(artifacts: tuple[Artifact, ...], options: dict[str, object]) -> tuple[int, str | None]:
    declared = options.get("required_kinds")
    if not declared:
        return 0, None
    required = (
        (str(declared),) if isinstance(declared, str) else tuple(str(k) for k in declared)
    )
    kinds = {a.kind.value for a in artifacts}
    if kinds & set(required):
        return 0, f"提出形式 {'・'.join(sorted(kinds))}"
    return 1, f"提出形式 {'・'.join(sorted(kinds))} — 指定は {'・'.join(required)}"


def _score(
    criterion: RubricCriterion,
    level: int,
    *,
    artifact: Artifact | None,
    reasons: list[str],
    raw: dict[str, object],
) -> EvaluationOutcome:
    result_id = EvaluatorResultId(new_id("evr"))
    evidence = ()
    if artifact is not None:
        evidence = (
            Evidence(
                artifact_id=artifact.id,
                artifact_content_hash=artifact.content_hash,
                span=WholeSpan(),
                note="提出そのものを見た（本文は読んでいない）",
            ),
        )
    return EvaluationOutcome(
        status=EvaluatorStatus.OK,
        scores=(
            CriterionScore(
                id=CriterionScoreId(new_id("cs")),
                criterion_id=criterion.id,
                evaluator_result_id=result_id,
                kind=EvaluatorKind.DETERMINISTIC,
                level=level,
                score_ratio=criterion.level_for(level).score_ratio,
                weight=criterion.weight,
                confidence=1.0,
                # 提出の事実は確定する。AI に覆させない（P3）。
                conclusive=True,
                evidence=evidence,
                rationale="。".join(reasons) + "。",
            ),
        ),
        raw_output={**raw, "level": level},
    )


def build() -> SubmissionCompliance:
    return SubmissionCompliance()


__all__ = ["EVALUATOR_ID", "SubmissionCompliance", "build"]
