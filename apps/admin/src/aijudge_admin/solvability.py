"""問題文だけを別のモデルに渡して解かせる（S2、設計方針 §5）。

教員レビューの**前**に走らせ、承認・却下の判断材料にする。語彙と読み方は
`aijudge_authoring.solvability`、モデルを呼ぶのはここ（S2 と S6 は互いを
import しない）。

**下書きを作ったモデルとは別のモデルを使う。** 同じモデルが自分の作った
課題を解けても、課題文だけで解答に至れる証拠にはならない ── 同じ癖で
書いて同じ癖で読むだけである。

**参照解答をプロンプトに入れない。** 入れたら何も測っていない。
"""

from __future__ import annotations

from aijudge_authoring.solvability import (
    SolvabilityOutcome,
    SolvabilityReport,
    SolverAttempt,
)
from aijudge_core import TaskVersion
from aijudge_llm_gateway import (
    DataClass,
    LlmError,
    LlmGateway,
    PromptTemplate,
    default_gateway,
    default_model,
)

from .task_verifier import TaskVerifier

PROMPT = PromptTemplate(
    name="task_solve_ja",
    # 文面を変えたら必ず版を上げる（P8）。
    version="1",
    system=(
        "あなたは課題を解く学生です。"
        "**課題文に書かれていることだけから解答してください。**"
        "書かれていない仕様を推測して補わないでください ── "
        "補うと、課題文の曖昧さが見えなくなります。"
    ),
    template=(
        "次の課題を {language} で解いてください。\n\n"
        "## 課題文\n{statement}\n\n"
        "まず課題を何をするものと読んだかを `understanding` に短く書き、"
        "そのうえで `solution` に完全なプログラムを書いてください。\n\n"
        "## 知識要素の確認\n"
        "この課題は次の知識要素を問うものとして登録されています。\n"
        "{knowledge_components}\n"
        "**あなたの解答に実際に必要だったものだけ**を `exercised` に"
        "そのままの表記で挙げてください。要らなかったものは挙げないでください。\n"
    ),
)


class SolvabilityChecker:
    def __init__(
        self,
        verifier: TaskVerifier,
        gateway: LlmGateway | None = None,
        *,
        solver_model: str | None = None,
        language: str = "c",
        max_tokens: int = 4096,
    ) -> None:
        self._verifier = verifier
        self._gateway = gateway or default_gateway()
        self._model = solver_model or default_model()
        self._language = language
        self._max_tokens = max_tokens

    def check(
        self, task_version: TaskVersion, *, declared_kcs: tuple[str, ...] = ()
    ) -> SolvabilityReport:
        """課題文だけを渡して解かせる。

        `declared_kcs` は Blueprint が宣言した知識要素の正準キー。
        **解答役に候補として示し、実際に要ったものを選ばせる** ── Q-matrix が
        課題の中身と食い違っていないかを、教員が見る材料にするため。
        渡さなければ確認しない（従来どおり解けたかどうかだけを見る）。
        """
        if not task_version.test_cases:
            # 通ったかどうかを判定する手段が無い。**合格にしない。**
            return SolvabilityReport(
                outcome=SolvabilityOutcome.NOT_RUN,
                solver_model=self._model,
                declared_kcs=declared_kcs,
                detail="テストケースが無いので、解けたかどうかを判定できません",
            )

        try:
            result = self._gateway.complete_structured(
                PROMPT,
                SolverAttempt,
                model=self._model,
                # 課題文だけを渡す。学習者のデータは含まない（P7）。
                data_class=DataClass.NON_PERSONAL,
                max_tokens=self._max_tokens,
                statement=task_version.statement,
                language=self._language,
                knowledge_components=(
                    "\n".join(f"- {kc}" for kc in declared_kcs) or "（登録なし）"
                ),
            )
        except LlmError as exc:
            # **モデルが答えられなかったことを「解けない課題」にしない。**
            # 落ちたのは課題ではなく検査の側である。
            return SolvabilityReport(
                outcome=SolvabilityOutcome.NOT_RUN,
                solver_model=self._model,
                declared_kcs=declared_kcs,
                detail=f"解答役のモデルが応答しませんでした: {exc}",
            )

        attempt = result.value
        # **参照解答と突き合わせない。** 正しいプログラムは何通りもある。
        # 見るのは振る舞いで、判定は門 1 と同じ経路を通す。
        passed, detail = self._verifier.passes(task_version, attempt.solution)
        return SolvabilityReport(
            outcome=SolvabilityOutcome.SOLVED if passed else SolvabilityOutcome.UNSOLVED,
            solver_model=self._model,
            understanding=attempt.understanding,
            declared_kcs=declared_kcs,
            # **示した候補の外は捨てる。** 似て非なる名前が混じると、
            # 宣言との突き合わせが名前の揺れを測ることになる。
            exercised_kcs=tuple(kc for kc in attempt.exercised if kc in set(declared_kcs)),
            detail="" if passed else detail,
        )
