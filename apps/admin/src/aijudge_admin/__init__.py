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
    finalize_tasks,
    pending_counts,
    sweep_deadlines,
)
from .grading_settings import TrialResult, template_of, try_settings
from .grading_settings import save as save_grading_settings
from .grading_settings import validate as validate_grading_settings
from .kc import (
    KcUsage,
    allowed_namespaces,
    assert_registered,
    list_for_namespaces,
)
from .kc import register as register_kc
from .kc import restore as restore_kc
from .kc import retire as retire_kc
from .kc import usage as kc_usage
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
    "KcUsage",
    "ReviewPacket",
    "RosterEntry",
    "RosterError",
    "SavedTask",
    "SolvabilityChecker",
    "TaskOutcome",
    "TaskVerifier",
    "TrialResult",
    "allowed_namespaces",
    "approval_rate",
    "approve",
    "assert_registered",
    "build_packet",
    "create_staff",
    "enrol_roster",
    "ensure_course",
    "finalize_task",
    "finalize_tasks",
    "gate_advice",
    "generate_password",
    "import_tasks",
    "kc_usage",
    "list_courses",
    "list_for_namespaces",
    "list_tasks",
    "load_roster",
    "parse_roster",
    "pending_counts",
    "pending_reviews",
    "register_kc",
    "reject",
    "restore_kc",
    "retire_kc",
    "save_grading_settings",
    "save_task",
    "set_password",
    "sweep_deadlines",
    "template_of",
    "try_settings",
    "validate_grading_settings",
    "write_credentials",
]
