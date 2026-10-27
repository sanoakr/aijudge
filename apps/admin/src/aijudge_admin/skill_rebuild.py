"""習熟度を過去の採点記録から作り直す（ADR 0032）。

数え方を「採点 1 回ごと」から「課題ごとの最終提出」に変え、試験と演習で BKT の
パラメータを分けた。**変える前に積まれた習熟度は古い数え方の値のまま**なので、
採点記録から観測を作り直し、畳み直す。

ここは合成ルートである（`aijudge_grader.skill_subscriber` と同じ）。S7 は採点も
保存先も知らないので、提出・採点記録・課題を引いてイベントの形に直すのはここで行う。

- **テナント単位で作り直す。** 習熟度は KC 単位でコースをまたいで積み上がる（P6）。
  1 コースだけ作り直すと、他のコースの観測が抜けた値になる。
- 再生するのは提出ごとの**最新の採点**だけ。暫定の採点・動作確認の提出・確信度の
  足りない判定は、ふだんの更新と同じく数えない（`SkillService` が判断する）。
- **推移（`SkillPoint`）は消さない**（P8）。再生の途中では点を書かず、作り直した値を
  最後に 1 点ずつ足す。いつ作り直したかは監査ログに残す。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aijudge_audit import AuditAction, AuditRecorder
from aijudge_core import GradingCompleted, MasteryModel, MasteryScope, SkillPoint
from aijudge_core.ids import TaskId, TaskVersionId, TenantId
from aijudge_grading import grading_completed_event
from aijudge_persistence import Database
from aijudge_skill import SkillService


@dataclass(frozen=True)
class RebuildReport:
    courses: int
    submissions: int
    replayed: int
    learners: int
    states: int


def _events(database: Database, tenant_id: TenantId) -> tuple[int, int, list[GradingCompleted]]:
    """テナントの全コースの提出を、採点完了のイベントの形に直す。"""
    events: list[GradingCompleted] = []
    submissions = 0
    with database.unit_of_work() as uow:
        courses = uow.identity.list_courses(tenant_id)
        for course in courses:
            tasks = {task.id: task for task in uow.tasks.list_for_course(course.id)}
            task_of: dict[TaskVersionId, TaskId] = {
                version.id: version.task_id for version in uow.tasks.versions_for_tasks(tasks)
            }
            submitted = [
                item
                for item in uow.submissions.iter_for_course(course.id)
                if item.submitted_at is not None
            ]
            submissions += len(submitted)
            runs = uow.runs.latest_for_many([item.id for item in submitted])
            for item in submitted:
                run = runs.get(item.id)
                if run is None or not run.kc_outcomes:
                    continue
                task_id = task_of.get(item.task_version_id)
                task = None if task_id is None else tasks.get(task_id)
                events.append(
                    grading_completed_event(
                        run,
                        item,
                        tenant_id=tenant_id,
                        task_id=task_id,
                        exam=task is not None and task.exam,
                    )
                )
    return len(courses), submissions, events


def rebuild_skills(database: Database, tenant_id: TenantId, *, now: datetime) -> RebuildReport:
    """テナントの習熟度を作り直す。**1 つの作業単位で行う**（途中で落ちたら何も変えない）。"""
    courses, submissions, events = _events(database, tenant_id)
    # 提出の順に再生する。最終提出の判定と畳む順は提出の時刻で決まるが、
    # 冪等判定（同じ採点を二度数えない）に頼らずに済むよう、順序もそろえておく。
    events.sort(
        key=lambda event: (event.submitted_at or event.occurred_at, str(event.submission_id))
    )

    with database.unit_of_work() as uow:
        uow.skills.clear(tenant_id)
        service = SkillService(uow.skills, record_points=False)
        replayed = 0
        learners = set()
        for event in events:
            if event.is_trial:
                # 動作確認の提出は習熟度に積まない（`skill_subscriber` と同じ）。
                continue
            service.apply(event, observed_at=event.occurred_at)
            replayed += 1
            learners.add(event.learner_id)
        states = uow.skills.list_states_for(tenant_id, tuple(sorted(learners)))
        for state in states:
            # 作り直した値を、作り直した時刻の点として足す（過去の点は残す）。
            # 範囲（演習だけ・試験だけ・両方）ごとに、値のあるものだけ。
            for scope in MasteryScope:
                value = state.mastery_in(scope)
                if value is None:
                    continue
                uow.skills.append_point(
                    SkillPoint(
                        tenant_id=tenant_id,
                        learner_id=state.learner_id,
                        kc_id=state.kc_id,
                        mastery=value,
                        observation_count=state.count_in(scope),
                        model=MasteryModel.BKT,
                        scope=scope,
                        recorded_at=now,
                    )
                )
        report = RebuildReport(
            courses=courses,
            submissions=submissions,
            replayed=replayed,
            learners=len(learners),
            states=len(states),
        )
        AuditRecorder.for_system(uow.audit, tenant_id=tenant_id).record(
            AuditAction.SKILLS_REBUILT,
            target_type="tenant",
            target_id=str(tenant_id),
            summary=(
                f"習熟度を作り直しました（学習者 {report.learners} 人・"
                f"習熟度 {report.states} 件・再生した採点 {report.replayed} 件）"
            ),
            detail={
                "courses": report.courses,
                "submissions": report.submissions,
                "replayed": report.replayed,
                "learners": report.learners,
                "states": report.states,
            },
        )
        uow.commit()
    return report
