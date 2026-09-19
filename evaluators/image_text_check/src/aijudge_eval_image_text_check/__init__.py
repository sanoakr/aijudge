"""提出された**画像に何が書いてあるか**を読み、課題が宣言した条件と照合する評価器。

## 何をモデルにさせ、何をさせないか

**モデルにさせるのは書き起こしだけである。** 「この認定証は合格か」「この学生は
要件を満たしたか」は訊かない。訊くのは「この場所に何と書いてあるか」だけで、
段階を決めるのは下の決定的な照合（正規表現と文字列一致）である。

    モデル   画像 → 「受講者欄には `Y240040naka` と書いてある」
    こちら   `y240040` in `y240040naka` → 満たす

設計原則 P3 の素直な帰結である ── 文字列が一致するかは読めば機械的に分かること
なので、AI に訊かない。**訊いてよいのは「画像に何と書いてあるか」だけ**で、
そこだけが機械に読めない部分である。

この分け方には測定上の理由もある。2026-09-19 に ex1/paiza1 の実提出 47 件で
測ったとき、**誤りはすべて「書き起こしが学籍番号を落とす」形で、しかも一方向
だった** ── 要件を満たした学生を 0 点にする側にしか外れず、逆向き（書いて
いない学籍番号を捏造して 0 点を 1 点にする）は 141 回の書き起こしで 1 度も
起きなかった。モデルに段階を選ばせていたら、この非対称は保証されない。

## 課題が宣言するもの（ADR 0018）

項目は `TaskVersion.test_cases` のうち自分あてのものから取る
（`checklist_ai_judge` と同じ仕組み）。

    TestCase(
        name="受講者欄",
        evaluator_id="image_text_check",
        weight=1.0,
        payload={
            "criterion": "nickname",
            "where": "認定証の中央、講座名の下にある横線の上に書かれた 1 行。"
                     "左端から右端まで一字も落とさずそのまま写す。"
                     "氏名だけを抜き出してはならない",
            "expect": "learner_reference",
        },
    )

- `where` — **どこに何が書いてあるか。そのままプロンプトに載る。**
  ここが精度を決める。実測では「受講者名」と呼んだだけのプロンプトが
  `Y230020　岡本秀和` から学籍番号を落とし（47 件中 5 件）、上の文面に
  変えたら 0 件になった。**文面は課題の持ち物**であって評価器の持ち物では
  ない ── 認定証と答案用紙では「どこを見ろ」が違う。
- `criterion` — この項目がどの観点に効くか（観点の `code`）。省略すると
  その評価器を指名したすべての観点に効く。
- `pattern` — 満たしたと見なす正規表現。省略すると「空でなければ満たす」。
- `anywhere` — `pattern` を**他の項目の書き起こしに対しても**探す。
  同じ事実が画像の 2 か所に書いてあるときに使う。実例: paiza の認定証は
  講座名を題と修了文の 2 回書いており、OCR が題の「体験**編**1」から
  1 文字落としても修了文の側で決まる（実測で 1 件これに救われた）。
- `expect: learner_reference` — 期待する文字列が**提出者の学籍番号**である
  こと。課題側に書けない値なので、ここだけは名前で指す。
- `required` — 満たさなければ**最低段階にする**。比例配分では「0 にすべき
  提出」を表せない ── 別の講座の認定証にも修了文は書いてあるので、項目の
  一部が満たされて段階 1 になってしまう。

## 何を返すか

段階は**満たした項目の重み**から決める（`level_for`）。観点が持つ段数に
比例で割り当てるので、2 段でも 3 段でも同じ宣言で動く。

根拠は書き起こした文字列そのもの（P4）。`conclusive` は立てない ── AI の
判定は提案であって確定ではない（P5）。書き起こしが割れたら確信度が下がり、
人のレビューへ回る。

## 扱わないもの

**PDF は採点しない。** 描画手段を持たないため（PyMuPDF は AGPL で、
Apache-2.0 のこの repository には入れられない）、SKIP を返して人へ回す。
**0 点にはしない** ── 読めないのは学習者の落ち度とは限らない。
"""

