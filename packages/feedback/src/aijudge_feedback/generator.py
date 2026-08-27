"""フィードバック生成（設計方針 §04 step 6）。

**点数の説明ではなく「次の一手」。** 「テストケース 3 で落ちました、0 点です」
は結果の反復であって助けにならない。「n <= 0 のときに再入力を求める処理が
無い」が次の一手である。

材料の制約が 1 つある。**確定していない AI の判定を材料にしない。** AI の
ルーブリック判定は教員が確定させるまで学習者に見せない（設計原則 P5、
studentweb の `visibility.py`）。それを材料にしたフィードバックを即座に返せば、
見せないことにした判断が文章の形で漏れる。

したがって即座に返すフィードバックは**決定的評価の結果だけ**から作る。
プログラミング課題ではそれが行動可能な部分の中心でもある（どのテストが
落ちたか）。ルーブリック判定に基づく助言は、確定後に根拠と一緒に見える。

S6 が停止していてもフィードバックは返す。LLM を使わない要約に落ちる
（設計原則 P2）。無言で何も返さないと、学習者にはシステムが壊れたのか
自分の提出に問題が無いのか区別できない。
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ConfigDict, Field

from aijudge_core import (
    CriterionScore,
    EvaluatorKind,
    GradingRun,
    TaskVersion,
)
from aijudge_llm_gateway import (
    DataClass,
    LlmError,
    LlmGateway,
    PromptTemplate,
)

logger = logging.getLogger(__name__)

# プロンプトの版。文面を直したら必ず上げる（P8）。
FEEDBACK_PROMPT = PromptTemplate(
    name="next_step_feedback",
    version="1",
    system=(
        "あなたはプログラミング演習の TA です。学習者に次の一手を伝えます。\n"
        "規則:\n"
        "- 点数や合否に言及しない。採点結果の説明は別に表示されている。\n"
        "- 修正すべき箇所と、次に何を試すかを具体的に書く。\n"
        "- 解答そのもの（修正後のコード）は書かない。学習者が自分で直せる"
        "手がかりに留める。\n"
        "- 日本語で、2〜4 文。"
    ),
    template=(
        "課題:\n{statement}\n\n"
        "自動テストの結果:\n{findings}\n\n"
        "学習者の提出:\n```\n{source}\n```\n\n"
        "次の一手を書いてください。"
    ),
)

# 提出コードをプロンプトに載せる上限。長い提出を丸ごと送ると
# コンテキストを食い潰し、肝心のテスト結果が押し出される。
MAX_SOURCE_CHARS = 4000
MAX_FEEDBACK_CHARS = 1200


class NextStep(BaseModel):
    """LLM に返させる形。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    message: str = Field(min_length=1)


class FeedbackResult(BaseModel):
    """生成結果と出所。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    message: str = Field(min_length=1)
    model_id: str | None = None
    prompt_id: str | None = None
    # LLM を使わずに組み立てたか（S6 停止時の劣化動作）。
    fallback: bool = False


def releasable_scores(run: GradingRun) -> tuple[CriterionScore, ...]:
    """確定を待たずに材料にしてよい採点結果。

    決定的評価のものだけ。AI の判定は教員が確定させるまで学習者に
    見せないので、材料にもしない。
    """
    return tuple(
        score for score in run.criterion_scores if score.kind is EvaluatorKind.DETERMINISTIC
    )


def summarize_findings(run: GradingRun, task_version: TaskVersion) -> tuple[str, ...]:
    """決定的評価が何を見つけたかを箇条書きにする。

    これがフィードバックの材料であり、LLM が落ちたときの本文でもある。
    """
    codes = {criterion.id: criterion for criterion in task_version.criteria}
    lines: list[str] = []
    for score in releasable_scores(run):
        criterion = codes.get(score.criterion_id)
        title = criterion.title if criterion is not None else str(score.criterion_id)
        lines.append(f"- {title}: {score.rationale}")
    if run.unscored_criteria:
        lines.append("- 一部の観点は自動採点できませんでした（担当教員が確認します）。")
    return tuple(lines)


class FeedbackGenerator:
    """次の一手を作る。"""

    def __init__(
        self,
        gateway: LlmGateway | None = None,
        *,
        model: str = "",
        max_source_chars: int = MAX_SOURCE_CHARS,
    ) -> None:
        self._gateway = gateway
        self._model = model
        self._max_source_chars = max_source_chars

    def generate(
        self, run: GradingRun, task_version: TaskVersion, source: str
    ) -> FeedbackResult | None:
        """フィードバックを作る。材料が無ければ None。

        例外は投げない。フィードバックが出ないことは採点の失敗ではない。
        """
        findings = summarize_findings(run, task_version)
        if not findings:
            return None

        if self._gateway is None or not self._model:
            return _fallback(findings)

        try:
            result = self._gateway.complete_structured(
                FEEDBACK_PROMPT,
                NextStep,
                model=self._model,
                # 学習者の提出コードを載せるので PERSONAL。
                # ゲートウェイが学外プロバイダへの送信を拒否する（P7）。
                data_class=DataClass.PERSONAL,
                statement=task_version.statement,
                findings="\n".join(findings),
                source=_clip(source, self._max_source_chars),
            )
        except LlmError:
            # S6 が停止・過負荷・応答不正。無言にせず要約を返す（P2）。
            logger.warning("feedback generation fell back to the summary", exc_info=True)
            return _fallback(findings)

        message = result.value.message.strip()
        if not message:
            return _fallback(findings)
        return FeedbackResult(
            message=_clip(message, MAX_FEEDBACK_CHARS),
            model_id=result.model_id,
            prompt_id=result.prompt_id,
        )


def _fallback(findings: tuple[str, ...]) -> FeedbackResult:
    """LLM を使わない本文。

    材料をそのまま並べる。「次の一手」としては弱いが、無言よりましである
    （無言だと、システムが壊れたのか提出に問題が無いのか区別できない）。
    """
    return FeedbackResult(
        message="自動テストの結果は次のとおりです。\n" + "\n".join(findings),
        fallback=True,
    )


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（以下省略）"
