"""レポートの体裁を決定的に判定する。

**AI に判定させないもの**をここで確定させる（設計原則 P3）。必須節が
あるか、字数が足りているか、数値や表が示されているか ── これは読めば
機械的に分かることで、LLM に聞くのは費用の無駄であり、しかも結果が揺れる。

AI に残すのは、実験設計の妥当性や考察の深さのように読解が要る観点である。

## 見出しの探し方

実データ（ネットワーク演習のレポート 19 件）の見出しはこれだけ揺れていた。

    目的 / 1. 目的 / 1.  目的   / １．[目的] / 2.条件: / 5．考察 / 4. 測定結果

行の完全一致で探すと 19 件のうち 7 件しか通らない。**通らないのは体裁が
悪いのではなく、探し方が硬いだけ**なので、番号・全角記号・括弧・コロンを
読み飛ばして本体の語を見る。逆に本文中の「目的」に反応しないよう、
**短い行に限る**（見出しは行を占める）。
"""

from __future__ import annotations

import re

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    EvaluatorStatus,
)
from aijudge_core.ids import CriterionScoreId, EvaluatorResultId, new_id
from aijudge_core.spans import Evidence, WholeSpan
from aijudge_grading import EvaluationOutcome, EvaluationRequest

EVALUATOR_ID = "report_structure"

# 見出しとして扱う行の最大長。これを超える行は本文と見る。
MAX_HEADING_LENGTH = 30

# 行頭に付く番号や記号。`1.` `1．` `１．` `(1)` `第1章` などを読み飛ばす。
_LEADING = re.compile(r"^[\s　]*(?:第?[0-9０-９]+[\.．、）\)章節]?)?[\s　]*[\[［【]?")
# 行末の記号。`:` `：` `]` `】` などを落とす。
_TRAILING = re.compile(r"[\]］】：:\s　]*$")

# 既定の必須節と、実データに現れた言い換え。課題ごとに
# `evaluator_options.report_structure.sections` で差し替えられる。
DEFAULT_SECTIONS: dict[str, tuple[str, ...]] = {
    "目的": ("目的", "背景と目的", "はじめに"),
    "条件": ("条件", "実験条件", "実験環境", "環境", "測定条件"),
    "方法": ("方法", "実験方法", "手順", "測定方法"),
    "結果": ("結果", "実験結果", "測定結果"),
    "考察": ("考察", "議論"),
}

# 既定の下限。課題ごとに差し替えられる。
DEFAULT_MIN_CHARACTERS = 800
# 数値の記載があると見なす最小の個数。性能評価レポートなので、
# 測定値が 1 つも無ければ結果を示していない。
DEFAULT_MIN_NUMBERS = 8

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _heading(line: str) -> str:
    """行から見出しの本体を取り出す。見出しでなければ空文字。"""
    if len(line.strip()) > MAX_HEADING_LENGTH:
        return ""
    body = _LEADING.sub("", line)
    body = _TRAILING.sub("", body)
    return body.strip()


def found_sections(text: str, sections: dict[str, tuple[str, ...]]) -> dict[str, bool]:
    """必須節ごとに、それらしい見出しがあったか。"""
    headings = [h for h in (_heading(line) for line in text.splitlines()) if h]
    out: dict[str, bool] = {}
    for name, synonyms in sections.items():
        out[name] = any(
            any(synonym in heading for synonym in synonyms) for heading in headings
        )
    return out


