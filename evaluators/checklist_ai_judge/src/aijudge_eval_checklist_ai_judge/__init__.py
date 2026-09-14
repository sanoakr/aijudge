"""課題が求める**項目が提出物に含まれているか**を LLM に判定させる AI 評価器。

`rubric_ai_judge` との違いは、問いの形である。

    rubric_ai_judge     観点 1 つを読んで、**段階を選ぶ**（質の判断）
    checklist_ai_judge  項目 1 つずつを **あるか無いか**で答える（有無の判断）

レポートの必須節（目的・条件・方法・結果・考察）がいちばん分かりやすい例だが、
**構成に限らない**。「単位を付けた」「参考文献を挙げた」「エラー処理に触れた」
のように、**独立した素な項目を積み上げて点にする**ものはこの形で書ける。
項目は課題が決め、重みも項目ごとに持てる。

## なぜ機械的に探さないのか（#302）

必須節の判定は以前 `report_structure` が決定的に行っていた ── 見出しらしい
短い行に節名が含まれるかを正規表現で探していた。**それはレポートの実物に
対して成立しない。**

- **PDF には見出しという構造が無い。** 座標付きの文字列があるだけで、読めるのは
  抽出器が並べ直した行である。実データ 19 件のうち 1 件は 1 文字ずつ改行された
  テキストになり、節も字数も判定できなかった（`aijudge_norm_document_text`）。
- **言葉は揺れる。** 「目的」「背景と目的」「研究の動機」。言い換えの表を持つ
  形にしていたが、表に無い書き方は落ちる。
- **緩めると本文に反応する。** 部分一致にしていたので「本実験の目的は次の
  とおりである。」という 1 文が「目的の節がある」になった。

教員の実採点（2023 年度 28 件）との完全一致は 42.9% で、**教員が満点を付けた
レポートを「節が無い」で減点していた**。そのため採点から外されていた
（`subjects/report_ja.yaml` の記録）。

**設計原則 P3 は「読めば機械的に分かることを AI に聞くな」である。** 項目の
有無は、提出物が PDF である限り機械的に分かることではない ── だからここは
AI の仕事になる。

## 何を返すか

**段階は LLM に選ばせない。** 項目ごとに「あるか・どの行か」だけを答えさせ、
点は**こちらが重みで積み上げて決める**（`level_for`）。採点の目盛りをモデルに
預けない ── 任せるのは読解だけである。

出力は必ず根拠つき（P4）。あると答えた項目は提出物の行を引かせ、実在しない
行を指す根拠は捨てる。確信度は自己一貫性から作り、割れたら人に回る（P5）。
`conclusive` は立てない ── AI の判定は提案であって確定ではない。

## 項目はどこから来るか

**課題が持つ**（`TaskVersion.test_cases` のうち自分あての分）。`TestCase` は
「決定的評価器が使う検証データ」として作られた型だが、コアの定義どおり
「レポートの必須節リストも、すべてこの型に載せる」（`aijudge_core.task`）。

    TestCase(name="目的", evaluator_id="checklist_ai_judge", weight=1.0,
             payload={"description": "何を確かめる実験かが書かれている",
                      "aliases": ["目的", "背景と目的"]})

課題が 1 件も持たなければ科目プロファイルの `items`（旧 `sections` も読む）、
それも無ければ `DEFAULT_ITEMS`。**プロファイルは複数のコースが共有する雛形**
なので、1 問ごとの「何を書かせるか」をそこに書くと他のコースの採点まで変わる。
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
)

EVALUATOR_ID = "checklist_ai_judge"

ENV_SAMPLES = "AIJUDGE_JUDGE_SAMPLES"
DEFAULT_SAMPLES = 3


class ChecklistItem(BaseModel):
    """求める項目 1 つ。**課題が決める。**"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    #: 何をもって「ある」とするか。書かなければ名前だけで判断させる。
    description: str = ""
    #: 言い換えの例。**一致の条件ではなく助言である** ── 例に無い書き方でも
    #: 役割が同じなら「ある」と答えてよい、という判断をさせたい。
    aliases: tuple[str, ...] = ()
    #: 積み上げるときの重み。**等しく数えるなら 1.0 のまま**（既定）。
    weight: float = 1.0


