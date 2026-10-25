"""通知メールに、影響とやることが書いてある（2026-10-25）。

unit 名と journal、確認のコマンド 1 行だけのメールでは、読んだ人が「急ぐのか」
「何を打てば直るのか」を毎回 deploy/README.md から探すことになった。固定したいのは 3 つ。

全 unit に案内   失敗を通知する unit はどれも、汎用の文ではなく専用の案内を持つ
障害と回復      障害のメールには「やること」、回復のメールには「対応は不要」が入る
配る            案内のスクリプトは、ほかの検査スクリプトと同じ経路で配られ、ずれを検査される
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY = REPO_ROOT / "deploy"
GUIDE = DEPLOY / "aijudge-failure-guide"
SECTIONS = ("== 何が起きたか ==", "== 影響 ==", "== やること（運用機で） ==", "== 直ったら ==")
#: 案内が無い unit に出る文。**ここに落ちる unit を作らない。**
FALLBACK = "まだここに書かれていません"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash が要る")


def _notified_units() -> list[str]:
    """`OnFailure=aijudge-notify@` を持つ unit（テンプレートは例のインスタンスで）。"""
    units = []
    for path in sorted((DEPLOY / "systemd").glob("*.service")):
        # 行頭の設定だけを見る（aijudge-notify@ 自身のコメントにも同じ字がある）。
        if not re.search(r"^OnFailure=aijudge-notify@", path.read_text(encoding="utf-8"), re.M):
            continue
        name = path.name.replace("@.service", "@1.service")
        units.append(name)
    return units


def _guide(unit: str) -> str:
    # **C ロケールで流す。** systemd の下はそうで、全角文字が変数名の続きに読まれて
    # 落ちたことがある（`$UNIT（` → unbound variable）。
    done = subprocess.run(
        ["bash", str(GUIDE), unit],
        env={**os.environ, "LC_ALL": "C"},
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.parametrize("unit", _notified_units())
def test_every_notified_unit_has_its_own_guide(unit: str) -> None:
    body = _guide(unit)
    for section in SECTIONS:
        assert section in body, f"{unit}: {section} が無い"
    assert FALLBACK not in body, f"{unit} の案内を deploy/aijudge-failure-guide に足すこと"
    assert re.search(r"^1\. ", body, re.M), f"{unit}: やることが 1 つも無い"
    assert "**" not in body, "メールはプレーンテキスト（強調の記号がそのまま見える）"


def test_an_unknown_unit_still_gets_the_basic_steps() -> None:
    body = _guide("aijudge-something-new.service")
    assert FALLBACK in body
    assert "journalctl -u aijudge-something-new.service" in body


def test_the_failure_notice_puts_the_guide_first() -> None:
    unit = (DEPLOY / "systemd" / "aijudge-notify@.service").read_text(encoding="utf-8")
    start = unit.split("ExecStart=", 1)[1]
    assert start.index("aijudge-failure-guide") < start.index("systemctl status")
    # 案内が配られる前の機械でも、状態と journal は送る。
    assert '|| echo "unit: %i"' in start


def _notify_stub(root: Path) -> tuple[Path, Path]:
    mails = root / "mails"
    notify = root / "notify"
    notify.write_text(
        f'#!/usr/bin/env bash\n{{ echo "SUBJECT $1"; cat; echo "----"; }} >> {mails}\n'
    )
    notify.chmod(0o755)
    return notify, mails


def test_the_llm_mail_says_what_to_do_and_when_nothing_is_needed(tmp_path: Path) -> None:
    notify, mails = _notify_stub(tmp_path)
    rc = tmp_path / "rc"
    fake_py = tmp_path / "fake-python"
    fake_py.write_text(f'#!/usr/bin/env bash\nexit "$(cat {rc})"\n')
    fake_py.chmod(0o755)
    env = {
        **os.environ,
        "AIJUDGE_LLM_CHECK_STATE": str(tmp_path / "state"),
        "AIJUDGE_LLM_CHECK_STREAK": str(tmp_path / "streak"),
        "AIJUDGE_LLM_CHECK_PY": str(fake_py),
        "AIJUDGE_LLM_CHECK_SCRIPT": "unused",
        "AIJUDGE_LLM_CHECK_NOTIFY": str(notify),
        "AIJUDGE_LLM_CHECK_DIR": str(tmp_path),
        "AIJUDGE_LLM_BASE_URL": "http://primary.invalid:11434",
    }
    for value in (0, 1, 0):
        rc.write_text(str(value))
        subprocess.run(["bash", str(DEPLOY / "aijudge-llm-primary-check.sh")], env=env, check=True)
    sent = mails.read_text().split("----\n")
    down = next(m for m in sent if "NG1" in m.splitlines()[0])
    assert "== やること ==" in down
    assert "curl -sS --max-time 10 http://primary.invalid:11434/api/tags" in down
    recovered = sent[-2]
    assert "-> OK" in recovered.splitlines()[0]
    assert "対応は不要" in recovered


def test_the_web_mail_says_what_to_do(tmp_path: Path) -> None:
    notify, mails = _notify_stub(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    code = tmp_path / "code"
    (bin_dir / "curl").write_text(f"#!/usr/bin/env bash\ncat {code}\n")
    (bin_dir / "curl").chmod(0o755)
    script = (DEPLOY / "aijudge-http-check.sh").read_text(encoding="utf-8")
    # 運用機の固定パスを、テストの置き場所へ差し替えた写しを走らせる。
    copy = tmp_path / "http-check.sh"
    copy.write_text(
        script.replace("/var/lib/aijudge/http-check.state", str(tmp_path / "state")).replace(
            "/usr/local/sbin/aijudge-notify", str(notify)
        )
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AIJUDGE_LEARNER_URL": "https://learner.invalid",
    }
    for value in ("200", "502", "200"):
        code.write_text(value)
        subprocess.run(["bash", str(copy)], env=env, check=True, capture_output=True)
    sent = mails.read_text().split("----\n")
    down = next(m for m in sent if "-> NG" in m.splitlines()[0])
    assert "== やること（運用機で） ==" in down
    assert "sudo systemctl restart aijudge-web" in down
    assert "対応は不要" in sent[-2]


NOTIFYING = (
    "aijudge-queue-check.sh",
    "aijudge-storage-check.sh",
    "aijudge-http-check.sh",
    "aijudge-llm-primary-check.sh",
    "aijudge-config-check.sh",
    "aijudge-purge-preview.sh",
)


@pytest.mark.parametrize("name", NOTIFYING)
def test_every_check_mail_carries_the_impact(name: str) -> None:
    source = (DEPLOY / name).read_text(encoding="utf-8")
    assert "aijudge-notify" in source or "NOTIFY" in source
    assert "== 影響 ==" in source, f"{name} のメールに影響とやることを書くこと"


def test_the_guide_is_shipped_and_checked_like_the_other_scripts() -> None:
    for name in ("install-units.sh", "bootstrap.sh", "aijudge-config-check.sh"):
        text = (DEPLOY / name).read_text(encoding="utf-8")
        assert "aijudge-failure-guide" in text, f"{name} が案内のスクリプトを扱っていない"
    assert os.access(GUIDE, os.X_OK)
