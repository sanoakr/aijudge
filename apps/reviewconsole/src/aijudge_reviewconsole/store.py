"""レビュー作業の保存先（ファイル版）。

DB はまだ無い（Phase 0 の残作業）。ここはインターフェースを固定するための
最小実装で、PostgreSQL に載せ替えるときに置き換わる。
ファイルで持つあいだも、GradingRun を上書きしない規則（P8）は守る。

    <root>/
      cs_intro_c/
        prog2-2025-ex06-p3/
          task/                 課題定義
          marks/
            s001.c              提出物
            s001.yaml           教員の blind 採点（blind 抽出対象のみ）
          runs/
            s001.json           採点結果（追記のみ・上書きしない）
          reviews/
            s001.json           確定した採点（HumanReview 相当）
          observations/
            s001.json           測定用の観測レコード（投影・書き直し可）

**採点はレビューとは独立に走る**（`worker.py`）。レビューは採点の前提条件では
なく、レビュー可能なのは採点が届いた提出だけ（ADR 0007）。

`observations/` は投影であって記録の正本ではない。正本は `runs/`（不変）と
`reviews/` で、そちらは上書きしない。観測は後から情報が付いた時点で書き直す。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from aijudge_core import GradingRun
from aijudge_observation import Observation

# 提出物として扱う拡張子。ここに無いものは待ち行列に出さない。
SUBMISSION_SUFFIXES = frozenset({".c", ".py", ".java", ".tex", ".md"})

# 抽出の判定に使う桁数。sha256 の先頭 8 バイトを [0,1) に写す。
_SAMPLE_BITS = 64


def is_blind_sample(entry_id: str, rate: float) -> bool:
    """この提出を blind 採点の対象にするか。

    提出 ID のハッシュで決める。**決定的**（同じ提出は毎回同じ判定）かつ
    教員の選択が入らないことが要点。教員に選ばせると、難しい提出だけを
    blind にするといった選択バイアスが入り、一致度がその分だけ意味を失う。
    """
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    digest = hashlib.sha256(entry_id.encode("utf-8")).digest()
    bucket = int.from_bytes(digest[:8], "big") / (1 << _SAMPLE_BITS)
    return bucket < rate


class GoldenMark(BaseModel):
    """`marks/*.yaml` — 教員が AI を見る前に付けた段階。

    測定の正解データはこれだけ。AI の判定を見たあとの段階は
    `FinalDecision` の側に入り、ここには書かない（ADR 0005）。

    書く側と読む側で形が違うと、片方を直したときに気づけない。
    このモデルを両方で使う。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    submission: str = Field(min_length=1)
    marks: dict[str, int] = Field(min_length=1)
    marker: str = Field(min_length=1)
    marked_at: date | None = None
    # AI の採点結果を見ずに付けたか。偽なら一致度の標本にしない。
    blind: bool = True
    notes: str | None = None