from __future__ import annotations

import base64
import os
import re

from pydantic import BaseModel, ConfigDict, Field

from aijudge_core import (
    ArtifactKind,
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
from aijudge_llm_gateway import (
    DataClass,
    LlmError,
    LlmGateway,
    PromptTemplate,
    default_vision_gateway,
    default_vision_model,
)

EVALUATOR_ID = "image_text_check"

ENV_SAMPLES = "AIJUDGE_JUDGE_SAMPLES"
DEFAULT_SAMPLES = 3

#: `expect` に書ける唯一の名前。提出者の学籍番号と照合する。
EXPECT_LEARNER_REFERENCE = "learner_reference"

#: モデルに渡す画像の形式。ollama はこの 2 つをそのまま受ける。
READABLE_KINDS = frozenset({ArtifactKind.IMAGE})


class FieldSpec(BaseModel):
    """読み取る項目 1 つ。**課題が決める。**"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    #: 画像のどこに何が書いてあるか。そのままプロンプトに載る。
    where: str = ""
    #: どの観点に効くか（観点の `code`）。空ならすべての観点に効く。
    criterion: str = ""
    #: 満たしたと見なす正規表現。空なら「書き起こしが空でなければ満たす」。
    pattern: str = ""
    #: `pattern` を他の項目の書き起こしにも探すか。
    anywhere: bool = False
    #: 期待する文字列が提出者の学籍番号であること。
    expect: str = ""
    #: これを満たさなければ**最低段階にする**（重みの比では決めない）。
    #:
    #: 比例配分だけでは「0 にすべき提出」を表せない。実例: 認定証の観点は
    #: 段階 0 を「別の講座」と定義しているが、別の講座の認定証にも修了文は
    #: 書いてあるので、項目の一部が満たされて段階 1 になる ── **課題と
    #: 無関係な認定証が半分の点を取る。** 講座名を `required` にすると、
    #: そこが外れた時点で 0 になる。
    required: bool = False
    weight: float = 1.0


class FieldText(BaseModel):
    """1 項目の書き起こし。"""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=100)
    # **既定値を与える。** 「書いていない」を空文字で表せないと、モデルは
    # 空欄を埋めにかかる（`checklist_ai_judge` の evidence とは逆向きの判断
    # ── あちらは根拠を省かせないために必須にしている）。
    text: str = Field(default="", max_length=500)


class Transcription(BaseModel):
    """画像の書き起こし（P4 の構造化出力）。**段階は含まない。**"""

    model_config = ConfigDict(extra="ignore")

    readable: bool = True
    fields: list[FieldText] = Field(default_factory=list)

    @property
    def transcribed(self) -> tuple[tuple[str, str], ...]:
        """**自己一貫性の照合先**（`sample_structured` の `key`）。

        段階ではなく書き起こしそのもので一致を見る ── この評価器は段階を
        答えないので、割れているかどうかはここにしか出ない。
        """
        return tuple(sorted((field.name, field.text.strip()) for field in self.fields))

    def text_of(self, name: str) -> str:
        for field in self.fields:
            if field.name == name:
                return field.text.strip()
        return ""


PROMPT = PromptTemplate(
    name="image_field_transcribe_ja",
    # 文面を変えたら必ず版を上げること（P8）。版が同じで文面が違うと、
    # 過去の採点が何で出たのか追えなくなる。
    #
    # ## 版 1 で何を狙っているか
    #
    # **採点させない**と最初に言い切る。訊くのは書き起こしだけである。
    # **推測で埋めさせない**と明示する ── 空欄を空欄と報告できることが、
    # この評価器の誤りを一方向に保つ条件そのものである（module docstring）。
    # 実測（47 件 × 3 本）では、空欄 5 件と氏名のみ 1 件の計 18 回すべてで
    # 正しく空・氏名のみと答え、学籍番号の捏造は 1 度も起きなかった。
    #
    # **どこを見るか（`where`）は課題が書く。** ここに認定証の話を書かない。
    version="1",
    system=(
        "あなたは大学の課題の採点補助です。"
        "画像に書かれている文字を、指定された場所ごとにそのまま書き写します。"
        "採点はしません。JSON オブジェクトのみを出力し、それ以外の文字は書きません。"
    ),
    template="""# 課題
{statement}

# 書き写す項目
{fields}

# 指示

添付した画像を見て、項目ごとに**そこに書かれている文字をそのまま書き写す**。

## 守ること
- **採点しない。** 良し悪しも段階も判断しない。
- **推測で埋めない。** その場所に何も書かれていなければ空文字にする。
  読み取れない項目も空文字にする。**もっともらしい値を作らない。**
- **一部だけを抜き出さない。** 指定された場所にある文字列を、
  英数字も記号も含めて端から端までそのまま写す。
- 画像そのものが読めない（課題と無関係・破損・真っ黒）のであれば
  `readable` を false にする。
- 上に挙げた項目だけを答える。増やさない。減らさない。

出力する JSON の形:
{{"readable": true, "fields": [{{"name": "項目の名前", "text": "書かれていた文字"}}]}}
""",
)


def fields_of(request: EvaluationRequest, criterion: RubricCriterion) -> tuple[FieldSpec, ...]:
    """この観点のために読む項目。

    **AI 評価器には `EvaluationRequest.test_cases` が渡らない**（パイプラインは
    決定的評価器にだけ渡す）。課題版そのものは渡るので、そこから自分あての
    ものを拾う ── `checklist_ai_judge` と同じ経路である。
    """
    specs = []
    for case in request.task_version.test_cases:
        if case.evaluator_id != EVALUATOR_ID:
            continue
        spec = _spec_of(case)
        if spec.criterion and spec.criterion != criterion.code:
            continue
        specs.append(spec)
    return tuple(specs)


def _spec_of(case: object) -> FieldSpec:
    payload = dict(getattr(case, "payload", {}) or {})
    return FieldSpec(
        name=str(getattr(case, "name", "")),
        where=str(payload.get("where") or ""),
        criterion=str(payload.get("criterion") or ""),
        pattern=str(payload.get("pattern") or ""),
        anywhere=bool(payload.get("anywhere") or False),
        expect=str(payload.get("expect") or ""),
        required=bool(payload.get("required") or False),
        weight=float(getattr(case, "weight", 1.0)),
    )


def describe_fields(specs: tuple[FieldSpec, ...]) -> str:
    return "\n".join(
        f"- {spec.name}: {spec.where}" if spec.where else f"- {spec.name}" for spec in specs
    )


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


def satisfies(
    spec: FieldSpec,
    transcription: Transcription,
    learner_reference: str | None,
) -> bool:
    """この項目は満たされたか。**ここに LLM は関与しない。**"""
    own = transcription.text_of(spec.name)

    if spec.expect == EXPECT_LEARNER_REFERENCE:
        # 学籍番号が分からなければ**満たしたことにしない。** 分からないまま
        # 通すと、誰の認定証でも通る。
        if not learner_reference:
            return False
        haystack_own = normalise(own)
        return any(
            candidate and candidate in haystack_own
            for candidate in reference_forms(learner_reference)
        )

    if not spec.pattern:
        return bool(own)

    haystack = [own]
    if spec.anywhere:
        haystack = [field.text for field in transcription.fields]
    return any(re.search(spec.pattern, text, re.IGNORECASE) for text in haystack)


def level_for(satisfied: float, total: float, criterion: RubricCriterion) -> int:
    """満たした項目の重みを、その観点が持つ段階に割り当てる。

    **数えるのはこちらの仕事である**（`checklist_ai_judge.level_for` と同じ
    理由でここにも置く ── 評価器どうしを import で結合させない、ADR 0007）。
    """
    levels = sorted(level.level for level in criterion.levels)
    if total <= 0 or satisfied >= total:
        return levels[-1]
    if satisfied <= 0:
        return levels[0]
    index = round(satisfied / total * (len(levels) - 1))
    return levels[max(0, min(index, len(levels) - 1))]


class ImageTextCheck:
    """画像を書き起こし、課題が宣言した条件と決定的に照合する。"""

    evaluator_id = EVALUATOR_ID
    kind = EvaluatorKind.AI
    # 読む項目を課題から受け取る。**画面はこの宣言で欄を出す。**
    uses_test_cases = True
    test_case_shape = "items"

    def __init__(
        self,
        gateway: LlmGateway | None = None,
        *,
        model: str | None = None,
        samples: int | None = None,
    ) -> None:
        # **画像を読むモデルは主系とは限らない。** 運用機の主系 `gemma4:e4b` は
        # vision を持たない（`default_vision_gateway` の説明を参照）。
        self._gateway = gateway or default_vision_gateway()
        self._model = model or default_vision_model()
        self._samples = samples if samples is not None else _samples_from_env()

    def evaluate(self, request: EvaluationRequest) -> EvaluationOutcome:
        criterion = request.criterion
        if criterion is None:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "this evaluator is called per criterion"},
            )
        # **指名された観点だけを見る**（`checklist_ai_judge` と同じ理由）。
        # 科目が AI 評価器を 2 つ宣言していると、指名の無い観点が両方に
        # 判定されて点が二重に付く。LLM を呼ぶ前に返す。
        if criterion.evaluator_id != EVALUATOR_ID:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "this criterion does not name image_text_check"},
            )

        specs = fields_of(request, criterion)
        if not specs:
            # 課題が何も宣言していない。**既定を持たない** ── どこを見るかは
            # 課題ごとに違い、当て推量の既定は静かに誤る。
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "the task declares no fields for this criterion"},
            )

        artifact_id, payload, skipped = self._image(request)
        if artifact_id is None or payload is None:
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": skipped},
            )

        samples = int(request.options.get("samples", self._samples))
        try:
            result = self._gateway.sample_structured(
                PROMPT,
                Transcription,
                model=self._model,
                # 提出物は個人に紐づく。ローカルプロバイダ以外へは流れない（P7）。
                data_class=DataClass.PERSONAL,
                samples=samples,
                key="transcribed",
                timeout_seconds=request.timeout_seconds,
                max_tokens=1200,
                images=(base64.b64encode(payload).decode(),),
                statement=request.task_version.statement,
                fields=describe_fields(specs),
            )
        except LlmError as exc:
            # LLM が使えなくても採点全体は落とさない（P2 / §04 step 2）。
            return EvaluationOutcome(
                status=EvaluatorStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )

        transcription = result.value
        if not transcription.readable:
            # **0 点にしない。** 読めないのは学習者の落ち度とは限らない。
            return EvaluationOutcome(
                status=EvaluatorStatus.SKIPPED,
                raw_output={"reason": "the model could not read the image"},
            )

        reference = request.learner_reference
        met = {spec.name: satisfies(spec, transcription, reference) for spec in specs}
        satisfied = sum(spec.weight for spec in specs if met[spec.name])
        total = sum(spec.weight for spec in specs)
        # **必須の項目が外れたら重みを数えない。** 比例配分では「0 にすべき
        # 提出」を表せない（`FieldSpec.required`）。
        unmet_required = [spec.name for spec in specs if spec.required and not met[spec.name]]
        if unmet_required:
            level = min(level.level for level in criterion.levels)
        else:
            level = level_for(satisfied, total, criterion)
        missing = [name for name, hit in met.items() if not hit]

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
                    # 書き起こしの一致度をそのまま確信度にする。割れたら人が見る（P5）。
                    confidence=result.agreement,
                    # **確定させない。** AI の判定は提案である（P5）。
                    conclusive=False,
                    evidence=self._to_evidence(request, artifact_id, specs, transcription),
                    rationale=self._rationale(met, transcription, missing, unmet_required),
                ),
            ),
            model_id=result.model_id,
            prompt_id=result.prompt_id,
            raw_output={
                "fields": {spec.name: transcription.text_of(spec.name) for spec in specs},
                "satisfied": met,
                "missing": missing,
                "unmet_required": unmet_required,
                "satisfied_weight": satisfied,
                "total_weight": total,
                "agreement": result.agreement,
                "samples": result.samples,
                "attempts": result.attempts,
                "provider": result.provider,
                "duration_ms": result.usage.duration_ms,
            },
        )

    # -- internals ---------------------------------------------------------

    def _image(self, request: EvaluationRequest) -> tuple[ArtifactId | None, bytes | None, str]:
        """判定する画像を選ぶ。

        **PDF は扱わない。** 描画手段を持たないので、PDF で出された提出は
        SKIP して人へ回す（module docstring）。ここで「読めなかった」と
        0 点にすると、形式の選択が減点になる。
        """
        saw_unsupported = ""
        for artifact in request.submission.gradable_artifacts:
            if artifact.kind not in READABLE_KINDS:
                saw_unsupported = saw_unsupported or f"cannot read a {artifact.kind} submission"
                continue
            content = request.artifact_contents.get(artifact.id)
            if content:
                return artifact.id, content, ""
        return None, None, saw_unsupported or "no image artifact to read"

    def _to_evidence(
        self,
        request: EvaluationRequest,
        artifact_id: ArtifactId,
        specs: tuple[FieldSpec, ...],
        transcription: Transcription,
    ) -> tuple[Evidence, ...]:
        """書き起こした文字列そのものを根拠にする（P4）。

        **範囲は画像全体にする。** 行も座標も持っていないので、持っている
        ふりをしない ── `LineSpan` を書けば画面はその行を指そうとする。
        """
        content_hash = next(
            (a.content_hash for a in request.submission.artifacts if a.id == artifact_id),
            "unknown",
        )
        return tuple(
            Evidence(
                artifact_id=artifact_id,
                artifact_content_hash=content_hash,
                span=WholeSpan(),
                quote=transcription.text_of(spec.name)[:500] or None,
                note=f"「{spec.name}」",
            )
            for spec in specs
        )

    def _rationale(
        self,
        met: dict[str, bool],
        transcription: Transcription,
        missing: list[str],
        unmet_required: list[str],
    ) -> str:
        parts = []
        for name, hit in met.items():
            text = transcription.text_of(name)
            shown = f"「{text}」" if text else "（空欄）"
            parts.append(f"{name}は{shown}で{'条件を満たします' if hit else '条件を満たしません'}")
        if unmet_required:
            parts.append(f"必須の項目を満たしていません: {'・'.join(unmet_required)}")
        elif missing:
            parts.append(f"満たしていない項目: {'・'.join(missing)}")
        return "。".join(parts) + "。"


def _samples_from_env() -> int:
    raw = os.environ.get(ENV_SAMPLES)
    if not raw:
        return DEFAULT_SAMPLES
    try:
        return max(1, int(raw))
    except ValueError:
        return DEFAULT_SAMPLES


def build() -> ImageTextCheck:
    """entry point から呼ばれるファクトリ。"""
    return ImageTextCheck()


__all__ = [
    "EVALUATOR_ID",
    "EXPECT_LEARNER_REFERENCE",
    "FieldSpec",
    "FieldText",
    "ImageTextCheck",
    "Transcription",
    "build",
    "fields_of",
    "level_for",
    "normalise",
    "reference_forms",
    "satisfies",
]
