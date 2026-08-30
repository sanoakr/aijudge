"""コースの採点設定を組み立てて確かめる。

雛形（`subjects/*.yaml`）を土台に、コースの上書きを作る場所。上書きの意味は
`aijudge_grading.overrides` にある ── ここは画面から来た値を形にし、保存の
前に**壊れていないこと**を確かめる役。

検査は 2 段。

  1. **保存時**（必ず）── 模型の検証と、評価器が実在し種別が宣言と一致
     すること。起動時と同じ検査なので、通れば少なくとも採点は始まる
  2. **試走**（任意）── 実際に 1 件走らせる。`language` の取り違えは
     設定として正しく、結果は「全員 0 点」で原因が提出側に見える。
     設定の検査では捕まらないので、道具として置く。関門にはしない
     （見本が無いコースもあり、そこで保存を止めると何もできなくなる）

**試走は保存しない。** 走らせるだけで `GradingRun` は残さない ── 設定を
試した記録が成績の履歴に混ざると、学習者に見える採点が増える。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aijudge_core import Course, TaskVersion
from aijudge_grading import (
    EvaluatorRegistry,
    GradingPipeline,
    OverrideError,
    SubjectProfile,
    effective_profile,
    load_profile,
)
from aijudge_persistence import Database

from .operations import AdminError


@dataclass(frozen=True)
class TrialResult:
    """試走の結果。**設定が効いたかどうかを見るためのもの。**"""

    task: object
    score_ratio: float
    unscored: tuple[str, ...]
    message: str

    @property
    def looks_wrong(self) -> bool:
        """参照解答が満点にならなかった。**設定の取り違えを疑う場面。**

        参照解答は課題が自分で持っている正解なので、通らないなら課題か
        設定のどちらかが違う。`language` の取り違えはここで現れる。
        """
        return self.score_ratio < 1.0 or bool(self.unscored)


def template_of(course: Course, profiles_dir: Path) -> SubjectProfile:
    """このコースの雛形。上書きを重ねる前のもの。"""
    path = profiles_dir / f"{course.subject_profile}.yaml"
    if not path.is_file():
        raise AdminError(f"雛形 {course.subject_profile!r} がありません（{path}）")
    return load_profile(path)


def validate(
    course: Course, overrides: dict, profiles_dir: Path, registry: EvaluatorRegistry
) -> SubjectProfile:
    """上書きを重ねて検査する。**保存の前に必ず通す。**"""
    base = template_of(course, profiles_dir)
    try:
        return effective_profile(base, overrides, registry)
    except OverrideError as exc:
        raise AdminError(str(exc)) from None


def save(
    database: Database,
    course: Course,
    overrides: dict,
    *,
    profiles_dir: Path,
    registry: EvaluatorRegistry,
) -> Course:
    """検査してから保存する。空の上書きは持たない（雛形のままに戻す）。"""
    validate(course, overrides, profiles_dir, registry)
    updated = course.model_copy(update={"grading_overrides": overrides})
    with database.unit_of_work() as uow:
        uow.identity.save_course(updated)
        uow.commit()
    return updated


def try_settings(
    database: Database,
    course: Course,
    overrides: dict,
    *,
    profiles_dir: Path,
    registry: EvaluatorRegistry,
) -> TrialResult:
    """このコースの課題を 1 件、参照解答で採点してみる。**保存しない。**

    参照解答とテストケースを持つ課題を探して走らせる。見つからなければ
    そう言って断る ── 試せないことを黙って「成功」にすると、確かめた
    つもりで壊れた設定が入る。
    """
    profile = validate(course, overrides, profiles_dir, registry)
    version, task = _sample(database, course)
    if version is None:
        raise AdminError(
            "試せる課題がありません（参照解答とテストケースを持つ課題が要ります）。"
            "設定は保存できますが、実際の採点で確かめてください。"
        )

    pipeline = GradingPipeline(registry, profile)
    source = (version.reference_solution or "").encode("utf-8")
    try:
        run = pipeline.run(version, _stub_submission(version), lambda _artifact: source)
    except Exception as exc:
        raise AdminError(f"試走に失敗しました: {exc}") from exc

    unscored = tuple(str(kc) for kc in run.unscored_criteria)
    return TrialResult(
        task=task,
        score_ratio=run.score_ratio,
        unscored=unscored,
        message=(
            f"{task.title}：参照解答で {run.score_ratio * 100:.0f}%"
            + ("（採点できなかった観点があります）" if unscored else "")
        ),
    )


def _sample(database: Database, course: Course) -> tuple[TaskVersion | None, object]:
    """試走に使える課題を 1 件選ぶ。参照解答とテストケースの両方が要る。"""
    with database.unit_of_work() as uow:
        for task in uow.tasks.list_for_course(course.id):
            version = uow.tasks.latest_version(task.id)
            if version is not None and version.reference_solution and version.test_cases:
                return version, task
    return None, None


def _stub_submission(version: TaskVersion):
    """試走のための仮の提出。**保存しない。**"""
    from datetime import UTC, datetime

    from aijudge_core import Artifact, ArtifactKind, ArtifactRole, Submission, SubmissionState
    from aijudge_core.ids import ArtifactId, SubmissionId, UserId, new_id

    submission_id = SubmissionId(new_id("sub"))
    now = datetime.now(UTC)
    return Submission(
        id=submission_id,
        task_version_id=version.id,
        learner_id=UserId(new_id("usr")),
        state=SubmissionState.SUBMITTED,
        artifacts=(
            Artifact(
                id=ArtifactId(new_id("art")),
                submission_id=submission_id,
                role=ArtifactRole.ORIGINAL,
                kind=ArtifactKind.CODE,
                filename="reference",
                storage_key=f"trial/{submission_id}",
                content_hash="sha256:trial",
                byte_size=len(version.reference_solution or ""),
                created_at=now,
            ),
        ),
        created_at=now,
        submitted_at=now,
    )


__all__ = ["TrialResult", "save", "template_of", "try_settings", "validate"]
