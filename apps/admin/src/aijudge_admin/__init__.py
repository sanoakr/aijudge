"""aiJudge admin — 学期の頭に要る操作（合成の中心）。

コース、受講者、課題。**すべて冪等**で、学期の頭に何度流し直しても
同じ結果になる。既存利用者のパスワードは再生成しない（配った紙が
無効になるため）。

パスワードはファイルにだけ書き出す（0600）。標準出力に出すと端末の
履歴・ログ・画面共有に残る。
"""

from __future__ import annotations

from . import rubric
from .authoring import SavedTask, save_task
from .bundle_plan import PlannedChange, PlannedTask, plan_bundle
from .bundles import BundledTask, read_bundle, template_bundle
from .course_copy import DuplicatedCourse, duplicate_course
from .courses import DeletedCourse, delete_course
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
from .kc import delete as delete_kc
from .kc import edit as edit_kc
from .kc import register as register_kc
from .kc import restore as restore_kc
from .kc import retire as retire_kc
from .kc import usage as kc_usage
from .operations import (
    AdminError,
    EnrolReport,
    ImportedTask,
    ImportReport,
    course_id_for,
    create_staff,
    enrol_roster,
    ensure_course,
    import_tasks,
    list_courses,
    list_tasks,
    set_password,
)
from .profiles import (
    ProfileSummary,
    duplicate_profile,
    list_profiles,
    read_profile_text,
    rename_profile,
    save_profile_text,
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
    "BundledTask",
    "DeletedCourse",
    "DuplicateChecker",
    "DuplicatedCourse",
    "EnrolReport",
    "FinalizeReport",
    "ImportReport",
    "ImportedTask",
    "KcUsage",
    "PlannedChange",
    "PlannedTask",
    "ProfileSummary",
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
    "course_id_for",
    "create_staff",
    "delete_course",
    "delete_kc",
    "duplicate_course",
    "duplicate_profile",
    "edit_kc",
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
    "list_profiles",
    "list_tasks",
    "load_roster",
    "parse_roster",
    "pending_counts",
    "pending_reviews",
    "plan_bundle",
    "read_bundle",
    "read_profile_text",
    "register_kc",
    "reject",
    "rename_profile",
    "restore_kc",
    "retire_kc",
    "rubric",
    "save_grading_settings",
    "save_profile_text",
    "save_task",
    "set_password",
    "sweep_deadlines",
    "template_bundle",
    "template_of",
    "try_settings",
    "validate_grading_settings",
    "write_credentials",
]
