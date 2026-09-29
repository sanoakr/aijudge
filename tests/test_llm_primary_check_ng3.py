"""フォールバック不能（NG3）は連続したときだけ通知する（2026-09-29）。

従系 LLM の OS アップデートで NG3 が 1 回出ただけでメールが飛んでいた。主系が生きて
いれば採点に影響しないので、NG3 は 2 回続いてから通知する。NG1（主系停止）・NG2
（両系停止）は即時のまま。実際に `deploy/aijudge-llm-primary-check.sh` を動かす。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "aijudge-llm-primary-check.sh"

LABELS = {
    1: "NG1 (primary down, fallback serving)",
    2: "NG2 (both down)",
    3: "NG3 (fallback down, primary serving)",
}

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash が要る")


class Harness:
    """判定（終了コード）を差し替えて、通知メールの件名を集める。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.rc_file = root / "rc"
        self.mails = root / "mails"
        self.state = root / "state"
        self.streak = root / "streak"
        # python の代わりに「rc ファイルの値で終了する」スクリプトを使う
        fake_py = root / "fake-python"
        fake_py.write_text(f'#!/usr/bin/env bash\nexit "$(cat {self.rc_file})"\n')
        fake_py.chmod(0o755)
        notify = root / "notify"
        notify.write_text(f'#!/usr/bin/env bash\necho "$1" >> {self.mails}\ncat >/dev/null\n')
        notify.chmod(0o755)
        self.env = {
            **os.environ,
            "AIJUDGE_LLM_CHECK_STATE": str(self.state),
            "AIJUDGE_LLM_CHECK_STREAK": str(self.streak),
            "AIJUDGE_LLM_CHECK_PY": str(fake_py),
            "AIJUDGE_LLM_CHECK_SCRIPT": "unused",
            "AIJUDGE_LLM_CHECK_NOTIFY": str(notify),
            "AIJUDGE_LLM_CHECK_DIR": str(root),
        }

    def run(self, rc: int) -> None:
        self.rc_file.write_text(str(rc))
        done = subprocess.run(["bash", str(SCRIPT)], env=self.env, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr  # タイマーを failed にしない

    def subjects(self) -> list[str]:
        return self.mails.read_text().splitlines() if self.mails.exists() else []


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_single_ng3_is_not_notified(harness: Harness) -> None:
    harness.run(0)  # 初回（UNKNOWN -> OK）
    before = len(harness.subjects())
    harness.run(3)
    assert len(harness.subjects()) == before


def test_ng3_recovering_before_confirmation_sends_nothing(harness: Harness) -> None:
    harness.run(0)
    before = len(harness.subjects())
    harness.run(3)
    harness.run(0)
    assert len(harness.subjects()) == before


def test_second_consecutive_ng3_is_notified_once_and_recovery_too(harness: Harness) -> None:
    harness.run(0)
    before = len(harness.subjects())
    harness.run(3)
    harness.run(3)
    harness.run(3)  # 3 回目は同じ状態なので増えない
    assert harness.subjects()[before:] == [f"[{_short_host()}] LLM primary OK -> {LABELS[3]}"]
    harness.run(0)
    assert harness.subjects()[-1].endswith("NG3 -> OK")


def test_ng3_streak_restarts_after_a_good_check(harness: Harness) -> None:
    harness.run(0)
    before = len(harness.subjects())
    for rc in (3, 0, 3, 0, 3):
        harness.run(rc)
    assert len(harness.subjects()) == before


@pytest.mark.parametrize("rc", [1, 2])
def test_primary_and_both_down_are_notified_immediately(harness: Harness, rc: int) -> None:
    harness.run(0)
    before = len(harness.subjects())
    harness.run(rc)
    assert harness.subjects()[before:] == [f"[{_short_host()}] LLM primary OK -> {LABELS[rc]}"]


def test_worsening_from_pending_ng3_to_ng1_is_immediate(harness: Harness) -> None:
    harness.run(0)
    before = len(harness.subjects())
    harness.run(3)  # 保留中
    harness.run(1)
    assert harness.subjects()[before:] == [f"[{_short_host()}] LLM primary OK -> {LABELS[1]}"]


def _short_host() -> str:
    return subprocess.run(["hostname", "-s"], capture_output=True, text=True).stdout.strip()
