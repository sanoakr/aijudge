"""測定が読む観測レコードの場所は、ワーカー・コンソールが書く場所と同じ（#702）。

読む側だけが別の既定（`~/.aijudge/golden`）を持ち、書く側の `AIJUDGE_OBSERVATION_DIR` だけを
設定した配備では、測定が空の場所を読んで「観測レコードがありません」を返し続けた。
"""

from __future__ import annotations

from pathlib import Path

from aijudge_evalrunner.observations import ENV_GOLDEN_DIR, ENV_OBSERVATION_DIR, golden_root
from aijudge_grader.cli import DEFAULT_OBSERVATION_DIR


def test_the_default_is_where_the_writers_write(monkeypatch) -> None:
    monkeypatch.delenv(ENV_GOLDEN_DIR, raising=False)
    monkeypatch.delenv(ENV_OBSERVATION_DIR, raising=False)

    assert golden_root() == DEFAULT_OBSERVATION_DIR


def test_the_writers_variable_alone_is_enough(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(ENV_GOLDEN_DIR, raising=False)
    monkeypatch.setenv(ENV_OBSERVATION_DIR, str(tmp_path / "obs"))

    assert golden_root() == tmp_path / "obs"


def test_an_explicit_golden_dir_still_wins(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ENV_GOLDEN_DIR, str(tmp_path / "gold"))
    monkeypatch.setenv(ENV_OBSERVATION_DIR, str(tmp_path / "obs"))

    assert golden_root() == tmp_path / "gold"
