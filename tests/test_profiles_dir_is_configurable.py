"""科目プロファイルの置き場所が、すべての入口で環境変数から決まること。

運用のプロファイルは **git のチェックアウトの外**に置く（`subjects/README.md`）。
デプロイは `git checkout <tag>` でコードを入れ替えるので、リポジトリの
`subjects/` に運用の変更を置くと、そのとき消える。

**入口ごとにばらつくと、食い違いは「採点だけ古い宣言で走る」形で現れる。**
画面では新しい設定が見えているのにワーカーは古い方を読む、という状態は
再現も説明も難しい（`docs/RUNNING.md` の環境変数表が「すべてが同じ場所を
指すこと」と書いているのはこのため）。だから、`--profiles` を持つ入口が
1 つでも環境変数を読み落としていないことを機械で確かめる。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aijudge_admin.cli import build_parser

REPO_ROOT = Path(__file__).resolve().parents[1]

ENV_PROFILES_DIR = "AIJUDGE_PROFILES_DIR"

# `--profiles` を持つ入口。増えたらここも増える（下のテストが検出する）。
CLI_MODULES = (
    REPO_ROOT / "apps" / "admin" / "src" / "aijudge_admin" / "cli.py",
    REPO_ROOT / "apps" / "grader" / "src" / "aijudge_grader" / "cli.py",
    REPO_ROOT / "apps" / "reviewconsole" / "src" / "aijudge_reviewconsole" / "cli.py",
    REPO_ROOT / "apps" / "studentweb" / "src" / "aijudge_studentweb" / "cli.py",
)


def test_the_admin_cli_reads_the_profiles_directory_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(ENV_PROFILES_DIR, str(tmp_path / "subjects"))

    args = build_parser().parse_args(["course", "list"])

    assert args.profiles == tmp_path / "subjects"


def test_the_admin_cli_falls_back_to_the_repositorys_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既定はリポジトリの `subjects/`（サンプル）。開発機はこれで動く。"""
    monkeypatch.delenv(ENV_PROFILES_DIR, raising=False)

    args = build_parser().parse_args(["course", "list"])

    assert args.profiles == REPO_ROOT / "subjects"


def test_every_entry_point_resolves_the_profiles_directory_from_the_environment() -> None:
    """`--profiles` を持つ入口が、既定を環境変数から取っていること。

    決め打ちの `REPO_ROOT / "subjects"` だけを既定にしている入口が 1 つでも
    あると、その入口だけデプロイで消える場所を読み続ける。
    """
    missing: list[str] = []
    for path in CLI_MODULES:
        source = path.read_text(encoding="utf-8")
        assert '"--profiles"' in source, f"{path.relative_to(REPO_ROOT)} に --profiles が無い"
        if ENV_PROFILES_DIR not in source:
            missing.append(str(path.relative_to(REPO_ROOT)))
    assert not missing, f"{ENV_PROFILES_DIR} を読んでいない入口がある: {missing}"


def test_no_other_entry_point_defines_profiles_without_the_environment() -> None:
    """`--profiles` を持つ入口が増えたら、このテストの一覧にも足す。

    一覧に無い入口を見つけたら失敗させる ── 足し忘れると、上のテストは
    「知っている入口だけ」を確かめて通ってしまう。
    """
    known = {path.resolve() for path in CLI_MODULES}
    found = {
        path.resolve()
        for path in (REPO_ROOT / "apps").rglob("cli.py")
        if re.search(r'"--profiles"', path.read_text(encoding="utf-8"))
    }
    assert found == known, f"一覧に無い入口がある: {sorted(str(p) for p in found - known)}"
