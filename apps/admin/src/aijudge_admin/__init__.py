"""aiJudge admin — 学期の頭に要る操作（合成の中心）。

コース、受講者、課題。**すべて冪等**で、学期の頭に何度流し直しても
同じ結果になる。既存利用者のパスワードは再生成しない（配った紙が
無効になるため）。

パスワードはファイルにだけ書き出す（0600）。標準出力に出すと端末の
履歴・ログ・画面共有に残る。
"""

from __future__ import annotations

from .authoring import SavedTask, save_task
from .duplicates import DuplicateChecker
from .finalization import (
    FinalizeReport,
    TaskOutcome,
    finalize_task,
    pending_counts,
    sweep_deadlines,
)
from .operations import (
    AdminError,
    EnrolReport,
    ImportedTask,
    ImportReport,
    create_staff,
    enrol_roster,
    ensure_course,
    import_tasks,
    list_courses,
    list_tasks,
    set_password,
)
from .roster import (
    RosterEntry,
    RosterError,
    generate_password,
    load_roster,
    parse_roster,
    write_credentials,
)
from .solvability import SolvabilityChecker
from .task_review import (
    APPROVAL_RATE_GATE,
    ApprovalRate,
    ReviewPacket,
    approval_rate,
    approve,
    build_packet,
    gate_advice,
    pending_reviews,
    reject,
)
from .task_verifier import DEFAULT_MUTATION_LIMIT, TaskVerifier

__all__ = [
    "APPROVAL_RATE_GATE",
    "DEFAULT_MUTATION_LIMIT",
    "AdminError",
    "ApprovalRate",
    "DuplicateChecker",
    "EnrolReport",
    "FinalizeReport",
    "ImportReport",
    "ImportedTask",
    "ReviewPacket",
    "RosterEntry",
    "RosterError",
    "SavedTask",
    "SolvabilityChecker",
    "TaskOutcome",
    "TaskVerifier",
    "approval_rate",
    "approve",
    "build_packet",
    "create_staff",
    "enrol_roster",
    "ensure_course",
    "finalize_task",
    "gate_advice",
    "generate_password",
    "import_tasks",
    "list_courses",
    "list_tasks",
    "load_roster",
    "parse_roster",
    "pending_counts",
    "pending_reviews",
    "reject",
    "save_task",
    "set_password",
    "sweep_deadlines",
    "write_credentials",
]
