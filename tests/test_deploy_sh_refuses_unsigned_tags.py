"""手で流すデプロイ（`deploy.sh <tag>`）で、署名の確認が checkout より前に効くと確かめる。

#708。

`test_release_tag_verification.py` の `test_deploy_checks_the_signature_before_checking_out`
は文字列の出現順しか見ない。`deploy.sh` の署名確認の行を `|| true` にしても、
コメントにしても、引数を `"${TAG}"` から変えても通っていた。autodeploy 経由では
`aijudge-autodeploy.sh` が別に確かめるが、`sudo -u aijudge deploy.sh <tag>` と手で
流す経路は `deploy.sh` の 1 行だけが門である。

そこで `deploy.sh` 本体を使い捨てのチェックアウトに対して走らせる。

- `git` は本物を呼ぶ替え玉にし、`checkout` だけは記録して何もしない
  （署名の確認 `verify-release-tag.sh` は本物の git で走る）
- `uv`・`systemctl`・`flock`・`curl`・`aijudge-db-backup.sh` は記録するだけの替え玉
- ロックのパス（`/run/lock/...`）だけは一時ディレクトリへ書き換えた写しを走らせる。
  実機の `/run/lock` に触れないためで、書き換えは 1 か所であることを先に確かめる

本物の DB・systemd・ネットワークには一切触れない。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# `release` fixture を流用する（bare origin・許可した鍵・署名済み/未署名/
# 知らない鍵/名前の付け替えのタグ）。
from test_release_tag_verification import release  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SH = REPO_ROOT / "deploy" / "deploy.sh"
LOCK_PATH = "/run/lock/aijudge-deploy.lock"

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or shutil.which("git") is None,
    reason="git と ssh-keygen が要る",
)

# 呼ばれたコマンドを 1 行ずつ記録するだけの替え玉。
RECORDER = '#!/usr/bin/env bash\necho "{name} $*" >> "${{FAKE_CALL_LOG}}"\n'

# git の替え玉。`checkout` は記録して何もしない。それ以外は本物へ渡す。
FAKE_GIT = """#!/usr/bin/env bash
echo "git $*" >> "${{FAKE_CALL_LOG}}"
if [ "${{1:-}}" = "checkout" ]; then
    exit 0
fi
exec "{real_git}" "$@"
"""


def _sandboxed_deploy_sh(dest: Path, lock: Path) -> Path:
    """ロックのパスだけを一時ディレクトリへ向けた `deploy.sh` の写し。"""
    text = DEPLOY_SH.read_text(encoding="utf-8")
    assert text.count(LOCK_PATH) == 1, "ロックの書き方が変わった。替え方を見直すこと"
    dest.write_text(text.replace(LOCK_PATH, str(lock)), encoding="utf-8")
    dest.chmod(0o755)
    return dest


def _deploy(release_world: tuple[Path, dict[str, str], Path], tag: str):
    checkout, env, tmp_path = release_world
    real_git = shutil.which("git")
    assert real_git is not None

    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "git").write_text(FAKE_GIT.format(real_git=real_git))
    for name in ("uv", "systemctl", "flock", "curl", "aijudge-db-backup.sh"):
        (bin_dir / name).write_text(RECORDER.format(name=name))
    for path in bin_dir.iterdir():
        path.chmod(0o755)

    script = _sandboxed_deploy_sh(tmp_path / "deploy.sh", tmp_path / "deploy.lock")
    call_log = tmp_path / "calls.log"
    # 呼び出し元の環境にある AIJUDGE_* は持ち込まない（本物の URL に疎通確認しないため）。
    run_env = {k: v for k, v in env.items() if not k.startswith("AIJUDGE_")}
    run_env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "AIJUDGE_REPO_DIR": env["AIJUDGE_REPO_DIR"],
            "AIJUDGE_RELEASE_SIGNERS": env["AIJUDGE_RELEASE_SIGNERS"],
            "AIJUDGE_DEPLOY_STATE": env["AIJUDGE_DEPLOY_STATE"],
            "AIJUDGE_DATABASE_URL": "postgresql://nowhere.invalid/none",
            "FAKE_CALL_LOG": str(call_log),
        }
    )
    result = subprocess.run(
        ["bash", str(script), tag],
        cwd=checkout,
        env=run_env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = call_log.read_text().splitlines() if call_log.exists() else []
    return result, calls


def _touches_the_system(calls: list[str]) -> list[str]:
    """署名の確認より後にしか起きてはいけない呼び出し。"""
    return [
        c
        for c in calls
        if c.startswith(("git checkout", "uv ", "systemctl ", "aijudge-db-backup.sh"))
    ]


def test_a_signed_tag_is_deployed(release) -> None:  # noqa: F811
    """対照: 替え玉だけで `deploy.sh` が最後まで通ること（通らなければ下の検査は無意味）。"""
    _, env, _ = release
    result, calls = _deploy(release, "v1.0.0")
    assert result.returncode == 0, result.stderr
    assert "git checkout --detach refs/tags/v1.0.0" in calls
    assert any(c.startswith("uv run") and "alembic upgrade head" in c for c in calls), calls
    assert "systemctl restart aijudge.target" in calls
    assert Path(env["AIJUDGE_DEPLOY_STATE"]).read_text() == "v1.0.0\n"


@pytest.mark.parametrize(
    "tag",
    [
        "v1.1.0",  # 署名なし（軽量タグ）
        "v1.0.1",  # 許可リストに無い鍵の署名
        "v99.0.0",  # 署名済みのタグオブジェクトを別の名前で出し直したもの
    ],
)
def test_an_unverified_tag_stops_before_checkout(release, tag: str) -> None:  # noqa: F811
    """署名を確かめられないタグでは非ゼロで終わり、checkout・migration・restart に進まない。"""
    _, env, _ = release
    result, calls = _deploy(release, tag)
    assert result.returncode != 0, result.stdout
    assert _touches_the_system(calls) == [], calls
    # 記録はデプロイ前の版のまま（autodeploy が「入った」と誤読しない）。
    assert Path(env["AIJUDGE_DEPLOY_STATE"]).read_text() == "v0.9.0\n"