class QueueEntry(BaseModel):
    """レビュー対象 1 件と、その進行状況。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_profile: str
    task_name: str
    submission: str
    task_dir: Path
    source_path: Path
    mark_path: Path
    # 採点が届いているか。届いていない提出はレビューできない。
    graded: bool = False
    # blind 採点が済んでいるか。
    marked: bool = False
    # 教員が確定させたか。
    decided: bool = False
    # blind 抽出の対象か。Console が科目プロファイルを見て埋める。
    blind_required: bool = False

    @property
    def id(self) -> str:
        return f"{self.subject_profile}/{self.task_name}/{self.submission}"

    @property
    def stem(self) -> str:
        return Path(self.submission).stem

    @property
    def needs_blind_mark(self) -> bool:
        """先に blind 採点を求めるか。抽出対象で、まだ付けていないとき。"""
        return self.blind_required and not self.marked

    @property
    def pending(self) -> bool:
        """まだ教員の確定を待っているか。"""
        return self.graded and not self.decided


class FinalDecision(BaseModel):
    """教員が確定させた採点。GradingRun は書き換えず、これを別に残す（P8）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    grading_run_id: str
    grader: str = Field(min_length=1)
    # blind 段階で教員が付けた段階（AI を見る前）。抽出対象外なら空。
    blind_levels: dict[str, int] = Field(default_factory=dict)
    # AI を見たうえでの最終段階。
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
        """レビュー対象を列挙する。未確定が先、その中では名前順。"""
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
                    stem = source.stem
                    entries.append(
                        QueueEntry(
                            subject_profile=subject_dir.name,
                            task_name=task_dir.name,
                            submission=source.name,
                            task_dir=definition,
                            source_path=source,
                            mark_path=source.with_suffix(".yaml"),
                            graded=(task_dir / "runs" / f"{stem}.json").is_file(),
                            marked=source.with_suffix(".yaml").is_file(),
                            decided=(task_dir / "reviews" / f"{stem}.json").is_file(),
                        )
                    )
        return tuple(sorted(entries, key=lambda e: (e.decided, e.id)))

    def find(self, entry_id: str) -> QueueEntry | None:
        return next((entry for entry in self.queue() if entry.id == entry_id), None)

    # -- 採点結果 ----------------------------------------------------------

    def _path(self, entry: QueueEntry, folder: str) -> Path:
        return entry.task_dir.parent / folder / f"{entry.stem}.json"

    def save_run(self, entry: QueueEntry, run: GradingRun) -> None:
        """採点結果を保存する。既にあれば別名で残し、上書きしない（P8）。"""
        path = self._path(entry, "runs")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            path.rename(path.with_name(f"{path.stem}.{stamp}.json"))
        _write_json(path, run.model_dump(mode="json"))

    def load_run(self, entry: QueueEntry) -> GradingRun | None:
        path = self._path(entry, "runs")
        if not path.is_file():
            return None
        return GradingRun.model_validate_json(path.read_text(encoding="utf-8"))

    # -- 確定した採点 ------------------------------------------------------

    def save_decision(self, entry: QueueEntry, decision: FinalDecision) -> None:
        path = self._path(entry, "reviews")
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, decision.model_dump(mode="json"))

    def load_decision(self, entry: QueueEntry) -> FinalDecision | None:
        path = self._path(entry, "reviews")
        if not path.is_file():
            return None
        return FinalDecision.model_validate_json(path.read_text(encoding="utf-8"))

    # -- blind 採点（測定用の正解データ）----------------------------------

    def save_blind_mark(
        self,
        entry: QueueEntry,
        *,
        levels: dict[str, int],
        marker: str,
        notes: str | None = None,
    ) -> None:
        """blind 採点を書く。

        **必ず blind 段階の段階を書く。** AI を見たあとに変えた段階を
        書いてしまうと、その採点は AI に引きずられており正解データにならない
        （ADR 0005）。最終成績は FinalDecision の側に残る。
        """
        mark = GoldenMark(
            submission=entry.submission,
            marks=dict(levels),
            marker=marker,
            marked_at=datetime.now(UTC).date(),
            blind=True,
            notes=notes,
        )
        payload = mark.model_dump(mode="json", exclude_none=True)
        entry.mark_path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    def load_blind_mark(self, entry: QueueEntry) -> GoldenMark | None:
        """blind 採点を読む。壊れていれば例外にする。

        黙って飛ばすと、標本が減ったことに気づかないまま κ を見ることになる
        （ADR 0005）。
        """
        if not entry.mark_path.is_file():
            return None
        data = yaml.safe_load(entry.mark_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{entry.mark_path} does not contain a mapping")
        return GoldenMark.model_validate(data)

    # -- 観測レコード（測定への唯一の受け渡し）----------------------------

    def save_observations(self, entry: QueueEntry, observations: tuple[Observation, ...]) -> None:
        """観測を書く。投影なので上書きしてよい（正本は runs/ と reviews/）。"""
        path = self._path(entry, "observations")
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, [item.model_dump(mode="json") for item in observations])

    def load_observations(self, entry: QueueEntry) -> tuple[Observation, ...]:
        path = self._path(entry, "observations")
        if not path.is_file():
            return ()
        data = json.loads(path.read_text(encoding="utf-8"))
        return tuple(Observation.model_validate(item) for item in data)

    def iter_observations(self, subject_profile: str | None = None) -> Iterator[Observation]:
        """全観測を走査する。測定側が読むのはこれだけ。"""
        for entry in self.queue(subject_profile):
            yield from self.load_observations(entry)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
