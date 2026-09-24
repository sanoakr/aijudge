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

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from aijudge_core import Course
from aijudge_core.ids import CourseId, SubmissionId, is_id
from aijudge_persistence import Database
from aijudge_submission import StreamingArtifactStore

from .operations import AdminError

# 行動記録の本体の置き場所（web と同じ変数・`aijudge_studentweb.cli`）。
ENV_ACTIVITY_DIR = "AIJUDGE_ACTIVITY_DIR"


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
    database: Database,
    *,
    course_id: CourseId,
    artifact_store: object | None = None,
    activity_dir: Path | None = None,
) -> DeletedCourse:
    """**学習者の提出が 1 件も無いコースだけを消す。**

    1 件でもあれば消さない ── 採点結果は課題版を指しており（P8）、消すと
    過去の成績が何のコースの点なのか辿れなくなる。
    """
    with database.unit_of_work() as uow:
        course = uow.identity.get_course(course_id)
        if course is None:
            raise AdminError(f"コース {course_id!r} がありません")
        # **打ち切られた一覧で決めない**（#219）。`list_for_course` の上限は
        # 画面のためのもので、古い側だけを見て「学習者の提出は無い」と結論
        # すると、実際にはある提出を成果物ごと消す。
        #
        # `is_trial` が列になったので、判断は数える 1 文で済む。中身が要る
        # のは**消すと決まってから**で、そこで初めて試行を読む。
        learner_submissions = uow.submissions.count_for_course(course_id).learner
        trial_ids: list[SubmissionId] = []
        keys: list[str] = []
        if not learner_submissions:
            for submission in uow.submissions.iter_for_course(course_id):
                trial_ids.append(submission.id)
                keys.extend(artifact.storage_key for artifact in submission.artifacts)
        tasks = uow.tasks.list_for_course(course_id)
        enrolments = uow.identity.list_enrollments(course_id)

    if learner_submissions:
        raise AdminError(
            "このコースには学習者の提出があります。"
            "消すと、その提出に付いた成績が何のコースの点なのか辿れなくなります。"
        )

    with database.unit_of_work() as uow:
        # 提出 → 課題 → コースの順に消す。**逆にすると、消したコースを指す
        # 課題が残っている瞬間ができる**（途中で落ちたときに残る形が変わる）。
        uow.submissions.delete(trial_ids)
        for task in tasks:
            uow.tasks.delete_task(task.id)
        # IDE の行動記録の索引（ADR 0023）。**学習者の提出が無くても、学習者の
        # 記録はありうる** ── エディタを開いて書いたが出さなかった学習者の分。
        uow.ide_activity.delete_for_course(course_id)
        uow.identity.delete_course(course_id)
        uow.commit()

    _remove_artifacts(artifact_store, keys)
    _remove_activity(activity_dir, course_id)
    return DeletedCourse(
        course=course,
        tasks=len(tasks),
        trial_submissions=len(trial_ids),
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


def _remove_activity(activity_dir: Path | None, course_id: CourseId) -> None:
    """このコースの行動記録の本体（ファイル）を消す。

    本体は `{根}/{コース}/...` に置いてある（`aijudge_ide.ActivityFiles`）ので、
    コースのディレクトリごと消す。根は引数、無ければ web と同じ環境変数から読む。
    **どちらも無ければ何もしない** ── 成果物と同じく、索引を消せたのにファイルで
    失敗して全体を巻き戻す形にはしない。
    """
    root = activity_dir
    if root is None:
        configured = os.environ.get(ENV_ACTIVITY_DIR)
        root = Path(configured).expanduser() if configured else None
    if root is None:
        return
    # コース ID は `crs_<32 桁>` の形に限る。**根の外を消させない。**
    if not is_id(str(course_id), "crs"):
        return
    target = root / str(course_id)
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)


__all__ = ["DeletedCourse", "delete_course"]
