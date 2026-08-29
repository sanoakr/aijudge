"""作問とレビューの操作（`aijudge-admin task draft` / `review`）。

**検査を通す順序をここに固定する**（設計方針 §5）。

    draft   生成 → 門 1・門 2 → 解答可能性 → 保存（IN_REVIEW）
    review  待ち行列を出す / 1 件を承認・却下する / 承認率を出す

`--dry-run` を既定にしない代わりに、**保存しても出題はされない** ──
生成物は `IN_REVIEW` で止まり、教員が承認するまで動かない（設計原則 P5）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from aijudge_authoring import TaskChecks, build_task_version
from aijudge_authoring.drafting import Blueprint, Difficulty
from aijudge_core import Task
from aijudge_core.ids import CourseId, TaskVersionId, UserId
from aijudge_grading import EvaluatorRegistry, load_profile
from aijudge_persistence import Database

from .drafting import TaskDrafter
from .solvability import SolvabilityChecker
from .task_review import approval_rate, approve, build_packet, pending_reviews, reject
from .task_verifier import TaskVerifier


def _verifier(args: argparse.Namespace, subject_profile: str) -> TaskVerifier:
    profile = load_profile(Path(args.profiles) / f"{subject_profile}.yaml")
    return TaskVerifier(EvaluatorRegistry().load_installed(), profile)


def cmd_task_draft(args: argparse.Namespace) -> int:
    blueprint = Blueprint(
        knowledge_components=tuple(args.kc),
        subject_profile=args.profile_name,
        difficulty=Difficulty(args.difficulty),
        language=args.language,
        constraints=tuple(args.constraint or ()),
        test_case_count=args.test_cases,
    )

    drafter = TaskDrafter(model=args.model)
    result = drafter.draft(blueprint, key=args.key)
    version = build_task_version(
        result.spec,
        subject_profile=args.profile_name,
        authored_by=UserId(args.author),
        generated_by=result.model,
        generation_prompt_version=result.prompt_id,
    )

    verifier = _verifier(args, args.profile_name)
    verification = verifier.verify(version)

    # **門が落ちたら解答役を呼ばない。** 参照解答が自分のテストケースを
    # 通らない課題を別のモデルに解かせても、測っているものが無い。
    solvability = None
    if verification.usable and not args.no_solvability:
        solvability = SolvabilityChecker(
            verifier, solver_model=args.solver_model, language=args.language
        ).check(version, declared_kcs=blueprint.knowledge_components)

    packet = build_packet(
        version, verification, solvability, declared_kcs=blueprint.knowledge_components
    )
    print(packet.render())

    if args.dry_run:
        print("\n--dry-run のため保存していません。")
        return 0

    database = _open(args)
    try:
        with database.unit_of_work() as uow:
            if uow.tasks.get_task(version.task_id) is None:
                uow.tasks.save_task(
                    Task(
                        id=version.task_id,
                        course_id=CourseId(args.course),
                        title=result.draft.title,
                    )
                )
            uow.tasks.save_version(version)
            # **検査の結果を残す。** 残さないとレビュー画面に出せず、
            # 教員には「検査した」としか示せない（何が生き残ったかが要る）。
            uow.tasks.save_checks(
                version.id,
                TaskChecks(
                    verification=verification,
                    solvability=solvability,
                    declared_kcs=blueprint.knowledge_components,
                    checked_at=datetime.now(UTC),
                ),
            )
            uow.commit()
    finally:
        database.dispose()

    print(f"\n保存しました: {version.id}")
    print("**まだ出題されません。** `aijudge-admin task review` で承認してください。")
    # 門を通らなかったものも保存する。**捨てると門が厳しすぎることに
    # 誰も気づけない**（生き残った変異は課題の欠陥とは限らない）。
    return 0 if packet.clean else 1


def cmd_task_review_list(args: argparse.Namespace) -> int:
    database = _open(args)
    try:
        with database.unit_of_work() as uow:
            waiting = pending_reviews(uow.tasks)
            for version in waiting:
                keys = _kc_keys(uow, version)
                print(f"{version.id}  {version.subject_profile}")
                print(f"  出所: {version.provenance.generated_by or '教員が作成'}")
                print(f"  知識要素: {'・'.join(keys) if keys else '登録なし'}")
    finally:
        database.dispose()
    if not waiting:
        print("レビュー待ちはありません。")
    return 0


def cmd_task_review_decide(args: argparse.Namespace) -> int:
    database = _open(args)
    try:
        with database.unit_of_work() as uow:
            version_id = TaskVersionId(args.version)
            if args.reject:
                updated = reject(
                    uow.tasks,
                    version_id,
                    reviewer=UserId(args.reviewer),
                    reason=args.reason,
                )
            else:
                updated = approve(uow.tasks, version_id, reviewer=UserId(args.reviewer))
            uow.commit()
    except ValueError as exc:
        print(f"できません: {exc}", file=sys.stderr)
        return 1
    finally:
        database.dispose()

    print(f"{updated.id}: {updated.provenance.review_state.value}")
    if updated.provenance.reject_reason:
        print(f"  理由: {updated.provenance.reject_reason}")
    return 0


def cmd_task_review_rate(args: argparse.Namespace) -> int:
    """生成の質を見る。**採点のゲートとは別の指標**（混ぜると読めなくなる）。"""
    database = _open(args)
    try:
        with database.unit_of_work() as uow:
            versions = _all_versions(uow, CourseId(args.course))
    finally:
        database.dispose()
    print(approval_rate(versions).render())
    return 0


# -- internals --------------------------------------------------------------


def _open(args: argparse.Namespace) -> Database:
    return Database.connect(args.database_url, create=args.create_schema)


def _kc_keys(uow, version) -> tuple[str, ...]:
    """ID を可読な正準キーに直す。

    **ID のまま出さない**（`kc_9f3a…` は教員に何も伝えない）。KC が
    登録されていなければその旨を出す ── 黙って空にすると「KC が無い課題」と
    区別が付かない。
    """
    keys: list[str] = []
    for entry in version.q_matrix:
        kc = uow.skills.get_kc(entry.kc_id)
        keys.append(kc.key if kc is not None else f"{entry.kc_id}（未登録）")
    return tuple(keys)


def _all_versions(uow, course_id: CourseId) -> tuple:
    versions = []
    for task in uow.tasks.list_for_course(course_id):
        version = uow.tasks.latest_version(task.id)
        if version is not None:
            versions.append(version)
    return tuple(versions)


def register(task_parser) -> None:
    """`aijudge-admin task` に作問とレビューを足す。"""
    draft = task_parser.add_parser("draft", help="AI に課題を作らせる（要レビュー）")
    draft.add_argument("--course", required=True)
    draft.add_argument("--key", required=True, help="課題キー（例 gen/ex01）")
    draft.add_argument("--author", required=True, help="作問を指示した教員の利用者 ID")
    draft.add_argument(
        "--kc", action="append", required=True, help="問う知識要素の正準キー（複数可）"
    )
    draft.add_argument("--profile-name", default="cs_intro_c", help="科目プロファイル名")
    draft.add_argument("--language", default="c")
    draft.add_argument(
        "--difficulty",
        default=Difficulty.STANDARD.value,
        choices=[d.value for d in Difficulty],
    )
    draft.add_argument("--constraint", action="append", help="課題文に入れる制約（複数可）")
    draft.add_argument("--test-cases", type=int, default=5)
    draft.add_argument("--model", default=None, help="下書きを作るモデル")
    draft.add_argument(
        "--solver-model",
        default=None,
        help="解答可能性を試すモデル。**下書きとは別のモデルにすること**",
    )
    draft.add_argument(
        "--no-solvability", action="store_true", help="解答可能性の検査を省く"
    )
    draft.add_argument("--dry-run", action="store_true", help="保存しない")
    draft.set_defaults(func=cmd_task_draft)

    review = task_parser.add_parser("review", help="生成された課題のレビュー")
    review_sub = review.add_subparsers(dest="review_command", required=True)

    listing = review_sub.add_parser("list", help="レビュー待ちの一覧")
    listing.set_defaults(func=cmd_task_review_list)

    decide = review_sub.add_parser("decide", help="1 件を承認または却下する")
    decide.add_argument("--version", required=True, help="課題版 ID")
    decide.add_argument("--reviewer", required=True, help="教員の利用者 ID")
    decide.add_argument("--reject", action="store_true", help="却下する（既定は承認）")
    decide.add_argument("--reason", default=None, help="却下の理由（却下には必須）")
    decide.set_defaults(func=cmd_task_review_decide)

    rate = review_sub.add_parser("rate", help="承認率（Phase 4 の基準 60%%）")
    rate.add_argument("--course", required=True)
    rate.set_defaults(func=cmd_task_review_rate)
