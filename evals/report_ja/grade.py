"""新しいルーブリックで 19 件を採点し直す。

**採点そのものは `GradingPipeline` にやらせる。** ここで別の呼び方をすると、
測ったものが本番の採点と違うものになる。このスクリプトがやるのは、DB を
立てずに課題版と提出をその場で組み立てて渡すことだけ。

結果は runs/<name>.json に落とす。測定（measure.py）は落ちた結果を読む。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path.home() / "dev/aijudge"))
sys.path.insert(0, str(Path(__file__).parent))

import rubric

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    GradingPhase,
    Provenance,
    ReviewPolicy,
    ReviewState,
    RubricCriterion,
    RubricLevel,
    Submission,
    SubmissionState,
    TaskVersion,
)
from aijudge_core.ids import (
    ArtifactId,
    CriterionId,
    SubmissionId,
    TaskId,
    TaskVersionId,
    UserId,
)
from aijudge_grading import GradingPipeline, SubjectProfile, default_normalizers, default_registry
from aijudge_grading.profile import InputPolicy, MeasurementPolicy

HERE = Path(__file__).parent
REPORTS = Path.home() / "pCloud Drive/Agent Projects/aiJudge設計検討/network-report2025"


def build_task_version() -> TaskVersion:
    """rubric.py の宣言から課題版を組む。"""
    criteria = []
    for spec in rubric.CRITERIA:
        criteria.append(
            RubricCriterion(
                # 観点 ID を内容から決めておく。走らせ直しても同じ ID になり、
                # 結果の突き合わせが名前ではなく ID でできる。
                id=CriterionId(
                    "crt_" + hashlib.sha256(spec["code"].encode()).hexdigest()[:32]
                ),
                code=spec["code"],
                title=spec["title"],
                description=spec["description"],
                weight=spec["weight"],
                evaluator_id=spec.get("evaluator"),
                levels=tuple(RubricLevel(**level) for level in spec["levels"]),
            )
        )
    return TaskVersion(
        id=TaskVersionId("tsv_" + "0" * 32),
        task_id=TaskId("tsk_" + "0" * 32),
        version=1,
        subject_profile="report_ja",
        statement=rubric.STATEMENT,
        criteria=tuple(criteria),
        max_score=20.0,
        allow_handwriting=False,
        provenance=Provenance(
            authored_by=UserId("usr_" + "0" * 32), review_state=ReviewState.APPROVED
        ),
        created_at=datetime.now(UTC),
    )


def build_profile(samples: int) -> SubjectProfile:
    options = dict(rubric.EVALUATOR_OPTIONS)
    options["rubric_ai_judge"] = {"samples": samples}
    return SubjectProfile(
        name="report_ja",
        description="実験レポート（教員の採点表に合わせたルーブリック）",
        input=InputPolicy(allow_handwriting=False),
        normalizers=("document_text",),
        deterministic=("report_structure",),
        ai_evaluators=("rubric_ai_judge",),
        evaluator_options=options,
        # **確信度は自己一貫性から作られるので、サンプル数が閾値の意味を決める。**
        # samples=2 では確信度が 1.0 か 0.5 にしかならず、どこに閾値を置いても
        # 「1 つでも割れたら人間」になる。3 本なら 2 対 1 の多数決が表現でき、
        # 0.6 を境にすれば「多数決が付いた」ものは自動で通る。
        review_policy=ReviewPolicy(
            confidence_below=0.6,
            boundary_score=0.6,
            boundary_margin=0.05,
        ),
        timeout_seconds=300.0,
        measurement=MeasurementPolicy(blind_sample_rate=0.2),
    )


def build_submission(login: str, path: Path) -> tuple[Submission, bytes]:
    payload = path.read_bytes()
    submission_id = SubmissionId("sub_" + hashlib.sha256(login.encode()).hexdigest()[:32])
    kind = ArtifactKind.PDF if path.suffix.lower() == ".pdf" else ArtifactKind.DOCX
    artifact = Artifact(
        id=ArtifactId("art_" + hashlib.sha256(path.name.encode()).hexdigest()[:32]),
        submission_id=submission_id,
        role=ArtifactRole.ORIGINAL,
        kind=kind,
        filename=path.name,
        storage_key=path.name,
        content_hash="sha256:" + hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
        created_at=datetime.now(UTC),
    )
    now = datetime.now(UTC)
    return (
        Submission(
            id=submission_id,
            task_version_id=TaskVersionId("tsv_" + "0" * 32),
            learner_id=UserId("usr_" + hashlib.sha256(login.encode()).hexdigest()[:32]),
            state=SubmissionState.SUBMITTED,
            artifacts=(artifact,),
            created_at=now,
            submitted_at=now,
        ),
        payload,
    )


def grade_one(pipeline, task_version, login, filename):
    submission, payload = build_submission(login, REPORTS / filename)
    # ContentLoader は Artifact を受け取る（ID ではない）。
    def load(artifact):
        return payload

    started = time.monotonic()
    base = pipeline.run(task_version, submission, load, phase=GradingPhase.DETERMINISTIC)
    run = pipeline.run(task_version, submission, load, phase=GradingPhase.AI, base=base)
    by_id = {c.id: c for c in task_version.criteria}
    return {
        "login": login,
        "score_ratio": run.score_ratio,
        "routing": run.routing.value,
        "seconds": round(time.monotonic() - started, 1),
        "unscored": [by_id[cid].code for cid in run.unscored_criteria if cid in by_id],
        "scores": {
            by_id[s.criterion_id].code: {
                "level": s.level,
                "confidence": s.confidence,
                "kind": s.kind.value,
                "conclusive": s.conclusive,
                "rationale": s.rationale,
            }
            for s in run.criterion_scores
            if s.criterion_id in by_id
        },
        "evaluators": [
            {"id": r.evaluator_id, "status": r.status.value, "error": r.error}
            for r in run.evaluator_results
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="run1", help="結果の保存名")
    parser.add_argument("--samples", type=int, default=2, help="自己一貫性のサンプル数")
    parser.add_argument("--workers", type=int, default=5, help="同時に走らせる提出の数")
    parser.add_argument("--only", default=None, help="この学生だけ")
    args = parser.parse_args()

    index = json.loads((HERE / "index.json").read_text(encoding="utf-8"))
    if args.only:
        index = [r for r in index if r["login"] == args.only.upper()]
    index.sort(key=lambda r: r["login"])

    task_version = build_task_version()
    pipeline = GradingPipeline(
        default_registry(), build_profile(args.samples), default_normalizers()
    )

    started = time.monotonic()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(grade_one, pipeline, task_version, r["login"], r["filename"]): r["login"]
            for r in index
        }
        for future in concurrent.futures.as_completed(futures):
            login = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # 1 件で全体を止めない
                print(f"  失敗 {login}: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            results.append(result)
            unscored = f"  未採点 {result['unscored']}" if result["unscored"] else ""
            print(
                f"  {result['login']:9} {result['score_ratio']*100:5.1f}%"
                f"  {result['routing']:16} {result['seconds']:6.1f} 秒{unscored}",
                flush=True,
            )

    results.sort(key=lambda r: r["login"])
    out = HERE / "runs"
    out.mkdir(exist_ok=True)
    (out / f"{args.name}.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n{len(results)} 件 / {time.monotonic() - started:.0f} 秒 → runs/{args.name}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