# 課題も科目も項目を宣言していないときの落とし先。実験レポートの標準的な
# 構成で、実データ（ネットワーク演習 19 件）に現れた言い換えを添えてある。
DEFAULT_ITEMS: tuple[ChecklistItem, ...] = (
    ChecklistItem(name="目的", aliases=("目的", "背景と目的", "はじめに")),
    ChecklistItem(name="条件", aliases=("条件", "実験条件", "実験環境", "環境", "測定条件")),
    ChecklistItem(name="方法", aliases=("方法", "実験方法", "手順", "測定方法")),
    ChecklistItem(name="結果", aliases=("結果", "実験結果", "測定結果")),
    ChecklistItem(name="考察", aliases=("考察", "議論")),
)


class ItemFinding(BaseModel):
    """項目 1 つについての判定。"""

    model_config = ConfigDict(extra="ignore")

    item: str = Field(min_length=1, max_length=100)
    present: bool
    # 見つけた場所の行。**`present` のときだけ意味を持つ。** 0 を許すのは、
    # 無いと答えるときに何を書かせるかでモデルを迷わせないため。
    line: int = Field(default=0, ge=0)
    quote: str = Field(default="", max_length=500)


class ChecklistVerdict(BaseModel):
    """項目表の照合結果（P4 の構造化出力）。"""

    model_config = ConfigDict(extra="ignore")

    findings: list[ItemFinding] = Field(min_length=1)

    @property
    def present_items(self) -> tuple[str, ...]:
        """**自己一貫性の照合先**（`sample_structured` の `key`）。

        段階ではなく「どの項目があると答えたか」の組で一致を見る ── この
        評価器は段階を答えないので、割れているかどうかはここにしか出ない。
        """
        return tuple(sorted(f.item for f in self.findings if f.present))


PROMPT = PromptTemplate(
    name="checklist_items_judge_ja",
    # 文面を変えたら必ず版を上げること（P8）。版が同じで文面が違うと、
    # 過去の採点が何で出たのか追えなくなる。
    version="1",
    system=(
        "あなたは大学の課題の採点補助です。"
        "指定された項目が提出物に含まれているかだけを判定し、"
        "根拠として本文の行番号を示します。"
        "JSON オブジェクトのみを出力し、それ以外の文字は書きません。"
    ),
    template="""# 課題
{statement}

# 判定する項目
{items}

# 学習者の提出物（行番号つき）
```
{numbered_text}
```

# 指示

項目ごとに、提出物に**その内容が含まれているか**を答える。

## 守ること
- **言葉ではなく中身で判断する。** 「研究の動機」は目的にあたり、
  「実験の流れ」は方法にあたる。添えた例は例であって、そこに無い書き方でも
  求めている内容が書かれていれば `present` は true にする。
- **触れているだけのものを「ある」としない。** 「本実験の目的は…」という
  1 文があるだけでは、目的の項目を満たしたことにはならない。求めた内容が
  そこに書かれていること。
- PDF から取り出した本文なので、**行が崩れていることがある**（途中で改行
  される、番号と見出しが離れる）。崩れていても内容として読み取れるなら、
  あると判断してよい。
- `present` が true の項目は、その内容が始まる行番号を `line` に入れ、
  その行の文字列を `quote` に写す。**行番号は上に示したものを使う。**
- `present` が false の項目は `line` を 0 にする。
- 上に挙げた項目だけを答える。増やさない。減らさない。
- **点は付けない。** 良し悪しや段階は判断しない。

出力する JSON の形:
{{"findings": [{{"item": "項目の名前", "present": true,
                 "line": 整数, "quote": "その行の文字列"}}]}}
""",
)


def number_lines(source: str) -> str:
    """モデルに行を引かせるため、行番号を振って渡す。"""
    lines = source.replace("\r\n", "\n").split("\n")
    width = len(str(len(lines)))
    return "\n".join(f"{index:>{width}} | {line}" for index, line in enumerate(lines, 1))


def describe_items(items: tuple[ChecklistItem, ...]) -> str:
    out = []
    for item in items:
        line = f"- {item.name}"
        if item.description:
            line += f": {item.description}"
        others = [a for a in item.aliases if a != item.name]
        if others:
            line += f"（例: {'・'.join(others)}）"
        out.append(line)
    return "\n".join(out)


