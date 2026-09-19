"""提出された画像に写っている文字を、ビジョンモデルで書き起こす抽出器。

`document_text`（PDF / DOCX → 本文）と**同じ契約に載る**。対象が違うだけで
仕事は同じで、違いは手段が模型であることだけである（`aijudge_core.extraction`）。

## 書き起こすだけで、採点はしない

訊くのは「画像に何と書いてあるか」だけである。「この認定証は合格か」は訊かない。
段階を決めるのは、この本文に対して働く**決定的な評価器**の側である（P3・P4）。

    ここ       画像 → 「認定証 / Python体験編1… / Y240040naka / …」
    評価器     `y240040` in その行 → 満たす

**どこを読むかを指定しない。** 見えている文字を上から順に全部写させる。
狙い撃ちにすると精度が上がると考えていたが、実測（2026-09-19・47 件）では
逆だった ── 「受講者名を出せ」のように**意味で抜き出させる**と、モデルは
`Y230020　岡本秀和` から学籍番号を落として氏名だけを返した（5 件）。
「見えるものを全部写せ」にはその誘導が無く、5 件とも学籍番号ごと残った。

全文にすると出力が画面の中身に比例するが、そこは Gateway の打ち切り検出が
受け止める（ADR 0021）。

## 空欄を埋めない

**推測で埋めさせないことが、この抽出器のいちばん大事な性質**である。
実データには受講者欄が空の提出が 5 件あり、そこに学籍番号を捏造されると
0 点であるべき提出が満点になる。138 回の書き起こしで 1 度も起きなかった。

`blank_lines` のような「空欄を数え上げさせる」項目を足してはならない。
余白の多い画面（ブラウザ枠ごと撮った提出）で、空文字を延々と吐き続ける
縮退ループに入ることを実測している（ADR 0021）。
"""

from __future__ import annotations

import base64
import logging

from pydantic import BaseModel, ConfigDict, Field

from aijudge_core import Artifact, ArtifactKind, Extraction
from aijudge_llm_gateway import (
    DataClass,
    LlmError,
    LlmGateway,
    PromptTemplate,
    default_vision_gateway,
    default_vision_model,
)

logger = logging.getLogger(__name__)

EXTRACTOR_ID = "image_text"

#: 扱える種類。**ollama はこの形式をそのまま受ける**ので、変換の依存を持たない。
#: PDF は扱わない ── 描画には PyMuPDF が要り、AGPL なのでこの repository には
#: 入れられない（Apache-2.0）。PDF は `document_text` の担当である。
READABLE_KINDS = frozenset({ArtifactKind.IMAGE})

#: 出力の上限。**ローカルモデルなので費用はかからない**（ADR 0021 の実測）。
#: 上限は目標ではないので、正常に終わる呼び出しの時間はこの値に依存しない。
#: 効くのは縮退ループに入ったときだけで、35〜40 tok/s ＝ 最悪 108 秒。
MAX_TOKENS = 4000


class Transcription(BaseModel):
    """書き起こし（P4 の構造化出力）。**段階も判定も含まない。**"""

    model_config = ConfigDict(extra="ignore")

    readable: bool = True
    # **既定を空にする。** 「書いていない」を空で表せないと、モデルは空欄を
    # 埋めにかかる。
    lines: list[str] = Field(default_factory=list)


PROMPT = PromptTemplate(
    name="image_transcribe_ja",
    # 文面を変えたら必ず版を上げること（P8）。版が同じで文面が違うと、
    # 過去の採点が何で出たのか追えなくなる。
    #
    # ## 版 1 で何を狙い、何を狙わないか
    #
    # **採点させない**と最初に言い切る。**意味で抜き出させない** ── 項目名を
    # 挙げて「そこを出せ」と言うと、モデルは解釈して一部だけを返す（実測で
    # 学籍番号が落ちた）。**空行を数え上げさせない** ── 縮退ループの引き金。
    version="1",
    system=(
        "あなたは大学の課題の採点補助です。"
        "画像に書かれている文字を、見えるとおりに書き写します。"
        "採点はしません。JSON オブジェクトのみを出力し、それ以外の文字は書きません。"
    ),
    template="""添付は学生が提出した課題の画像である。

**採点はしない。画像に書かれている文字を、見えるとおりに書き写すだけでよい。**

## 守ること
- 上から下へ、見えている順に 1 行ずつ写す。
- **一字も落とさない。** 英数字も記号も、書かれているとおりに写す。
- **推測で足さない。** 書かれていないものを書かない。**もっともらしい値を
  作らない。**
- **文字が書かれている行だけ**を写す。空白しかない場所は飛ばす。
  同じ行を繰り返さない。
- 画像そのものが読めない（破損・真っ黒）なら `readable` を false にする。

出力する JSON の形:
{{"readable": true, "lines": ["1 行目", "2 行目"]}}
""",
)


class ImageText:
    """画像を本文に直す抽出器。"""

    extractor_id = EXTRACTOR_ID

    def __init__(
        self,
        gateway: LlmGateway | None = None,
        *,
        model: str | None = None,
    ) -> None:
        # **画像を読むモデルは主系とは限らない。** 運用機の主系 `gemma4:e4b` は
        # vision を持たない（`default_vision_gateway` の説明を参照）。
        self._gateway = gateway or default_vision_gateway()
        self._model = model or default_vision_model()

    def applies_to(self, kind: ArtifactKind) -> bool:
        return kind in READABLE_KINDS

    def extract(self, artifact: Artifact, payload: bytes) -> Extraction:
        if not payload:
            return self._failed("画像の中身がありません")
        try:
            result = self._gateway.complete_structured(
                PROMPT,
                Transcription,
                model=self._model,
                # 提出物は個人に紐づく。ローカルプロバイダ以外へは流れない（P7）。
                data_class=DataClass.PERSONAL,
                max_tokens=MAX_TOKENS,
                images=(base64.b64encode(payload).decode(),),
            )
        except LlmError as exc:
            # **例外にしない。** 1 件の読めない画像で受付を止めない。
            logger.warning("could not transcribe %s: %s", artifact.id, exc)
            return self._failed(f"{type(exc).__name__}: {exc}")

        transcription = result.value
        if not transcription.readable:
            return self._failed("画像を読み取れませんでした")
        text = "\n".join(line.strip() for line in transcription.lines if line.strip())
        if not text:
            # **空を「白紙」と読ませない**（`document_text` と同じ判断）。
            return self._failed("画像から文字を読み取れませんでした")
        return Extraction(
            text=text.encode("utf-8"),
            engine=self.extractor_id,
            model_id=result.model_id,
            prompt_version=result.prompt_id,
        )

    def _failed(self, reason: str) -> Extraction:
        return Extraction(engine=self.extractor_id, failed_reason=reason)


def build() -> ImageText:
    """entry point から呼ばれるファクトリ。"""
    return ImageText()


__all__ = ["EXTRACTOR_ID", "MAX_TOKENS", "ImageText", "Transcription", "build"]
