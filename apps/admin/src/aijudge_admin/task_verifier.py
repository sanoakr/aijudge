"""生成された課題を 2 つの門にかける（S2、ADR 0008 / 設計方針 §5）。

**実際に走らせるのはここだけ。** 変異を作る規則は `aijudge_authoring` に
あり、実行環境を知らない。束ねてよいのは app 層だけである（ADR 0001）。

門は**採点と同じ経路で走る。** 別に検査用の実行系を書くと、門を通ったのに
採点では落ちる課題ができる。評価器レジストリと科目プロファイルをそのまま
使うので、門が確かめているのは「この課題がこの科目で採点されたときどうなるか」
そのものである。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from aijudge_authoring import (
    DEFAULT_MIN_KILL_RATIO,
    GateOutcome,
    Mutation,
    MutationOutcome,
    VerificationReport,
    mutate,
)
from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    EvaluatorStatus,
    Submission,
    SubmissionState,
    TaskVersion,
    new_id,
)
from aijudge_core.ids import ArtifactId, SubmissionId, UserId
from aijudge_grading import EvaluationRequest, EvaluatorRegistry, SubjectProfile

# 門にかける仮の提出が使う ID。**実在の学習者ではない。**
_VERIFICATION_USER = "usr_" + "0" * 32

logger = logging.getLogger(__name__)

# 変異 1 つにつきサンドボックスの実行が要る。既定を小さく取るのは、
# 作問のたびに数分待たされると誰も門を通さなくなるからである。
DEFAULT_MUTATION_LIMIT = 12


class TaskVerifier:
    def __init__(
        self,
        registry: EvaluatorRegistry,
        profile: SubjectProfile,
        *,
        mutation_limit: int = DEFAULT_MUTATION_LIMIT,
        min_kill_ratio: float = DEFAULT_MIN_KILL_RATIO,
    ) -> None:
        self._registry = registry
        self._profile = profile
        self._mutation_limit = mutation_limit
        self._min_kill_ratio = min_kill_ratio

    def verify(self, task_version: TaskVersion) -> VerificationReport:
        reference = task_version.reference_solution
        if not reference or not task_version.test_cases:
            # 参照解答もテストケースも無ければ門は何も確かめられない。
            # **合格にしない**（`NOT_RUN` は合格ではない）。
            return VerificationReport(
                reference_passes=GateOutcome.NOT_RUN,
                reference_detail="参照解答かテストケースが無いので検査していません",
                min_kill_ratio=self._min_kill_ratio,
            )

        passed, detail = self.passes(task_version, reference)
        if not passed:
            # **門 1 で落ちたら門 2 は走らせない。** 参照解答が通らない課題で
            # 変異を測っても、測っているのは参照解答の誤りである。
            return VerificationReport(
                reference_passes=GateOutcome.FAILED,
                reference_detail=detail,
                min_kill_ratio=self._min_kill_ratio,
            )

        killed = 0
        viable = 0
        not_viable = 0
        survivors: list[Mutation] = []
        for mutation in mutate(reference, limit=self._mutation_limit):
            outcome = self._outcome(task_version, mutation)
            if outcome is MutationOutcome.NOT_VIABLE:
                not_viable += 1
                continue
            viable += 1
            if outcome is MutationOutcome.KILLED:
                killed += 1
            else:
                survivors.append(mutation)

        return VerificationReport(
            reference_passes=GateOutcome.PASSED,
            reference_detail=detail,
            mutants_total=viable,
            mutants_killed=killed,
            mutants_not_viable=not_viable,
            survivors=tuple(survivors),
            min_kill_ratio=self._min_kill_ratio,
        )

    # -- internals ---------------------------------------------------------

    def _outcome(self, task_version: TaskVersion, mutation: Mutation) -> MutationOutcome:
        passed, detail = self.passes(task_version, mutation.source)
        if passed:
            return MutationOutcome.SURVIVED
        if _is_build_failure(detail):
            # **コンパイルできない変異は証拠にならない**（`MutationOutcome`）。
            return MutationOutcome.NOT_VIABLE
        return MutationOutcome.KILLED

    def passes(self, task_version: TaskVersion, source: str) -> tuple[bool, str]:
        """決定的評価器に通して、満点かどうかを見る。

        **公開しているのは解答可能性の検査が使うためである**
        （`solvability.py`）。別に実行系を書くと、門が通したものを
        解答可能性が落とす、といった食い違いが実行系の違いから生まれる。

        AI 評価器は呼ばない。**門は決定的でなければならない** ── 同じ課題が
        走らせるたびに通ったり落ちたりすると、通ったこと自体が意味を失う。

        リクエストの組み立ては採点パイプラインと同じにする。違えると、門が
        確かめているのは採点とは別のものになる。
        """
        submission, artifact_id = _candidate(task_version)
        for evaluator_id in self._profile.deterministic:
            evaluator = self._registry.get(evaluator_id)
            if evaluator is None:
                continue
            cases = tuple(
                case for case in task_version.test_cases if case.evaluator_id == evaluator_id
            )
            if not cases:
                continue
            outcome = evaluator.evaluate(
                EvaluationRequest(
                    task_version=task_version,
                    submission=submission,
                    artifact_contents={artifact_id: source.encode()},
                    test_cases=cases,
                    timeout_seconds=self._profile.timeout_seconds,
                    options=self._profile.evaluator_options.get(evaluator_id, {}),
                )
            )
            if outcome.status is EvaluatorStatus.FAILED:
                return False, str(outcome.raw_output)[:400]
            for score in outcome.scores:
                if score.score_ratio < 1.0:
                    return False, (score.rationale or "満点ではありません")[:400]
        return True, ""


def _candidate(task_version: TaskVersion) -> tuple[Submission, ArtifactId]:
    """門にかけるための仮の提出。

    **保存しない。** 参照解答と変異は学習者の提出ではないので、提出の表にも
    採点結果の表にも残さない。評価器が `Submission` を要求するので形だけ作る。
    """
    artifact_id = ArtifactId(new_id("art"))
    submission_id = SubmissionId(new_id("sub"))
    now = datetime.now(UTC)
    return (
        Submission(
            id=submission_id,
            task_version_id=task_version.id,
            learner_id=UserId(_VERIFICATION_USER),
            attempt=1,
            state=SubmissionState.SUBMITTED,
            artifacts=(
                Artifact(
                    id=artifact_id,
                    submission_id=submission_id,
                    role=ArtifactRole.ORIGINAL,
                    kind=ArtifactKind.CODE,
                    storage_key="verification",
                    content_hash="sha256:verification",
                    byte_size=1,
                    filename="candidate",
                    created_at=now,
                ),
            ),
            created_at=now,
            submitted_at=now,
        ),
        artifact_id,
    )


def _is_build_failure(detail: str) -> bool:
    """落ちた理由がコンパイル失敗か。

    字面で見る。評価器がここに構造を返してくれれば読み替えるが、いまは
    `code_test_runner` の理由文しか手がかりが無い。**判定を誤ると門 2 が
    甘くなる**（コンパイル失敗を「殺した」に数える）ので、疑わしいものは
    コンパイル失敗として分母から外す ── 甘くするより厳しくする向きに倒す。
    """
    lowered = detail.lower()
    return any(
        marker in lowered
        for marker in ("compil", "コンパイル", "build failed", "syntax error", "構文")
    )
