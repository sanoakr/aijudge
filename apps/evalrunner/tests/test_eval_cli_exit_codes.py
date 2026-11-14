"""測定の CLI の終了コードを確かめる（#704）。

**終了コード 2 は「測れていない」であって合格ではない**（ADR 0005）。CI は 2 を
warning として扱い、0 なら「passed」と出す。既存のテストで 2 を見ていたのは
観測が 0 件のときの早期 return だけで、観測はあるが標本が足りずに判定が
`NOT_MEASURED` になる経路（判定から終了コードへの対応表）は返り値を見て
いなかった。対応表で `NOT_MEASURED` を 0 にしても、どのテストも落ちなかった。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from aijudge_analytics import Verdict
from aijudge_evalrunner import load_observations, measure
from aijudge_evalrunner.cli import EXIT_NOT_MEASURED, load_gates, main
from aijudge_observation import Observation

REPO_ROOT = Path(__file__).resolve().parents[3]
GATES_PATH = REPO_ROOT / "evals" / "gates.yaml"
SUBJECT = "cs_lang_c_intro"
LEVELS = (0, 1, 2, 3)


def _unmarked(submission: str) -> Observation:
    """人がまだ付けていない観測。数えられる標本にならない。"""
    return Observation(
        subject_profile=SUBJECT,
        task_name="example-task",
        submission=submission,
        criterion_code="readability",
        levels=LEVELS,
        machine_level=1,
        machine_confidence=1.0,
        conclusive=False,
        unscored=False,
        grading_run_id="grn_test",
        human_level=None,
        blind=False,
        marker=None,
        auto_confirmed=True,
        machine_corrected=None,
        observed_at=datetime(2026, 8, 28, tzinfo=UTC),
    )


def test_an_unmeasurable_verdict_exits_2_not_0(tmp_path: Path) -> None:
    """観測はあるが標本が足りないときも 2 で終わる（対応表を通る経路）。"""
    folder = tmp_path / SUBJECT / "example-task" / "observations"
    folder.mkdir(parents=True)
    (folder / "s001.json").write_text(
        json.dumps([_unmarked("s001.c").model_dump(mode="json")], ensure_ascii=False),
        encoding="utf-8",
    )

    # 前提: 観測 0 件の早期 return ではなく、判定まで進んで NOT_MEASURED になる。
    observations = load_observations(tmp_path, SUBJECT)
    assert observations
    report = measure(observations, gates=load_gates(GATES_PATH), subject_profile=SUBJECT)
    assert report.verdict is Verdict.NOT_MEASURED

    assert main(["--golden", str(tmp_path), "--gates", str(GATES_PATH)]) == EXIT_NOT_MEASURED
