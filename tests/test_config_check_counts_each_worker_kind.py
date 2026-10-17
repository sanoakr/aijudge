"""設定の検査は、ワーカーの種類ごとに本数を数える（2026-10-17）。

v1.52.0 で書き起こしのワーカー（#627）の本数を数える検査を足したとき、AI ワーカーの
本数を入れていた変数を同じ名前で上書きした。そのあとの「待ち時間の目安」と「接続数」は
AI ワーカーの本数として読むので、AI 4 本を 2 本と取り違え、初回の検査が
「AIJUDGE_AI_WORKERS=4 が実際の 2 本と違う」と誤って知らせた。

実際に `deploy/aijudge-config-check.sh` を動かす。`systemctl` と `psql` は替え玉にする。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "aijudge-config-check.sh"

AI_WORKERS = 4
TRANSCRIPTION_WORKERS = 2
WEB_WORKERS = 2

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash が要る")

# `list-units` は本数ぶんの行を返し、`show` は環境変数を返す。ほかは成功で返す。
FAKE_SYSTEMCTL = f"""#!/usr/bin/env bash
case "$1" in
  list-units)
    case "$2" in
      'aijudge-worker-ai@*.service') n={AI_WORKERS} ;;
      'aijudge-worker-transcribe@*.service') n={TRANSCRIPTION_WORKERS} ;;
      *) n=0 ;;
    esac
    for i in $(seq 1 "$n"); do echo "unit@$i.service loaded active running x"; done ;;
  show)
    case "$*" in
      *MainPID*) echo 0 ;;
      *) echo "AIJUDGE_AI_WORKERS={AI_WORKERS} AIJUDGE_WEB_WORKERS={WEB_WORKERS}" ;;
    esac ;;
esac
exit 0
"""


def _run(tmp_path: Path, max_connections: int) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fakes = {
        "systemctl": FAKE_SYSTEMCTL,
        "psql": f"#!/usr/bin/env bash\necho {max_connections}\n",
        # `sudo -u postgres psql` を失敗させ、替え玉の `psql` に落とす。
        "sudo": "#!/usr/bin/env bash\nexit 1\n",
    }
    for name, body in fakes.items():
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AIJUDGE_REPO_DIR": str(REPO_ROOT),
    }
    done = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_the_ai_worker_hint_is_compared_with_the_ai_workers(tmp_path: Path) -> None:
    out = _run(tmp_path, max_connections=1000)
    assert "AIJUDGE_AI_WORKERS" not in out, out
    assert "AI ワーカーが" not in out, out
    assert "書き起こしのワーカーが" not in out, out


def test_the_connection_budget_counts_the_transcription_workers(tmp_path: Path) -> None:
    """web 2 + review 1 + det 1 + 書き起こし 2 + AI 4 + finalize 1 = 11 プロセス。"""
    out = _run(tmp_path, max_connections=300)
    assert "11 プロセス" in out, out
