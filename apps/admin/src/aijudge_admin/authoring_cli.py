"""作問の操作（`aijudge-admin task draft` / `review rate`）。

**検査を通す順序をここに固定する**（設計方針 §5）。

    draft        生成 → 門 1・門 2 → 解答可能性 → 重複 → **下書き**として保存
    review rate  承認率を出す

`--dry-run` を既定にしない代わりに、**保存しても出題はされない** ──
生成物は下書き（`TaskDraftRecord`）になり、コンソールの「未承認の課題（AI 作問）」
で教員が承認するまで課題にならない（設計原則 P5・ADR 0019）。

**承認・却下は画面だけで行う**（#522）。以前は `review list` / `review decide` が
あり、生成物を「承認待ちの課題版」として保存していたが、その版は画面から承認
できず、承認の規則も CLI と画面の 2 か所に分かれていた。
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

from aijudge_authoring import DraftKind, TaskChecks, TaskDraftRecord, build_task_version
from aijudge_authoring.difficulty import (
    DEFAULT_PASS_THRESHOLD,
    DifficultyEstimate,
    TaskOutcomeStats,
    estimate,
)
from aijudge_authoring.drafting import Blueprint, Difficulty
from aijudge_core import new_id
from aijudge_core.ids import CourseId, TaskVersionId, UserId
from aijudge_course_admin.drafting import TaskDrafter
from aijudge_course_admin.duplicates import DuplicateChecker
from aijudge_course_admin.solvability import SolvabilityChecker
from aijudge_course_admin.task_review import approval_rate, build_packet
from aijudge_course_admin.task_verifier import TaskVerifier
from aijudge_grading import EvaluatorRegistry, load_profile
from aijudge_persistence import Database


def _verifier(args: argparse.Namespace, subject_profile: str) -> TaskVerifier:
    profile = load_profile(Path(args.profiles) / f"{subject_profile}.yaml")
    return TaskVerifier(EvaluatorRegistry().load_installed(), profile)


def cmd_task_draft(args: argparse.Namespace) -> int:
    blueprint = Blueprint(
        knowledge_components=tuple(args.kc),
        subject_profile=args.profile_name,
        difficulty=Difficulty(args.difficulty),
        language=args.language,
        # `--constraint` は `--instruction` の旧名。**同じ列に流す** ──
        # 別々に持つと、どちらに書いたかでモデルへの渡り方が変わる。
        instructions=tuple(args.instruction or ()) + tuple(args.constraint or ()),
        test_case_count=args.test_cases,
    )

    drafter = TaskDrafter(model=args.model)
    result = drafter.draft(blueprint, key=args.key)
    version = build_task_version(
        result.spec,
        course_id=CourseId(args.course),
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

    # 重複は門と独立に測れる（実行が要らない）ので、門が落ちても走らせる。
    duplicates = None
    difficulty = None
    if not args.no_duplicates:
        database = _open(args)
        try:
            with database.unit_of_work() as uow:
                existing = _existing_statements(uow, CourseId(args.course), version.id)
                duplicates = DuplicateChecker(
                    uow.tasks, embedding_model=args.embedding_model
                ).check(version, existing)
                # **近傍は重複検出が選んだものをそのまま使う。** 別に選び直すと、
                # 「重複していない」と「難度が近い」が違う近傍を根拠にすることになる。
                difficulty = _estimate_difficulty(uow, duplicates)
                uow.commit()
        finally:
            database.dispose()

    packet = build_packet(
        version,
        verification,
        solvability,
        declared_kcs=blueprint.knowledge_components,
        duplicates=duplicates,
        difficulty=difficulty,
    )
    print(packet.render())

    if args.dry_run:
        print("\n--dry-run のため保存していません。")
        return 0

    # **課題にはしない。下書きとして置く**（#321・#522）。画面の作問と同じ形にし、
    # コンソールの「未承認の課題（AI 作問）」で承認できるようにする。検査の結果は
    # 下書きが持つ（課題版はまだ無い）。出題先は承認のときに決める（#84）。
    draft = TaskDraftRecord(
        id=new_id("dft"),
        course_id=CourseId(args.course),
        kind=DraftKind.NEW,
        spec=result.spec,
        generated_by=result.model,
        generation_prompt_version=result.prompt_id,
        created_by=UserId(args.author),
        created_at=datetime.now(UTC),
        checks=TaskChecks(
            verification=verification,
            solvability=solvability,
            declared_kcs=blueprint.knowledge_components,
            duplicates=duplicates,
            difficulty=difficulty,
            checked_at=datetime.now(UTC),
        ),
        subject_profile=args.profile_name,
        readability_weight=result.spec.readability_weight,
    )
    database = _open(args)
    try:
        with database.unit_of_work() as uow:
            uow.tasks.save_draft(draft)
            uow.commit()
    finally:
        database.dispose()

    print(f"\n下書きとして保存しました: {draft.id}")
    print("**まだ出題されません。** コンソールの「未承認の課題（AI 作問）」で承認してください。")
    # 門を通らなかったものも保存する。**捨てると門が厳しすぎることに
    # 誰も気づけない**（生き残った変異は課題の欠陥とは限らない）。
    return 0 if packet.clean else 1


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


def _estimate_difficulty(uow, duplicates) -> DifficultyEstimate | None:
    """似た課題の実績から難度を見込む（設計方針 §5）。

    **実績が無ければ推定しない。** 学期の頭は必ずこの状態になり、そこで
    数字を出すと、根拠の無い難度が課題に付いたまま残る。
    """
    if duplicates is None or not duplicates.nearest:
        return None
    ids = tuple(TaskVersionId(item.task_version_id) for item in duplicates.nearest)
    counted = uow.tasks.pass_rates(ids, threshold=DEFAULT_PASS_THRESHOLD)
    stats = {
        vid: TaskOutcomeStats(task_version_id=vid, attempts=attempts, passed=passed)
        for vid, (attempts, passed) in counted.items()
    }
    return estimate(duplicates.nearest, stats, method=duplicates.method)


def _existing_statements(uow, course_id: CourseId, exclude) -> dict:
    """比較対象の既存課題。

    **同じコースに限る。** どこまでを既存と見なすかは運用の判断だが、
    既定を科目全体にすると、別のコースの課題と似ていることを理由に
    教員が判断を迷う（そのコースの学習者には初見である）。
    """
    found = {}
    for task in uow.tasks.list_for_course(course_id):
        version = uow.tasks.latest_version(task.id)
        if version is not None and version.id != exclude:
            found[version.id] = (task.title, version.statement)
    return found


def _all_versions(uow, course_id: CourseId) -> tuple:
    versions = []
    for task in uow.tasks.list_for_course(course_id):
        version = uow.tasks.latest_version(task.id)
        if version is not None:
            versions.append(version)
    return tuple(versions)


def register(task_parser) -> None:
    """`aijudge-admin task` に作問とレビューを足す。"""
    draft = task_parser.add_parser(
        "draft", help="AI に課題を作らせる（下書きになる。承認は画面で）"
    )
    draft.add_argument("--course", required=True)
    draft.add_argument("--key", required=True, help="課題キー（例 gen/ex01）")
    draft.add_argument("--author", required=True, help="作問を指示した教員の利用者 ID")
    draft.add_argument(
        "--kc", action="append", required=True, help="問う知識要素の正準キー（複数可）"
    )
    draft.add_argument("--profile-name", default="cs_lang_c_intro", help="科目プロファイル名")
    draft.add_argument("--language", default="c")
    draft.add_argument(
        "--difficulty",
        default=Difficulty.STANDARD.value,
        choices=[d.value for d in Difficulty],
    )
    draft.add_argument(
        "--instruction", action="append", help="AI への指示（複数可）。必須事項も希望も書ける"
    )
    # 旧名。台本に残っているので受け続ける（`--instruction` と同じ列に入る）。
    draft.add_argument("--constraint", action="append", help=argparse.SUPPRESS)
    draft.add_argument("--test-cases", type=int, default=5)
    draft.add_argument("--model", default=None, help="下書きを作るモデル")
    draft.add_argument(
        "--solver-model",
        default=None,
        help="解答可能性を試すモデル。**下書きとは別のモデルにすること**",
    )
    draft.add_argument("--no-solvability", action="store_true", help="解答可能性の検査を省く")
    draft.add_argument(
        "--embedding-model",
        default=None,
        help="重複検出に使う埋め込みモデル。指定しなければ字面だけで測る",
    )
    draft.add_argument("--no-duplicates", action="store_true", help="重複の検査を省く")
    draft.add_argument("--dry-run", action="store_true", help="保存しない")
    draft.set_defaults(func=cmd_task_draft)

    # 承認・却下は画面だけ（#522）。ここに残すのは承認率だけ。
    review = task_parser.add_parser("review", help="生成された課題の承認率")
    review_sub = review.add_subparsers(dest="review_command", required=True)

    rate = review_sub.add_parser("rate", help="承認率（Phase 4 の基準 60%%）")
    rate.add_argument("--course", required=True)
    rate.set_defaults(func=cmd_task_review_rate)