def items_of(request: EvaluationRequest) -> tuple[tuple[ChecklistItem, ...], str]:
    """この課題が求める項目と、その出どころ（課題 / 科目 / 既定）。

    **AI 評価器には `EvaluationRequest.test_cases` が渡らない**（パイプラインは
    決定的評価器にだけ渡す）。課題版そのものは渡るので、そこから自分あての
    ものを拾う。
    """
    declared = tuple(
        _item_of(case)
        for case in request.task_version.test_cases
        if case.evaluator_id == EVALUATOR_ID
    )
    if declared:
        return declared, "task"
    from_profile = _items_from(request.options or {})
    if from_profile:
        return from_profile, "profile"
    return DEFAULT_ITEMS, "default"


def _item_of(case) -> ChecklistItem:
    raw = case.payload.get("aliases") or ()
    if isinstance(raw, str):
        raw = [raw]
    return ChecklistItem(
        name=str(case.name),
        description=str(case.payload.get("description") or ""),
        aliases=tuple(str(a).strip() for a in raw if str(a).strip()),
        weight=float(case.weight),
    )


def _items_from(options: dict[str, object]) -> tuple[ChecklistItem, ...]:
    """科目プロファイルの宣言。

    `{"目的": ["目的", "はじめに"]}` でも `["目的", "条件"]` でもよい。
    `sections` も読む ── この評価器は `report_structure` の後継で、
    既存のプロファイルがその名前で節を宣言している。
    """
    declared = options.get("items") or options.get("sections")
    if not declared:
        return ()
    if isinstance(declared, dict):
        return tuple(
            ChecklistItem(name=str(name), aliases=tuple(str(a) for a in aliases))
            for name, aliases in declared.items()
        )
    if isinstance(declared, list | tuple):
        return tuple(ChecklistItem(name=str(name)) for name in declared)
    return ()


