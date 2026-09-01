"""確定の根拠を、教員が書いた要点から文章にする。

**理由そのものは作らせない。** 根拠欄を必須にしているのは、教員が実際に
下した判断を学習者に返すためである（ADR 0009 §4）。同じ文字列は一致度
（κ）の標本にも紐づく `HumanReview.comment` で、「教員がこの 1 件を読んだ」
という事実の中身にあたる。差分から理由を作文させると、教員が考えていない
理由が学習者に表示され、その記録が測定に混ざる。

**だからモデルには整形しかさせない。** 材料は教員が書いた要点で、そこに
無いことは足させない ── シラバスの読み取り（`syllabus.py`）が「書かれて
いないことを足さない」と指示しているのと同じ形である。

観点の題名だけは渡す。要点に「テスト 3」とだけ書かれていても、どの観点の
話かをモデルが取り違えないようにするためで、判断の中身ではない。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from aijudge_llm_gateway import (
    DataClass,
    LlmGateway,
    PromptTemplate,
    default_gateway,
    default_model,
)


class JustificationDraft(BaseModel):
    """モデルに返させる構造化出力（設計原則 P4）。"""

    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=1200)


PROMPT = PromptTemplate(
    name="justification_ja",
    # 文面を変えたら必ず版を上げる（P8）。
    version="1",
    system=(
        "あなたは大学教員が書いた採点の要点を、学習者が読む日本語の文章に"
        "整える助手です。**要点に書かれていないことを足しません。**"
        "理由を推測して補うことは、教員が考えていない理由を学習者に示すことに"
        "なります。断定できない箇所は要点のまま残してください。"
        "敬体で、2〜4 文にまとめます。"
    ),
    template=(
        "## 教員が書いた要点\n{points}\n\n"
        "## この提出で教員が段階を変えた観点\n{adjusted}\n\n"
        "上の要点を、学習者に向けた文章に整えてください。"
        "観点の名前は上の一覧の表記に合わせます。\n"
    ),
)


DRAFT_PROMPT = PromptTemplate(
    name="justification_draft_ja",
    # 文面を変えたら必ず版を上げる（P8）。
    version="1",
    system=(
        "あなたは大学教員が確定の根拠を書くための素案を用意する助手です。"
        "**渡された判定に書かれていないことを足しません。** 理由を推測して"
        "補うことは、教員が考えていない理由を学習者に示すことになります。"
        "点そのものの妥当性は論じません ── 決めるのは教員です。"
        "敬体で、2〜3 文にまとめます。"
    ),
    template=(
        "## この提出に対する観点ごとの判定\n{judgements}\n\n"
        "上の判定を、学習者に向けた確定の根拠の**素案**として整えてください。"
        "教員がこれを読んで直します。観点の名前は上の表記に合わせます。\n"
    ),
)


class JustificationWriter:
    """要点を文章にする。**採点そのものには関与しない。**"""

    def __init__(
        self,
        gateway: LlmGateway | None = None,
        *,
        model: str | None = None,
        max_tokens: int = 800,
    ) -> None:
        self._gateway = gateway or default_gateway()
        self._model = model or default_model()
        self._max_tokens = max_tokens

    def draft(self, judgements: str) -> str:
        """AI の判定から確定根拠の素案を作る（#97）。**保存の判断はしない。**

        材料は**その提出について AI が出した観点別の判定と根拠だけ**である。
        そこに無い理由を作文させると、教員が考えていない理由が学習者に出て、
        その記録が一致度の標本に混ざる（ADR 0009 §4）。
        """
        result = self._gateway.complete_structured(
            DRAFT_PROMPT,
            JustificationDraft,
            model=self._model,
            # 判定は学習者の提出についての記述なので個人データ（設計原則 P7）。
            data_class=DataClass.PERSONAL,
            max_tokens=self._max_tokens,
            judgements=judgements.strip()[:4000],
        )
        return result.value.text.strip()

    def polish(self, points: str, *, adjusted: tuple[str, ...] = ()) -> str:
        """要点を文章にして返す。**保存はしない。**

        返るのは候補で、教員が読んで直してから確定する。ここで保存すると、
        誰も読んでいない文章が根拠として残る（設計原則 P5）。
        """
        result = self._gateway.complete_structured(
            PROMPT,
            JustificationDraft,
            model=self._model,
            # 要点は学習者の提出についての記述なので個人データ。
            # ローカルのモデルにしか渡さない（設計原則 P7）。
            data_class=DataClass.PERSONAL,
            max_tokens=self._max_tokens,
            points=points.strip()[:2000],
            adjusted="\n".join(f"- {name}" for name in adjusted) or "（変更なし）",
        )
        return result.value.text.strip()


__all__ = ["PROMPT", "JustificationDraft", "JustificationWriter"]
