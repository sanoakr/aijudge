"""unit の配布（`install-units.sh`）を実際に動かして、配ってよいタグを確かめる（2026-11-14）。

配るのは root で、配るものには root で走る unit とスクリプトが含まれる。だから
「署名済みのタグなら配る」に加えて、次を固定する。

- 下限: 下限のコミットを祖先に持たないタグは、署名済みでも配らない
- 順序: 最後に配った版より古いタグは配らない。root が切り戻しを指定したときだけ通す
- 既存の門: 署名の無いタグ・他者が書ける許可リスト・シンボリックリンクの HEAD は断る
- 名前: 署名済みのタグオブジェクトを別の名前で出し直したタグは断る

本物の git と ssh-keygen でタグを作る。`stat`・`install`・`systemctl` は PATH の
替え玉にする（root でなくても動かすため。`stat` は「root 所有」と答えるが、権限の
数字は実物を返すので、他者が書けるかの判定はそのまま効く）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "install-units.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or shutil.which("git") is None,
    reason="git と ssh-keygen が要る",
)

# 替え玉。`stat -c FMT PATH` だけを受ける。FAKE_NOT_ROOT に挙げたパスは root 以外の所有と答える。
FAKE_STAT = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os, stat, sys
    fmt, path = sys.argv[2], sys.argv[3]
    not_root = [p for p in os.environ.get("FAKE_NOT_ROOT", "").split(":") if p]
    if fmt == "%U":
        print("aijudge" if path in not_root else "root")
    elif fmt == "%a":
        print(format(stat.S_IMODE(os.stat(path).st_mode), "o"))
    else:
        sys.exit(2)
    """
)
# 替え玉。`install [-d] [-m MODE] [-o U] [-g G] SRC DST` の所有者だけを無視する。
FAKE_INSTALL = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import os, shutil, sys
    args, mode, make_dir, paths = sys.argv[1:], None, False, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-d":
            make_dir = True
        elif a in ("-m", "-o", "-g"):
            if a == "-m":
                mode = int(args[i + 1], 8)
            i += 1
        else:
            paths.append(a)
        i += 1
    if make_dir:
        for p in paths:
            os.makedirs(p, exist_ok=True)
        sys.exit(0)
    src, dst = paths
    shutil.copyfile(src, dst)
    if mode is not None:
        os.chmod(dst, mode)
    """
)
FAKE_SYSTEMCTL = '#!/usr/bin/env bash\necho "$*" >> "${FAKE_SYSTEMCTL_LOG}"\n'


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


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.git_env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.org",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.org",
        }
        self.key = _key(tmp_path / "allowed")
        self.signers = tmp_path / "allowed_signers"
        self.signers.write_text(f"t@example.org {(tmp_path / 'allowed.pub').read_text()}")
        self.signers.chmod(0o644)

        # 履歴: v1.0.0（下限より前）→ v1.1.0（下限）→ v1.2.0 → v1.3.0（署名なし）
        work = tmp_path / "work"
        (work / "deploy" / "systemd").mkdir(parents=True)
        _run("git", "init", "-q", "-b", "main", cwd=work, env=self.git_env)
        self.commits: dict[str, str] = {}
        for tag, signed in (
            ("v1.0.0", True),
            ("v1.1.0", True),
            ("v1.2.0", True),
            ("v1.3.0", False),
        ):
            (work / "deploy" / "systemd" / "aijudge-example.service").write_text(
                f"[Service]\n# {tag}\n"
            )
            shutil.copy(SCRIPT, work / "deploy" / "install-units.sh")
            _run("git", "add", "-A", cwd=work, env=self.git_env)
            _run("git", "commit", "-q", "-m", tag, cwd=work, env=self.git_env)
            if signed:
                _run(
                    "git",
                    "-c",
                    "gpg.format=ssh",
                    "-c",
                    f"user.signingkey={self.key}",
                    "tag",
                    "-s",
                    "-m",
                    tag,
                    tag,
                    cwd=work,
                    env=self.git_env,
                )
            else:
                _run("git", "tag", tag, cwd=work, env=self.git_env)
            self.commits[tag] = _run(
                "git", "rev-parse", "HEAD", cwd=work, env=self.git_env
            ).stdout.strip()

        self.origin = origin = tmp_path / "origin.git"
        _run("git", "clone", "-q", "--bare", str(work), str(origin), cwd=tmp_path, env=self.git_env)
        self.mirror = tmp_path / "mirror.git"
        _run(
            "git",
            "clone",
            "-q",
            "--bare",
            str(origin),
            str(self.mirror),
            cwd=tmp_path,
            env=self.git_env,
        )
        self.mirror.chmod(0o755)

        # チェックアウト: install-units.sh が読むのは .git/HEAD の中身だけ
        self.checkout = tmp_path / "checkout"
        (self.checkout / ".git").mkdir(parents=True)

        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, body in (
            ("stat", FAKE_STAT),
            ("install", FAKE_INSTALL),
            ("systemctl", FAKE_SYSTEMCTL),
        ):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)

        self.dst = tmp_path / "etc-systemd"
        self.sbin = tmp_path / "sbin"
        self.lib = tmp_path / "lib"
        for d in (self.dst, self.sbin, self.lib):
            d.mkdir()
        self.state = tmp_path / "installed-units"
        self.rollback = tmp_path / "units-rollback-to"
        self.systemctl_log = tmp_path / "systemctl.log"
        self.env = {
            **self.git_env,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "AIJUDGE_REPO_DIR": str(self.checkout),
            "AIJUDGE_RELEASE_MIRROR": str(self.mirror),
            "AIJUDGE_RELEASE_SIGNERS": str(self.signers),
            "AIJUDGE_RELEASE_FLOOR": self.commits["v1.1.0"],
            "AIJUDGE_UNITS_STATE": str(self.state),
            "AIJUDGE_UNITS_ROLLBACK": str(self.rollback),
            "AIJUDGE_UNITS_DST": str(self.dst),
            "AIJUDGE_UNITS_SBIN": str(self.sbin),
            "AIJUDGE_UNITS_LIB": str(self.lib),
            "FAKE_SYSTEMCTL_LOG": str(self.systemctl_log),
        }

    def head_at(self, tag: str) -> None:
        (self.checkout / ".git" / "HEAD").write_text(self.commits[tag] + "\n")

    def record(self, tag: str) -> None:
        self.state.write_text(f"{tag} {self.commits[tag]}\n")
        self.state.chmod(0o644)

    def run(self, **extra: str):
        return _run("bash", str(SCRIPT), cwd=self.tmp, env={**self.env, **extra}, check=False)

    def installed(self) -> str | None:
        unit = self.dst / "aijudge-example.service"
        return unit.read_text() if unit.exists() else None


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _refused(world: World, result) -> None:
    """断ったときは何も配らず、記録も変えない。"""
    assert result.returncode != 0, result.stdout
    assert world.installed() is None
    assert not (world.sbin / "aijudge-install-units").exists()
    assert not world.systemctl_log.exists()


def test_a_current_signed_tag_is_installed_and_recorded(world: World) -> None:
    world.head_at("v1.2.0")
    result = world.run()
    assert result.returncode == 0, result.stderr
    assert "# v1.2.0" in (world.installed() or "")
    assert (world.sbin / "aijudge-install-units").exists()
    assert "daemon-reload" in world.systemctl_log.read_text()
    assert world.state.read_text() == f"v1.2.0 {world.commits['v1.2.0']}\n"


def test_a_signed_tag_below_the_floor_is_refused(world: World) -> None:
    world.head_at("v1.0.0")
    result = world.run()
    _refused(world, result)
    assert "下限" in result.stderr
    assert not world.state.exists()


def test_a_tag_older_than_the_last_installed_is_refused(world: World) -> None:
    world.record("v1.2.0")
    world.head_at("v1.1.0")
    result = world.run()
    _refused(world, result)
    assert "最後に配った" in result.stderr
    assert world.state.read_text().startswith("v1.2.0 ")


def test_root_can_roll_back_explicitly(world: World) -> None:
    world.record("v1.2.0")
    world.head_at("v1.1.0")
    world.rollback.write_text("v1.1.0\n")
    world.rollback.chmod(0o644)
    result = world.run()
    assert result.returncode == 0, result.stderr
    assert "# v1.1.0" in (world.installed() or "")
    assert world.state.read_text().startswith("v1.1.0 ")


def test_a_rollback_naming_another_tag_does_not_pass(world: World) -> None:
    world.record("v1.2.0")
    world.head_at("v1.1.0")
    world.rollback.write_text("v1.0.0\n")
    world.rollback.chmod(0o644)
    _refused(world, world.run())


def test_a_rollback_file_others_can_write_is_not_trusted(world: World) -> None:
    world.record("v1.2.0")
    world.head_at("v1.1.0")
    world.rollback.write_text("v1.1.0\n")
    world.rollback.chmod(0o666)
    _refused(world, world.run())


def test_a_record_not_owned_by_root_is_not_trusted(world: World) -> None:
    world.record("v1.1.0")
    world.head_at("v1.2.0")
    _refused(world, world.run(FAKE_NOT_ROOT=str(world.state)))


def test_a_newer_tag_than_the_record_is_installed(world: World) -> None:
    world.record("v1.1.0")
    world.head_at("v1.2.0")
    result = world.run()
    assert result.returncode == 0, result.stderr
    assert world.state.read_text().startswith("v1.2.0 ")


def test_an_unsigned_tag_is_refused(world: World) -> None:
    world.head_at("v1.3.0")
    result = world.run()
    _refused(world, result)
    assert "署名を確かめられません" in result.stderr


def test_a_signed_tag_republished_under_another_name_is_refused(world: World) -> None:
    """origin で v1.2.0 のタグオブジェクトを v9.0.0 として出し直しても、配らない。

    署名は本物なので `verify-tag` は通る。タグオブジェクトの中の名前（v1.2.0）と
    参照名（v9.0.0）の食い違いで断る。
    """
    tag_object = _run(
        "git",
        "--git-dir",
        str(world.origin),
        "rev-parse",
        "refs/tags/v1.2.0",
        cwd=world.tmp,
        env=world.git_env,
    ).stdout.strip()
    _run(
        "git",
        "--git-dir",
        str(world.origin),
        "update-ref",
        "refs/tags/v9.0.0",
        tag_object,
        cwd=world.tmp,
        env=world.git_env,
    )
    world.head_at("v1.2.0")
    result = world.run()
    _refused(world, result)
    assert "付け替え" in result.stderr
    assert not world.state.exists()


def test_a_signers_file_others_can_write_is_refused(world: World) -> None:
    world.head_at("v1.2.0")
    world.signers.chmod(0o666)
    _refused(world, world.run())


def test_a_symlinked_head_is_refused(world: World) -> None:
    target = world.tmp / "elsewhere"
    target.write_text(world.commits["v1.2.0"] + "\n")
    (world.checkout / ".git" / "HEAD").symlink_to(target)
    _refused(world, world.run())
