"""`install-units.sh` を動かして、配り方そのもの（何を・いつ書き換えるか）を確かめる（#708）。

`test_install_units_release_floor.py` は「どのタグなら配るか」の門を固定している。
こちらは門を通った後の配り方を固定する。どれも消しても他のテストが通っていた動作である。

- 中身が同じなら書き換えず、`daemon-reload` もしない（mtime だけが動くとログから
  何が変わったか読めない）
- unit が変わったら `daemon-reload` と `reenable aijudge.target` をする（`Wants=` を
  足しただけでは `.wants/` が増えず、AI ワーカー @2..@4 が起動していなかった）
- 配る中身はタグから取り出したもので、チェックアウトの作業ツリーは読まない（#417）
- 配ったスクリプトは実行できる権限、unit は 0644
- HEAD がコミットのハッシュでない（ブランチの上にいる）なら配らない

替え玉と fixture は `test_install_units_release_floor.py` の `World` を流用する。
"""

from __future__ import annotations

import shutil
import stat

import pytest
from test_install_units_release_floor import World, world  # noqa: F401

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or shutil.which("git") is None,
    reason="git と ssh-keygen が要る",
)


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_second_run_with_the_same_tag_changes_nothing(world: World) -> None:  # noqa: F811
    world.head_at("v1.2.0")
    first = world.run()
    assert first.returncode == 0, first.stderr
    unit = world.dst / "aijudge-example.service"
    before = unit.stat().st_mtime_ns
    world.systemctl_log.unlink()

    second = world.run()
    assert second.returncode == 0, second.stderr
    assert "unit updated" not in second.stdout
    assert "script updated" not in second.stdout
    assert "unit に変更なし" in second.stdout
    assert unit.stat().st_mtime_ns == before
    assert not world.systemctl_log.exists(), "変更が無いのに systemctl を呼んだ"


def test_a_changed_unit_reloads_and_reenables_the_target(world: World) -> None:  # noqa: F811
    world.record("v1.1.0")
    (world.dst / "aijudge-example.service").write_text("[Service]\n# v1.1.0\n")
    world.head_at("v1.2.0")
    result = world.run()
    assert result.returncode == 0, result.stderr
    assert "unit updated: aijudge-example.service" in result.stdout
    calls = world.systemctl_log.read_text().splitlines()
    assert calls == ["daemon-reload", "reenable aijudge.target"], calls


def test_the_content_comes_from_the_tag_not_the_checkout(world: World) -> None:  # noqa: F811
    """チェックアウトは aijudge が書ける。そこに置いた unit は root に配られない。"""
    world.head_at("v1.2.0")
    planted = world.checkout / "deploy" / "systemd"
    planted.mkdir(parents=True)
    (planted / "aijudge-example.service").write_text("[Service]\nUser=root\n")
    (planted / "aijudge-planted.service").write_text("[Service]\nUser=root\n")
    (world.checkout / "deploy" / "install-units.sh").write_text("#!/bin/sh\necho planted\n")

    result = world.run()
    assert result.returncode == 0, result.stderr
    assert "# v1.2.0" in (world.installed() or "")
    assert not (world.dst / "aijudge-planted.service").exists()
    assert "planted" not in (world.sbin / "aijudge-install-units").read_text()


def test_installed_files_get_the_intended_modes(world: World) -> None:  # noqa: F811
    world.head_at("v1.2.0")
    result = world.run()
    assert result.returncode == 0, result.stderr
    assert _mode(world.dst / "aijudge-example.service") == 0o644
    assert _mode(world.sbin / "aijudge-install-units") == 0o755
    assert _mode(world.state) == 0o644


def test_a_checkout_on_a_branch_is_refused(world: World) -> None:  # noqa: F811
    (world.checkout / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    result = world.run()
    assert result.returncode != 0
    assert "タグの上にありません" in result.stderr
    assert world.installed() is None
    assert not world.state.exists()
