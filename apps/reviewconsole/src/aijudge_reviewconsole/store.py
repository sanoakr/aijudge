"""レビュー作業の保存先（ファイル版）。

DB はまだ無い（PoC-0 の範囲）。ここはインターフェースを固定するための
最小実装で、PostgreSQL に載せ替えるときに置き換わる。
ファイルで持つあいだも、GradingRun を上書きしない規則（P8）は守る。

置き場所はゴールデンセットの根と同じにしてある。

    <golden_root>/
      cs_intro_c/
        prog2-2025-ex06-p3/
          task/                 課題定義
          marks/
            s001.c              提出物（yaml が無ければ未レビュー = 待ち行列）
            s001.yaml           教員の blind 採点（レビュー完了で書かれる）
          runs/
            s001.json           採点結果（追記のみ・上書きしない）
          reviews/
            s001.json           確定した採点（HumanReview 相当）

「待ち行列 = yaml の無い提出物」にしてあるので、レビューが進むほど
ゴールデンセットが貯まる。測定用のデータを別途集める作業が要らない。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from aijudge_core import GradingRun

# 提出物として扱う拡張子。ここに無いものは待ち行列に出さない。
SUBMISSION_SUFFIXES = frozenset({".c", ".py", ".java", ".tex", ".md"})


class QueueEntry(BaseModel):
    """レビュー待ち、またはレビュー済みの 1 件。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_profile: str
    task_name: str
    submission: str
    task_dir: Path
    source_path: Path
    mark_path: Path
    reviewed: bool

    @property
    def id(self) -> str:
        return f"{self.subject_profile}/{self.task_name}/{self.submission}"


class FinalDecision(BaseModel):
    """教員が確定させた採点。GradingRun は書き換えず、これを別に残す（P8）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    grading_run_id: str
    grader: str = Field(min_length=1)
    # blind 段階で教員が付けた段階（AI を見る前）。
    blind_levels: dict[str, int] = Field(min_length=1)
    # AI を見たうえでの最終段階。blind から変えたなら、変えた理由が要る。
    final_levels: dict[str, int] = Field(min_length=1)
    changed_after_seeing_ai: bool = False
    comment: str | None = None
    decided_at: datetime

    @property
    def agreed_with_ai(self) -> bool:
        return not self.changed_after_seeing_ai


class ReviewStore:
    """ファイル配置の読み書き。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    # -- 待ち行列 ----------------------------------------------------------

    def queue(self, subject_profile: str | None = None) -> tuple[QueueEntry, ...]:
        """レビュー対象を列挙する。未レビューが先、その中では名前順。"""
        entries: list[QueueEntry] = []
        if not self.root.is_dir():
            return ()

        for subject_dir in sorted(p for p in self.root.iterdir() if p.is_dir()):
            if subject_profile is not None and subject_dir.name != subject_profile:
                continue
            for task_dir in sorted(p for p in subject_dir.iterdir() if p.is_dir()):
                marks = task_dir / "marks"
                definition = task_dir / "task"
                if not marks.is_dir() or not definition.is_dir():
                    continue
                for source in sorted(marks.iterdir()):
                    if source.suffix.lower() not in SUBMISSION_SUFFIXES:
                        continue
                    mark_path = source.with_suffix(".yaml")
                    entries.append(
                        QueueEntry(
                            subject_profile=subject_dir.name,
                            task_name=task_dir.name,
                            submission=source.name,
                            task_dir=definition,
                            source_path=source,
                            mark_path=mark_path,
                            reviewed=mark_path.is_file(),
                        )
                    )
        return tuple(sorted(entries, key=lambda e: (e.reviewed, e.id)))

    def find(self, entry_id: str) -> QueueEntry | None:
        return next((entry for entry in self.queue() if entry.id == entry_id), None)

    # -- 採点結果 ----------------------------------------------------------

    def _run_path(self, entry: QueueEntry) -> Path:
        return entry.task_dir.parent / "runs" / f"{Path(entry.submission).stem}.json"

    def save_run(self, entry: QueueEntry, run: GradingRun) -> None:
        """採点結果を保存する。既にあれば別名で残し、上書きしない（P8）。"""
        path = self._run_path(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            path.rename(path.with_name(f"{path.stem}.{stamp}.json"))
        path.write_text(
            json.dumps(run.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_run(self, entry: QueueEntry) -> GradingRun | None:
        path = self._run_path(entry)
        if not path.is_file():
            return None
        return GradingRun.model_validate_json(path.read_text(encoding="utf-8"))

    # -- 確定した採点 ------------------------------------------------------

    def _decision_path(self, entry: QueueEntry) -> Path:
        return entry.task_dir.parent / "reviews" / f"{Path(entry.submission).stem}.json"

    def save_decision(self, entry: QueueEntry, decision: FinalDecision) -> None:
        path = self._decision_path(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(decision.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_decision(self, entry: QueueEntry) -> FinalDecision | None:
        path = self._decision_path(entry)
        if not path.is_file():
            return None
        return FinalDecision.model_validate_json(path.read_text(encoding="utf-8"))

    # -- ゴールデンセット --------------------------------------------------

    def save_blind_mark(
        self,
        entry: QueueEntry,
        *,
        levels: dict[str, int],
        marker: str,
        notes: str | None = None,
    ) -> None:
        """blind 採点をゴールデンセットとして書く。

        **必ず blind 段階の段階を書く。** AI を見たあとに変えた段階を
        書いてしまうと、その採点は AI に引きずられており正解データにならない
        （ADR 0005）。最終成績は FinalDecision の側に残る。
        """
        payload: dict[str, object] = {
            "submission": entry.submission,
            "marks": dict(levels),
            "marker": marker,
            "marked_at": datetime.now(UTC).date().isoformat(),
            "blind": True,
        }
        if notes:
            payload["notes"] = notes
        entry.mark_path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
