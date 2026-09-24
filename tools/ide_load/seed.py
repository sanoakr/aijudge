"""IDE の負荷試験用のデータを作る（設計書 §12 の段階 3）。

    uv run python tools/ide_load/seed.py sqlite:///path/load.db --learners 150

**使い捨ての DB にだけ使う。** 運用中の DB に向けない ── 学習者を N 人と
試験用のコースを作る。既定の接続先は持たせず、引数で必ず指定させる。

作るもの:

- コース 1 つ、エディタで解く問題セット `load01`（C の課題 3 問、1 問目に公開サンプル）
- 学習者 `load001`〜（パスワードは全員同じ・`--password`）
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aijudge_authoring.importers import sharif_judge
from aijudge_core import AnswerMode, Course, Role, Task
from aijudge_core.ids import CourseId, TaskId, TaskVersionId, TenantId, UserId
from aijudge_identity import AuthService
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_TASK = REPO_ROOT / "evals" / "golden" / "cs_lang_c_intro" / "example-task" / "task"
TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
AUTHOR = UserId("usr_" + "3" * 32)
PROBLEMS = 3


def main() -> None:
    parser = argparse.ArgumentParser(description="IDE の負荷試験用のデータを作る")
    parser.add_argument("database_url", help="使い捨ての DB（運用中の DB に向けない）")
    parser.add_argument("--learners", type=int, default=150)
    parser.add_argument("--password", default="load-test-pass")
    parser.add_argument("--minutes", type=int, default=90, help="受付の長さ（分）")
    args = parser.parse_args()

    db = Database.connect(args.database_url, create=True)
    now = datetime.now(UTC)
    with db.unit_of_work() as uow:
        uow.identity.save_course(
            Course(
                id=COURSE,
                tenant_id=TENANT,
                code="load",
                title="負荷試験",
                term="2026-後期",
                subject_profile="cs_lang_c_intro",
            )
        )
        base = sharif_judge.import_problem(
            EXAMPLE_TASK, course_id=COURSE, subject_profile="cs_lang_c_intro", authored_by=AUTHOR
        )
        for number in range(1, PROBLEMS + 1):
            cases = list(base.test_cases)
            cases[0] = cases[0].model_copy(update={"hidden": False})
            task_id = TaskId(f"tsk_{number:032x}")
            version = base.model_copy(
                update={
                    "id": TaskVersionId(f"tsv_{number:032x}"),
                    "task_id": task_id,
                    "test_cases": tuple(cases),
                }
            )
            uow.tasks.save_task(
                Task(
                    id=task_id,
                    course_id=COURSE,
                    title=f"問題 {number}",
                    unit="load01",
                    session=1,
                    position=number,
                    answer_mode=AnswerMode.EDITOR,
                    accepted_suffixes=(".c",),
                    opens_at=now - timedelta(minutes=5),
                    due_at=now + timedelta(minutes=args.minutes),
                    accepts_until=now + timedelta(minutes=args.minutes),
                )
            )
            uow.tasks.save_version(version)
        auth = AuthService(uow.identity, audit=uow.audit)
        for index in range(1, args.learners + 1):
            principal = auth.register(
                tenant_id=TENANT,
                login=f"load{index:03d}",
                display_name=f"負荷 {index:03d}",
                password=args.password,
            )
            auth.enroll(
                tenant_id=TENANT, course_id=COURSE, user_id=principal.user_id, role=Role.LEARNER
            )
        uow.commit()
    print(f"course={COURSE} learners={args.learners} unit=load01")


if __name__ == "__main__":
    main()
