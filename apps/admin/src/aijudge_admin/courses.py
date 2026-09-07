"""コースを消す（#156）。

**課題と同じ区別をコースにも当てる**（`aijudge_admin.tasks`）。

    学習者の提出が無い  削除できる（打ち間違い・使わなかったコースの後始末）
    1 件でもある        消さない

採点結果は課題版を指しており（P8）、消すと過去の成績が何のコースの点なのか
辿れなくなる。**「使わなくする」と「無かったことにする」は別の操作である。**

**教員の動作確認（trial、#108）は削除を妨げない。** 成績にも統計にも
数えない提出なので、失う記録が無い ── コースごと消すときは一緒に消す。
これがこのシステムで**提出を消す唯一の経路**で、だからこそ「学習者の提出が
1 件でもあれば消さない」を先に確かめる。

消した提出の成果物（ファイル）は、ストアが削除に対応していれば消す。
対応していないストア（`ArtifactStore` の最小の形）では残る ── 残っても
参照する行が無いだけで、成績には現れない。
"""

from __future__ import annotations

from dataclasses import dataclass

from aijudge_core import Course
from aijudge_core.ids import CourseId
from aijudge_persistence import Database
from aijudge_submission import StreamingArtifactStore

from .operations import AdminError


@dataclass(frozen=True)
class DeletedCourse:
    """消した結果。**何をどれだけ消したかを返す。**

    件数だけを返すと「何がどうなったのか」が言えなくなる
    （`aijudge_admin.tasks` の `UnitReport` と同じ理由）。
    """

    course: Course
    tasks: int
    trial_submissions: int
    enrolments: int


def delete_course(
    database: Database, *, course_id: CourseId, artifact_store: object | None = None
) -> DeletedCourse:
    """**学習者の提出が 1 件も無いコースだけを消す。**

    1 件でもあれば消さない ── 採点結果は課題版を指しており（P8）、消すと
    過去の成績が何のコースの点なのか辿れなくなる。
    """
    with database.unit_of_work() as uow:
        course = uow.identity.get_course(course_id)
        if course is None:
            raise AdminError(f"コース {course_id!r} がありません")
        submissions = uow.submissions.list_for_course(course_id)
        learner_submissions = [item for item in submissions if not item.is_trial]
        trials = [item for item in submissions if item.is_trial]
        tasks = uow.tasks.list_for_course(course_id)
        enrolments = uow.identity.list_enrollments(course_id)

    if learner_submissions:
        raise AdminError(
            f"このコースには学習者の提出が {len(learner_submissions)} 件あります。"
            "消すと、その提出に付いた成績が何のコースの点なのか辿れなくなります。"
        )

    keys = [artifact.storage_key for submission in trials for artifact in submission.artifacts]

    with database.unit_of_work() as uow:
        # 提出 → 課題 → コースの順に消す。**逆にすると、消したコースを指す
        # 課題が残っている瞬間ができる**（途中で落ちたときに残る形が変わる）。
        uow.submissions.delete([submission.id for submission in trials])
        for task in tasks:
            uow.tasks.delete_task(task.id)
        uow.identity.delete_course(course_id)
        uow.commit()

    _remove_artifacts(artifact_store, keys)
    return DeletedCourse(
        course=course,
        tasks=len(tasks),
        trial_submissions=len(trials),
        enrolments=len(enrolments),
    )


def _remove_artifacts(store: object | None, keys: list[str]) -> None:
    """動作確認の提出物を消す。**消せないストアでは何もしない。**

    削除は `StreamingArtifactStore` にしかない（最小の `ArtifactStore` は
    put/get/exists だけ）。**DB を消せたのにファイルで失敗して全体を巻き
    戻す、という形にはしない** ── 参照する行はもう無く、残っても成績には
    現れないので、消せるものを消す。
    """
    if store is None or not isinstance(store, StreamingArtifactStore):
        return
    for key in keys:
        try:
            store.delete(key)
        except Exception:  # pragma: no cover - ストアの実装差を吸収する
            continue


__all__ = ["DeletedCourse", "delete_course"]
