"""識別子。

すべての ID は `<prefix>_<uuid4hex>` 形式の文字列とする。
プレフィックスを持たせるのは、ログや API レスポンスに現れた ID だけを見て
それがどの実体か分かるようにするため（デバッグ時のコストが大きく変わる）。
"""

from __future__ import annotations

import re
import uuid
from typing import NewType

TenantId = NewType("TenantId", str)
CourseId = NewType("CourseId", str)
UserId = NewType("UserId", str)
TaskId = NewType("TaskId", str)
TaskVersionId = NewType("TaskVersionId", str)
CriterionId = NewType("CriterionId", str)
KcId = NewType("KcId", str)
SubmissionId = NewType("SubmissionId", str)
GradingJobId = NewType("GradingJobId", str)
SessionId = NewType("SessionId", str)
ArtifactId = NewType("ArtifactId", str)
GradingRunId = NewType("GradingRunId", str)
EvaluatorResultId = NewType("EvaluatorResultId", str)
CriterionScoreId = NewType("CriterionScoreId", str)
HumanReviewId = NewType("HumanReviewId", str)
FinalizationId = NewType("FinalizationId", str)
ReviewRequestId = NewType("ReviewRequestId", str)
CredentialId = NewType("CredentialId", str)
EventId = NewType("EventId", str)

# プレフィックス → 用途。新しい実体を足すときは必ずここにも登録する。
PREFIXES: dict[str, str] = {
    "ten": "Tenant",
    "crs": "Course",
    "usr": "User",
    "tsk": "Task",
    "tsv": "TaskVersion",
    "crt": "RubricCriterion",
    "kc": "KnowledgeComponent",
    "sub": "Submission",
    "job": "GradingJob",
    "ses": "Session",
    "art": "Artifact",
    "grn": "GradingRun",
    "evr": "EvaluatorResult",
    "cs": "CriterionScore",
    "hrv": "HumanReview",
    "fin": "Finalization",
    "rrq": "ReviewRequest",
    "cred": "Credential",
    "evt": "DomainEvent",
}

_ID_RE = re.compile(r"^(?P<prefix>[a-z]+)_(?P<body>[0-9a-f]{32})$")


def new_id(prefix: str) -> str:
    """新しい ID を採番する。"""
    if prefix not in PREFIXES:
        raise ValueError(f"unknown id prefix: {prefix!r}")
    return f"{prefix}_{uuid.uuid4().hex}"


# 決定的 ID を作るための名前空間。値を変えると既存 ID がすべて変わるので固定。
_NAMESPACE = uuid.UUID("6f9b1f6e-0d1a-4f1e-9b3a-8c5d2e7a4b10")


def derived_id(prefix: str, *parts: str) -> str:
    """同じ入力から必ず同じ ID を作る。

    取り込みのたびに ID が振り直されると、保存済みの採点結果を
    課題の観点に結び付けられなくなる（stored な GradingRun が読めても、
    どの観点の点なのか分からない）。既存資産の取り込みのように
    「同じものを何度も取り込む」経路では決定的な ID が要る。
    """
    if prefix not in PREFIXES:
        raise ValueError(f"unknown id prefix: {prefix!r}")
    if not parts:
        raise ValueError("derived_id needs at least one part")
    key = "\x1e".join(parts)
    return f"{prefix}_{uuid.uuid5(_NAMESPACE, f'{prefix}:{key}').hex}"


def prefix_of(value: str) -> str:
    """ID からプレフィックスを取り出す。形式が不正なら ValueError。"""
    match = _ID_RE.match(value)
    if match is None:
        raise ValueError(f"malformed id: {value!r}")
    prefix = match.group("prefix")
    if prefix not in PREFIXES:
        raise ValueError(f"unknown id prefix in {value!r}")
    return prefix


def is_id(value: str, prefix: str) -> bool:
    """`value` が `prefix` の ID かどうか。"""
    try:
        return prefix_of(value) == prefix
    except ValueError:
        return False
