"""生成された課題の教員レビュー（S2、設計方針 §5）。

生成物は `IN_REVIEW` で溜まる。ここが承認・却下の導線で、**却下理由を必ず
記録する** ── 生成の品質を上げる材料は「何が落ちたか」ではなく「なぜ落ちたか」
の方にあり、Phase 4 の合格基準（教員承認率 ≥ 60%）も分母に却下が要る。

**承認率は採点の精度とは別の指標である。** `evals/gates.yaml` が持つのは
採点の一致度で、こちらは作問の質。同じレポートに混ぜない ── 混ぜると
「どちらが不合格なのか」が読めなくなる。
"""

from __future__ import annotations

from dataclasses import dataclass

from aijudge_authoring import TaskRepository
from aijudge_authoring.solvability import SolvabilityReport
from aijudge_authoring.verification import VerificationReport
from aijudge_core import ReviewState, TaskVersion
from aijudge_core.ids import TaskVersionId, UserId

# Phase 4 の合格基準（設計方針 §9.2）。**ここを緩めるなら ADR に書くこと。**
APPROVAL_RATE_GATE = 0.60

# これを下回る標本数では承認率を判定しない。
#
# `evals/gates.yaml` の `min_sample_size` と同じ考え方（ADR 0005）── 3 件中
# 2 件が承認されても、それは 67% の証拠ではない。
MIN_SAMPLE_SIZE = 30


@dataclass(frozen=True)
class ApprovalRate:
    """生成した課題のうち、教員が承認した割合。

    **判定は 3 値。** 標本が足りなければ「測れていない」であって合格ではない
    （ADR 0005 が採点の一致度について定めたのと同じ規則を、作問にも当てる）。
    """

    approved: int
    rejected: int
    pending: int
    min_sample_size: int = MIN_SAMPLE_SIZE
    gate: float = APPROVAL_RATE_GATE

    @property
    def decided(self) -> int:
        """判定の済んだ数。**保留は分母に入れない** ── まだ落ちていない。"""
        return self.approved + self.rejected

    @property
    def rate(self) -> float | None:
        return None if self.decided == 0 else self.approved / self.decided

    @property
    def verdict(self) -> str:
        if self.decided < self.min_sample_size:
            return "NOT_MEASURED"
        rate = self.rate
        return "PASS" if rate is not None and rate >= self.gate else "FAIL"

    def render(self) -> str:
        rate = self.rate
        shown = "—" if rate is None else f"{rate:.0%}"
        lines = [
            f"承認率 {shown}（承認 {self.approved} / 却下 {self.rejected}"
            f" / レビュー待ち {self.pending}）",
            f"判定 {self.verdict}（基準 {self.gate:.0%}、最小標本 {self.min_sample_size}）",
        ]
        if self.verdict == "NOT_MEASURED":
            lines.append(
                f"  判定の済んだ課題が {self.decided} 件しかありません。"
                "**測れていないことは合格ではありません。**"
            )
        return "\n".join(lines)


def pending_reviews(repository: TaskRepository) -> tuple[TaskVersion, ...]:
    return repository.list_versions_in_review()


def approve(
    repository: TaskRepository, version_id: TaskVersionId, *, reviewer: UserId
) -> TaskVersion:
    return repository.record_review(
        version_id, approved=True, reviewer=reviewer, reason=None
    )


def reject(
    repository: TaskRepository, version_id: TaskVersionId, *, reviewer: UserId, reason: str
) -> TaskVersion:
    """却下する。**理由は必須**（コア側でも検証している）。

    理由は作問の改善に還流させる材料で、捨てると生成が同じ誤りを繰り返す。
    """
    return repository.record_review(
        version_id, approved=False, reviewer=reviewer, reason=reason
    )


def approval_rate(versions: tuple[TaskVersion, ...]) -> ApprovalRate:
    """生成物だけを数える。

    **手で書いた課題を分母に入れない。** 教員が自分で書いた課題は当然
    承認されるので、混ぜると承認率がいくらでも高く出る。
    """
    generated = [v for v in versions if v.provenance.generated_by is not None]
    return ApprovalRate(
        approved=sum(
            1 for v in generated if v.provenance.review_state is ReviewState.APPROVED
        ),
        rejected=sum(
            1 for v in generated if v.provenance.review_state is ReviewState.REJECTED
        ),
        pending=sum(
            1 for v in generated if v.provenance.review_state is ReviewState.IN_REVIEW
        ),
    )


