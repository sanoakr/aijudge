"""提出物から、採点が読む本文を取り出す契約（設計方針 §4 step 1）。

## なぜ 1 つの契約なのか

PDF から本文を抜くのも、画像から文字を読むのも、**やっていることは同じ**
── 学習者が出したものを、評価器が読める本文に直す。違うのは入力の種類と、
手段が決定的か模型かだけである。

    document_text   PDF / DOCX → 本文（pypdf。決定的。出所は無い）
    image_text      画像       → 本文（VLM。出所が要る）

分けて持つと、同じ仕事が 2 か所に住む。実際そうなっていた ── 片方は変換層
（`Normalizer`）、片方は採点層（画像を読んで点まで付ける評価器）にいて、
**画像から取り出した本文を既存の評価器が読めなかった。** PDF なら読めるのに。

## 出所を返せること（P8）

旧 `Normalizer` は `bytes` を返すだけで、モデル名もプロンプト版も返せなかった。
決定的な変換ではそれで足りるが、模型を使う抽出では「どのモデルのどの版で
起こした本文を採点したか」が残らない。`Extraction` がそれを運び、
`TranscriptionMeta` に写されて提出物に残る。

## どこに置かれるか

**契約は core にある。** 抽出を走らせるのは受付（S3）で、結果を読むのは
採点（S5）── 2 つのサブシステムが同じ型を見る必要があり、サブシステムどうし
は直接 import しない（ADR 0001）。実装の登録簿は採点側が持ち、実装を選んで
注入するのは合成ルートである。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:  # pragma: no cover - 型のためだけの import
    # **実行時に import しない。** `grading` が `extraction` を、`extraction` が
    # `submission` を、`submission` が…… と辿ると循環する。契約が要るのは型
    # だけなので、ここで切る。
    from .submission import Artifact, ArtifactKind


class Extraction(BaseModel):
    """取り出した本文と、その出所。

    **失敗は例外にしない。** 1 件の壊れた PDF で全員の受付を止めない
    （旧 `Normalizer` から引き継ぐ規則）。取り出せなければ `failed_reason`
    を入れて返し、下流が「読めない」と判定する ── **0 点にはしない。**
    読めないのは学習者の落ち度とは限らない。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: どの原本から取り出したか。**記録の側で埋める** ── 抽出器は自分が
    #: 渡された 1 件しか知らないので、対応づけは呼び出し側の仕事である。
    artifact_id: str = ""
    text: bytes = b""
    #: どの実装が起こしたか（`extractor_id`）。記録に残す。
    engine: str = Field(default="", min_length=0)
    #: 模型を使ったときだけ入る。決定的な抽出では空（P8）。
    model_id: str | None = None
    prompt_version: str | None = None
    #: 領域キー → 確信度。低確信度の箇所を画面が示すのに使う。
    confidence_map: dict[str, float] = Field(default_factory=dict)
    #: 取り出せなかった理由。入っていれば `text` は使わない。
    failed_reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.failed_reason is None and bool(self.text)


@runtime_checkable
class Extractor(Protocol):
    """本文を取り出すプラグイン。

    entry point のグループは `aijudge.extractors`。科目プロファイルの
    `input.transcription` が名前で指名する（評価器と同じ仕組み、ADR 0002）。

    **種類で選ぶ。** 1 つの課題に PDF と画像が混ざることは実際にある
    （レポート課題の実データ 19 件のうち 2 件が DOCX だった）ので、
    どれを扱えるかは実装が名乗る。
    """

    extractor_id: str

    def applies_to(self, kind: ArtifactKind) -> bool:
        """この種類の提出物を扱うか。"""
        ...

    def extract(self, artifact: Artifact, payload: bytes) -> Extraction:
        """本文を取り出す。取り出せなければ `failed_reason` を入れて返す。"""
        ...


__all__ = ["Extraction", "Extractor"]
