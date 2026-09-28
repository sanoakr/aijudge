"""コードのデプロイは、署名を確かめたタグだけを入れる（2026-09-29）。

署名を見ていたのは unit の配布（`install-units.sh`）だけで、コードのデプロイ
（`aijudge-autodeploy.sh` → `deploy.sh`）は origin の最新の `v*` タグを確かめずに
動かしていた。署名の無い v1.37.0 が実際にそのまま入った。

本物の git と ssh-keygen で、署名済み・署名なし・知らない鍵のタグを作って確かめる。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VERIFY = REPO_ROOT / "deploy" / "lib" / "verify-release-tag.sh"
AUTODEPLOY = REPO_ROOT / "deploy" / "aijudge-autodeploy.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or shutil.which("git") is None,
    reason="git と ssh-keygen が要る",
)


def _run(*args: str, cwd: Path, env: dict[str, str] | None = None, check: bool = True):
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, check=check)


def _key(path: Path) -> Path:
    _run(
        "ssh-keygen",
        "-q",
        "-t",
        "ed25519",
        "-N",
        "",
        "-C",
        "test",
        "-f",
        str(path),
        cwd=path.parent,
    )
    return path


@pytest.fixture
def release(tmp_path: Path):
    """origin（bare）と、そこから clone した運用機のチェックアウト。

    タグ: v1.0.0（許可した鍵で署名）・v1.0.1（知らない鍵で署名）・v1.1.0（署名なし）
    """
    git_env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.org",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.org",
    }
    allowed = _key(tmp_path / "allowed")
    stranger = _key(tmp_path / "stranger")
    signers = tmp_path / "allowed_signers"
    signers.write_text(f"t@example.org {(tmp_path / 'allowed.pub').read_text()}")
    signers.chmod(0o444)  # デプロイする者が書き換えられない

    work = tmp_path / "work"
    work.mkdir()
    _run("git", "init", "-q", "-b", "main", cwd=work, env=git_env)
    (work / "deploy" / "lib").mkdir(parents=True)
    shutil.copy(VERIFY, work / "deploy" / "lib" / VERIFY.name)
    shutil.copy(AUTODEPLOY, work / "deploy" / AUTODEPLOY.name)
    # deploy.sh の代わり: 呼ばれたタグを書くだけ（本物は DB・systemd に触れる）
    stub = work / "deploy" / "deploy.sh"
    stub.write_text('#!/usr/bin/env bash\necho "DEPLOY $1"\n')
    stub.chmod(0o755)
    _run("git", "add", "-A", cwd=work, env=git_env)
    _run("git", "commit", "-q", "-m", "init", cwd=work, env=git_env)

    def tag(name: str, key: Path | None) -> None:
        if key is None:
            _run("git", "tag", name, cwd=work, env=git_env)
            return
        _run(
            "git",
            "-c",
            "gpg.format=ssh",
            "-c",
            f"user.signingkey={key}",
            "tag",
            "-s",
            "-m",
            name,
            name,
            cwd=work,
            env=git_env,
        )

    tag("v1.0.0", allowed)
    tag("v1.0.1", stranger)
    tag("v1.1.0", None)

    origin = tmp_path / "origin.git"
    _run("git", "clone", "-q", "--bare", str(work), str(origin), cwd=tmp_path, env=git_env)
    checkout = tmp_path / "checkout"
    # デプロイ済みは古い版（タグがどれも同じコミットにあるので、記録が無いと
    # 作業ツリーのタグを「デプロイ済み」と読んで何もしない）
    (tmp_path / "deployed-tag").write_text("v0.9.0\n")
    _run("git", "clone", "-q", str(origin), str(checkout), cwd=tmp_path, env=git_env)
    env = {
        **git_env,
        "AIJUDGE_RELEASE_SIGNERS": str(signers),
        "AIJUDGE_REPO_DIR": str(checkout),
        "AIJUDGE_DEPLOY_STATE": str(tmp_path / "deployed-tag"),
        "AIJUDGE_DEPLOY_PIN": str(tmp_path / "deploy-pin"),
    }
    return checkout, env, tmp_path


def _verify(checkout: Path, env: dict[str, str], tag: str):
    return _run(str(VERIFY), tag, cwd=checkout, env=env, check=False)


def test_only_a_tag_signed_by_an_allowed_key_passes(release) -> None:
    checkout, env, _ = release
    assert _verify(checkout, env, "v1.0.0").returncode == 0
    for tag in ("v1.0.1", "v1.1.0"):  # 知らない鍵・署名なし（軽量タグ）
        result = _verify(checkout, env, tag)
        assert result.returncode == 1, tag
        assert "署名を確かめられません" in result.stderr


def test_a_signers_file_the_deployer_can_write_is_not_trusted(release) -> None:
    checkout, env, _ = release
    Path(env["AIJUDGE_RELEASE_SIGNERS"]).chmod(0o644)
    if os.geteuid() == 0:
        pytest.skip("root には書き込みの判定が効かない")
    result = _verify(checkout, env, "v1.0.0")
    assert result.returncode == 1
    assert "書き換えられます" in result.stderr


def test_a_missing_signers_file_refuses(release) -> None:
    checkout, env, tmp_path = release
    env = {**env, "AIJUDGE_RELEASE_SIGNERS": str(tmp_path / "nowhere")}
    assert _verify(checkout, env, "v1.0.0").returncode == 1


def test_autodeploy_picks_the_newest_signed_tag_and_names_the_skipped(release) -> None:
    checkout, env, _ = release
    result = _run("bash", str(checkout / "deploy" / AUTODEPLOY.name), cwd=checkout, env=env)
    assert "DEPLOY v1.0.0" in result.stdout
    assert "v1.1.0" in result.stderr and "v1.0.1" in result.stderr
    assert "DEPLOY v1.1.0" not in result.stdout


def test_autodeploy_refuses_a_pinned_unsigned_tag(release) -> None:
    checkout, env, _ = release
    Path(env["AIJUDGE_DEPLOY_PIN"]).write_text("v1.1.0\n")
    result = _run(
        "bash", str(checkout / "deploy" / AUTODEPLOY.name), cwd=checkout, env=env, check=False
    )
    assert result.returncode != 0
    assert "DEPLOY" not in result.stdout


def test_autodeploy_does_not_roll_back_to_an_older_signed_tag(release) -> None:
    """最新のタグが origin から消えても、1 つ前の署名済みの版へ自動で戻らない（#564）。"""
    checkout, env, _ = release
    Path(env["AIJUDGE_DEPLOY_STATE"]).write_text("v1.2.0\n")
    result = _run("bash", str(checkout / "deploy" / AUTODEPLOY.name), cwd=checkout, env=env)
    assert "DEPLOY" not in result.stdout
    assert "v1.0.0" in result.stderr and "v1.2.0" in result.stderr


def test_autodeploy_rolls_back_when_pinned(release) -> None:
    """意図した切り戻しは固定でする（#425）。固定すれば古い署名済みの版にも戻せる。"""
    checkout, env, _ = release
    Path(env["AIJUDGE_DEPLOY_STATE"]).write_text("v1.2.0\n")
    Path(env["AIJUDGE_DEPLOY_PIN"]).write_text("v1.0.0\n")
    result = _run("bash", str(checkout / "deploy" / AUTODEPLOY.name), cwd=checkout, env=env)
    assert "DEPLOY v1.0.0" in result.stdout


def test_deploy_checks_the_signature_before_checking_out() -> None:
    """deploy.sh 本体は DB と systemd に触れるので、順序を静的に確かめる。"""
    text = (REPO_ROOT / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    verify_at = text.index("deploy/lib/verify-release-tag.sh")
    assert verify_at < text.index('git checkout --detach "refs/tags/${TAG}"')
    assert verify_at < text.index('uv run --project "${REPO_DIR}" alembic upgrade head')