@dataclass(frozen=True)
class ReviewPacket:
    """教員が 1 件を判断するために読むもの一式。

    **どれも自動で承認も却下もしない**（設計原則 P5）。門と解答可能性は
    材料であって判定ではない ── 特に解答可能性は、落ちた原因が「課題文が
    曖昧」なのか「単に難しい」なのかを機械が分けられない。自動で捨てると
    **難しい良問から先に消える。**
    """

    version: TaskVersion
    verification: VerificationReport
    solvability: SolvabilityReport | None = None
    # この課題版が問うとされている知識要素の**正準キー**。
    #
    # **人が `Blueprint` に書いたものである。** モデルは決めない
    # （`TaskDraft` に KC の欄が無い）── Q-matrix は S7 の習熟度が全面的に
    # 依存する対応表なので、生成の揺れを流し込まない。
    #
    # `TaskVersion.q_matrix` が持つのはキーから導出した ID（`kc_9f3a…`）で、
    # 教員には読めない。**呼び出し側が可読なキーに直して渡す** ── 作問直後は
    # Blueprint から、あとからのレビューでは KC の保存先から引く。
    declared_kcs: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        """機械が見た範囲で気になる点が無いか。**承認の意味ではない。**"""
        return (
            self.verification.usable
            and (self.solvability is None or self.solvability.solved)
            and (self.solvability is None or not self.solvability.unexercised_kcs)
        )

    def render(self) -> str:
        lines = [
            f"課題版 {self.version.id}",
            f"  出所: {self.version.provenance.generated_by or '教員が作成'}"
            f"（{self.version.provenance.generation_prompt_version or '—'}）",
            "",
            self._knowledge_section(),
            "",
            gate_advice(self.verification),
        ]
        if self.solvability is not None:
            lines += ["", self.solvability.summary()]
            note = self.solvability.kc_note()
            if note:
                lines += ["", note]
        lines += ["", "**承認するかどうかは教員が決めます。**"]
        return "\n".join(lines)

    def _knowledge_section(self) -> str:
        """知識要素を必ず出す。

        **門も解答可能性も KC を見ていない。** 見ているのは「参照解答が
        通るか」「変異が落ちるか」「別のモデルが解けるか」だけで、
        **宣言した KC をこの課題が実際に問うているかは誰も検証していない。**
        教員に見せなければ、誰も気づかないまま Q-matrix に残る ── そして
        学習者には、問われていない KC の習熟度が付く。
        """
        declared = self.declared_kcs
        if not declared:
            if self.version.q_matrix:
                return (
                    f"知識要素: {len(self.version.q_matrix)} 件（キーが渡されていません）\n"
                    "  **表示できていません。** 呼び出し側が正準キーを渡していない"
                    "ので、\n  この課題が何を問うことになっているか教員に示せていません。"
                )
            return "知識要素: 登録なし（この課題では習熟度が付きません）"
        listed = "\n".join(f"  - {kc}" for kc in declared)
        return (
            "知識要素（**課題文がこれを問うているか確認してください**）:\n"
            + listed
            + "\n  機械はここを検証していません。"
        )


def build_packet(
    version: TaskVersion,
    verification: VerificationReport,
    solvability: SolvabilityReport | None = None,
    *,
    declared_kcs: tuple[str, ...] = (),
) -> ReviewPacket:
    """レビュー前に走らせた検査を束ねる（設計方針 §5 の並び）。

        生成 → 門 1・門 2 → 解答可能性 → **教員レビュー**

    解答可能性を門の後ろに置くのは、門 1 が落ちる課題（参照解答が自分の
    テストケースを通らない）を別のモデルに解かせても意味が無いからである。
    """
    return ReviewPacket(
        version=version,
        verification=verification,
        solvability=solvability,
        # 解答役に確認させた KC があればそちらを使う（同じ並びのはず）。
        declared_kcs=declared_kcs or (solvability.declared_kcs if solvability else ()),
    )


def gate_advice(report: VerificationReport) -> str:
    """門の結果を、教員がレビューで読む文にする。

    **門が落とした課題も教員に見せる。** 自動で捨てると、門が厳しすぎる
    ことに誰も気づけない（生き残った変異は課題の欠陥とは限らない）。
    """
    if report.usable:
        return "門 2 つを通っています。内容を確認してください。"
    return "**門を通っていません。** 承認する前に下の指摘を確かめてください。\n" + report.summary()
