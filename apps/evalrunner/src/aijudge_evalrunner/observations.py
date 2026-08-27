"""観測レコードの読み込み。

測定側が触るのはこのファイル群だけ。採点結果（`runs/`）も教員採点
（`marks/`）も課題定義（`task/`）も読まない。読まないからこそ、測定に
採点の語彙が要らない（ADR 0007）。

    <root>/<subject_profile>/<task>/observations/<submission>.json

配置はレビュー側（`aijudge_reviewconsole.store`）が書いたもの。Phase 0 では
DB が無いためファイルで受け渡している。DB に載せ替えるときは、この関数が
クエリに置き換わる。

.. warning::

   観測レコードには学習者の提出名と教員の採点が含まれる。個人情報なので
   リポジトリに置かない。既定の場所はリポジトリ外（`~/.aijudge/golden`）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

from aijudge_analytics import Observation

ENV_GOLDEN_DIR = "AIJUDGE_GOLDEN_DIR"
DEFAULT_GOLDEN_DIR = Path.home() / ".aijudge" / "golden"

OBSERVATIONS_DIR = "observations"


def golden_root() -> Path:
    return Path(os.environ.get(ENV_GOLDEN_DIR, DEFAULT_GOLDEN_DIR)).expanduser()


class ObservationSetError(Exception):
    """観測レコードが壊れている。"""


def iter_observations(root: Path, subject_profile: str | None = None) -> Iterator[Observation]:
    """観測レコードを走査する。壊れた項目は黙って飛ばさず例外にする。

    黙って飛ばすと、標本が減ったことに気づかないまま κ を見ることになる
    （ADR 0005）。
    """
    if not root.is_dir():
        return

    for subject_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if subject_profile is not None and subject_dir.name != subject_profile:
            continue
        for task_dir in sorted(p for p in subject_dir.iterdir() if p.is_dir()):
            folder = task_dir / OBSERVATIONS_DIR
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.json")):
                yield from _load(path)


def _load(path: Path) -> tuple[Observation, ...]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ObservationSetError(f"{path}: {exc}") from exc
    if not isinstance(data, list):
        raise ObservationSetError(f"{path} does not contain a list of observations")
    try:
        return tuple(Observation.model_validate(item) for item in data)
    except Exception as exc:
        raise ObservationSetError(f"{path}: {exc}") from exc


def load_observations(root: Path, subject_profile: str | None = None) -> tuple[Observation, ...]:
    return tuple(iter_observations(root, subject_profile))