class ReportStructure:
    evaluator_id = EVALUATOR_ID
    kind = EvaluatorKind.DETERMINISTIC

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        criterion = request.criterion
        if criterion is None:
            # 決定的評価器は観点を指定せずに呼ばれる（パイプラインの作り）。
            # 自分が担当する観点を課題版から探す。
            candidates = [
                c
                for c in request.task_version.criteria
                if c.evaluator_id == self.evaluator_id
            ]
            if not candidates:
                return EvaluationOutcome(
                    status=EvaluatorStatus.SKIPPED,
                    raw_output={"reason": "この課題に report_structure の観点がない"},
                )
            criterion = candidates[0]

        options = request.options or {}
        sections = _sections_from(options)
        min_chars = int(options.get("min_characters", DEFAULT_MIN_CHARACTERS))
        min_numbers = int(options.get("min_numbers", DEFAULT_MIN_NUMBERS))

        found = _readable_artifact(request)
        text = None if found is None else found[1]
        if text is None:
            # 本文が読めない。**0 点にしない。** 読めないのは学習者の責任とは
            # 限らず（スキャン PDF、暗号化）、体裁の評価は成立しない。
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED,
                error=(
                    "提出物から本文を取り出せませんでした"
                    "（PDF に文字が埋め込まれていない可能性）"
                ),
                raw_output={"readable": False},
            )

        present = found_sections(text, sections)
        missing = [name for name, ok in present.items() if not ok]
        characters = len(re.sub(r"\s", "", text))
        numbers = len(_NUMBER.findall(text))

        # 満たした条件の数で段階を決める。節・字数・数値の 3 本立て。
        checks = [
            not missing,
            characters >= min_chars,
            numbers >= min_numbers,
        ]
        satisfied = sum(1 for ok in checks if ok)
        level = _level_for(satisfied, len(checks), criterion)

        reasons = []
        if missing:
            reasons.append(f"見つからない節: {'・'.join(missing)}")
        else:
            reasons.append("必須の節はすべてあります")
        reasons.append(
            f"本文 {characters} 字（下限 {min_chars}）"
            + ("" if characters >= min_chars else " — 不足")
        )
        reasons.append(
            f"数値の記載 {numbers} 個（下限 {min_numbers}）"
            + ("" if numbers >= min_numbers else " — 不足")
        )

        result_id = EvaluatorResultId(new_id("evr"))
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
                    # **体裁は確定させる。** 読めば分かることを AI に覆させない（P3）。
                    conclusive=True,
                    # 根拠は提出物そのもの（P4）。どの提出のどの版を見たかを
                    # 残す ── 再提出のあとに「この点はどの版に付いたか」を
                    # 追えなくなる。
                    evidence=(
                        Evidence(
                            artifact_id=found[0].id,
                            artifact_content_hash=found[0].content_hash,
                            span=WholeSpan(),
                            note="提出物全体の体裁を見た",
                        ),
                    ),
                    rationale="。".join(reasons) + "。",
                ),
            ),
            raw_output={
                "readable": True,
                "sections": present,
                "missing": missing,
                "characters": characters,
                "numbers": numbers,
                "min_characters": min_chars,
                "min_numbers": min_numbers,
            },
        )


def _sections_from(options: dict[str, object]) -> dict[str, tuple[str, ...]]:
    """課題が節を宣言していればそれを使う。

    宣言の形は `{"目的": ["目的", "はじめに"], ...}` か、言い換えを省いた
    `["目的", "条件"]`。省いた場合は節名そのものだけを探す。
    """
    declared = options.get("sections")
    if not declared:
        return DEFAULT_SECTIONS
    if isinstance(declared, dict):
        return {
            str(name): tuple(str(s) for s in synonyms) or (str(name),)
            for name, synonyms in declared.items()
        }
    if isinstance(declared, list):
        return {str(name): (str(name),) for name in declared}
    return DEFAULT_SECTIONS


def _readable_artifact(request: EvaluationRequest):
    """本文が読める提出物と、その本文。

    正規化器が既に文字にしているはず（§4 step 1）。していなければ
    バイナリのまま届くので、読めないものとして扱う。
    """
    for artifact in request.submission.gradable_artifacts:
        payload = request.artifact_contents.get(artifact.id)
        if payload is None:
            continue
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if text.strip():
            return artifact, text
    return None


def _level_for(satisfied: int, total: int, criterion) -> int:
    """満たした条件の数を、その観点が持つ段階に割り当てる。

    観点の段階数は課題が決める（2 段階でも 4 段階でもよい）ので、
    比率で対応させる。
    """
    levels = sorted(level.level for level in criterion.levels)
    if satisfied >= total:
        return levels[-1]
    if satisfied <= 0:
        return levels[0]
    index = round(satisfied / total * (len(levels) - 1))
    return levels[max(0, min(index, len(levels) - 1))]


def build() -> ReportStructure:
    return ReportStructure()


__all__ = [
    "DEFAULT_MIN_CHARACTERS",
    "DEFAULT_MIN_NUMBERS",
    "DEFAULT_SECTIONS",
    "EVALUATOR_ID",
    "ReportStructure",
    "build",
    "found_sections",
]