class ChecklistAiJudge:
    """課題が求める項目が提出物に含まれているかを LLM に判定させる。"""

    evaluator_id = EVALUATOR_ID
    kind = EvaluatorKind.AI
    # 項目表を課題から受け取る（#302）。**画面はこの宣言で欄を出す。**
    uses_test_cases = True
    # 課題が持つ検証データの形。入出力の組ではなく項目の並びである。
    test_case_shape = "items"

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
        # **指名された観点だけを見る。** パイプラインは AI 評価器を
        # 「評価器を指名していない観点」にも回す（`criterion.evaluator_id` が
        # None なら、どの AI 評価器も対象になる）。科目が AI 評価器を 2 つ
        # 宣言していると、指名の無い観点が両方に判定されて点が二重に付く
        # ── 項目の有無を問う評価器は、質を問う観点に答えてはいけない。
        #
        # LLM を呼ぶ前に返すので、費用も待ち時間も発生しない。
        if criterion.evaluator_id != EVALUATOR_ID:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "this criterion does not name checklist_ai_judge"},
            )

        source_id, source = self._source(request)
        if source is None or source_id is None:
            # 本文が読めない。**0 点にしない** ── 読めないのは学習者の責任とは
            # 限らず（スキャン画像の PDF、暗号化）、項目の照合は成立しない。
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "no textual artifact to judge"},
            )

        items, items_from = items_of(request)
        samples = int(request.options.get("samples", self._samples))
        try:
            result = self._gateway.sample_structured(
                PROMPT,
                ChecklistVerdict,
                model=self._model,
                # 提出物は個人に紐づく。ローカルプロバイダ以外へは流れない（P7）。
                data_class=DataClass.PERSONAL,
                samples=samples,
                key="present_items",
                timeout_seconds=request.timeout_seconds,
                max_tokens=1200,
                statement=request.task_version.statement,
                items=describe_items(items),
                numbered_text=number_lines(source),
            )
        except LlmError as exc:
            # LLM が使えなくても採点全体は落とさない（P2 / §04 step 2）。
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )

        verdict = result.value
        line_count = len(source.replace("\r\n", "\n").split("\n"))
        # **課題が求めた項目だけを見る。** モデルが増やした項目は捨て、答え
        # なかった項目は「無い」とする ── 求めた数で数えなければ、段階が
        # モデルの出力の長さで変わる。
        answered = {f.item: f for f in verdict.findings}
        found = {
            item.name: (
                answered[item.name]
                if item.name in answered and answered[item.name].present
                else None
            )
            for item in items
        }
        missing = [name for name, hit in found.items() if hit is None]

        evidence = self._to_evidence(found, request, source_id, line_count)
        satisfied = sum(item.weight for item in items if found[item.name] is not None)
        total = sum(item.weight for item in items)
        level = level_for(satisfied, total, criterion)

        reasons = [
            "求めた項目はすべてあります"
            if not missing
            else f"見つからない項目: {'・'.join(missing)}"
        ]
        for name, hit in found.items():
            if hit is not None:
                reasons.append(f"「{name}」{hit.line} 行目")

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
                    # 一致度をそのまま確信度にする。割れたら人が見る（P5）。
                    confidence=result.agreement,
                    # **確定させない。** AI の判定は提案である（P5）。
                    conclusive=False,
                    evidence=evidence,
                    rationale="。".join(reasons) + "。",
                ),
            ),
            model_id=result.model_id,
            prompt_id=result.prompt_id,
            raw_output={
                "items_from": items_from,
                "items": {name: hit is not None for name, hit in found.items()},
                "item_lines": {name: hit.line for name, hit in found.items() if hit is not None},
                "missing": missing,
                "satisfied_weight": satisfied,
                "total_weight": total,
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
        """判定する本文を選ぶ。

        **提出時の種類では選ばない**（`rubric_ai_judge` と同じ理由）。正規化器
        が既に本文へ直しているので、`kind` は「学習者が何を出したか」の記録で
        あって「いま何が渡っているか」ではない。種類で絞ると、PDF で出された
        レポートを素通りさせる（実提出 19 件のうち 18 件でそうなった）。
        """
        for artifact in request.submission.gradable_artifacts:
            content = request.artifact_contents.get(artifact.id)
            if content is None:
                continue
            try:
                # errors="replace" にしない。バイナリが「文字化けした本文」
                # として通り、モデルにゴミを判定させることになる。
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if text.strip():
                return artifact.id, text
        return None, None

    def _to_evidence(
        self,
        found: dict[str, ItemFinding | None],
        request: EvaluationRequest,
        artifact_id: ArtifactId,
        line_count: int,
    ) -> tuple[Evidence, ...]:
        """あると答えた項目の行を根拠にする。

        **実在しない行を指す根拠は捨てる**（捏造を画面に出さない）。1 つも
        残らなければ提出物全体を指す根拠を 1 つ置く ── AI の判定は根拠を
        持たなければ保存できない（P4・`GradingRun` の検証）。「どこにも
        無かった」も、本文全体を読んだ結果である。
        """
        content_hash = next(
            (a.content_hash for a in request.submission.artifacts if a.id == artifact_id),
            "unknown",
        )
        evidence: list[Evidence] = []
        for name, hit in found.items():
            if hit is None or not (1 <= hit.line <= line_count):
                continue
            evidence.append(
                Evidence(
                    artifact_id=artifact_id,
                    artifact_content_hash=content_hash,
                    span=LineSpan(start_line=hit.line, end_line=hit.line),
                    quote=hit.quote[:500] or None,
                    note=f"「{name}」",
                )
            )
        if evidence:
            return tuple(evidence)
        return (
            Evidence(
                artifact_id=artifact_id,
                artifact_content_hash=content_hash,
                span=WholeSpan(),
                note="本文全体を読んだが、求めた項目が見つからなかった",
            ),
        )


def level_for(satisfied: float, total: float, criterion: RubricCriterion) -> int:
    """満たした項目の重みを、その観点が持つ段階に割り当てる。

    段階数は課題が決める（2 段でも 4 段でもよい）ので比率で対応させる。
    **数えるのはこちらの仕事である** ── モデルに段階を選ばせない。
    """
    levels = sorted(level.level for level in criterion.levels)
    if total <= 0 or satisfied >= total:
        return levels[-1]
    if satisfied <= 0:
        return levels[0]
    index = round(satisfied / total * (len(levels) - 1))
    return levels[max(0, min(index, len(levels) - 1))]


def _samples_from_env() -> int:
    raw = os.environ.get(ENV_SAMPLES)
    if not raw:
        return DEFAULT_SAMPLES
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_SAMPLES


def build() -> ChecklistAiJudge:
    """entry point から呼ばれるファクトリ。"""
    return ChecklistAiJudge()


__all__ = [
    "DEFAULT_ITEMS",
    "EVALUATOR_ID",
    "ChecklistAiJudge",
    "ChecklistItem",
    "ChecklistVerdict",
    "ItemFinding",
    "build",
    "items_of",
    "level_for",
]
