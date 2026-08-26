"""教員採点済みデータ（ゴールデンセット）の読み込み。

.. warning::

   **ゴールデンセットはリポジトリに入れない。**
   学習者の提出物と教員の採点は個人情報であり、これを git に載せると
   取り消せない。既定の場所はリポジトリ外（`~/.aijudge/golden`）で、
   `AIJUDGE_GOLDEN_DIR` で切り替える。
   リポジトリ内の `evals/golden/` は形式の例（合成データ）専用。

ディレクトリの形:

    <golden_dir>/
      cs_intro_c/                     科目プロファイル名
        prog2-2025-ex06-p3/           課題（Sharif Judge の問題ディレクトリ名）
          task/                       課題定義（desc.md, in/, out/, *.c）
          marks/
            s001.yaml                 1 提出ぶんの教員採点
            s001.c                    その提出物
            ...

`marks/*.yaml` の形:

    submission: s001.c
    marks:
      correctness: 3
      readability: 2
    marker: instructor-a
    marked_at: 2026-04-15
    blind: true          # AI の結果を見ずに採点したか
    notes: 変数名は良いが初期化が読みにくい
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

ENV_GOLDEN_DIR = "AIJUDGE_GOLDEN_DIR"
DEFAULT_GOLDEN_DIR = Path.home() / ".aijudge" / "golden"


def golden_root() -> Path:
    return Path(os.environ.get(ENV_GOLDEN_DIR, DEFAULT_GOLDEN_DIR)).expanduser()


class GoldenMark(BaseModel):
    """1 提出に対する教員採点。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    submission: str = Field(min_length=1)
    marks: dict[str, int] = Field(min_length=1)
    marker: str = Field(min_length=1)
    marked_at: date | None = None
    # AI の採点結果を見ずに付けたか。見て付けた採点は正解データにならない
    # （AI に引きずられるため）。κ の算出では blind のみを既定で使う。
    blind: bool = True
    notes: str | None = None


class GoldenItem(BaseModel):
    """採点対象 1 件。提出物の中身とその正解。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject_profile: str = Field(min_length=1)
    task_name: str = Field(min_length=1)
    task_dir: Path
    source_path: Path
    mark: GoldenMark

    @property
    def key(self) -> str:
        return f"{self.task_name}/{self.mark.submission}"


class GoldenSetError(Exception):
    """ゴールデンセットの構成が壊れている。"""


def _load_mark(path: Path) -> GoldenMark:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise GoldenSetError(f"{path} does not contain a mapping")
    try:
        return GoldenMark.model_validate(data)
    except Exception as exc:
        raise GoldenSetError(f"{path}: {exc}") from exc


def iter_golden(root: Path, subject_profile: str | None = None) -> Iterator[GoldenItem]:
    """ゴールデンセットを走査する。壊れた項目は黙って飛ばさず例外にする。

    黙って飛ばすと標本数が減ったことに気づかないまま κ を見ることになる。
    """
    if not root.is_dir():
        return

    for subject_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if subject_profile is not None and subject_dir.name != subject_profile:
            continue
        for task_dir in sorted(p for p in subject_dir.iterdir() if p.is_dir()):
            definition = task_dir / "task"
            marks_dir = task_dir / "marks"
            if not definition.is_dir():
                raise GoldenSetError(f"{task_dir} has no task/ directory")
            if not marks_dir.is_dir():
                raise GoldenSetError(f"{task_dir} has no marks/ directory")

            for mark_path in sorted(marks_dir.glob("*.yaml")):
                mark = _load_mark(mark_path)
                source = marks_dir / mark.submission
                if not source.is_file():
                    raise GoldenSetError(
                        f"{mark_path} refers to {mark.submission!r}, which is not there"
                    )
                yield GoldenItem(
                    subject_profile=subject_dir.name,
                    task_name=task_dir.name,
                    task_dir=definition,
                    source_path=source,
                    mark=mark,
                )


def load_golden(
    root: Path, subject_profile: str | None = None, *, blind_only: bool = True
) -> tuple[GoldenItem, ...]:
    """ゴールデンセットを読む。

    既定では blind な採点（AI の結果を見ずに付けたもの）だけを返す。
    AI の出力を見てから付けた採点は正解データにならない。
    """
    items = tuple(iter_golden(root, subject_profile))
    if blind_only:
        items = tuple(item for item in items if item.mark.blind)
    return items
