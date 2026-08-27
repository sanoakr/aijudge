"""境界規約そのものを検証する。

`.importlinter` は手で書く設定ファイルなので、パッケージを足したときに
登録を忘れる。忘れると contract が素通りして境界が静かに壊れるため、
ディレクトリ構成と設定ファイルを突き合わせて登録漏れを落とす。
"""

from __future__ import annotations

import configparser
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _import_linter_config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / ".importlinter")
    return parser


def _distribution_to_module(name: str) -> str:
    return name.replace("-", "_")


def test_repo_root_is_where_we_think_it_is() -> None:
    assert (REPO_ROOT / "pyproject.toml").exists()
    assert (REPO_ROOT / ".importlinter").exists()


def test_every_workspace_package_is_listed_in_the_forbidden_contract() -> None:
    """packages/* を足したら core の forbidden 契約にも足す。"""
    config = _import_linter_config()
    forbidden = set(
        config["importlinter:contract:core-is-independent"]["forbidden_modules"].split()
    )

    for manifest in sorted((REPO_ROOT / "packages").glob("*/pyproject.toml")):
        with manifest.open("rb") as handle:
            name = tomllib.load(handle)["project"]["name"]
        module = _distribution_to_module(name)
        if module == "aijudge_core":
            continue
        assert module in forbidden, (
            f"{module} is a workspace package but is missing from "
            f".importlinter's core-is-independent contract"
        )


def test_the_measurement_contract_is_declared() -> None:
    """測定が採点に依存しない契約が `.importlinter` にあること。

    契約ごと消せば依存を足せてしまう。契約の存在そのものを固定する
    （ADR 0007）。
    """
    config = _import_linter_config()
    section = "importlinter:contract:measurement-does-not-depend-on-grading"
    assert config.has_section(section), "測定の独立を保証する契約が .importlinter から消えている"
    forbidden = set(config[section]["forbidden_modules"].split())
    sources = set(config[section]["source_modules"].split())
    assert {"aijudge_analytics", "aijudge_evalrunner"} <= sources
    assert {"aijudge_core", "aijudge_grading"} <= forbidden


def test_the_measurement_app_declares_no_grading_dependencies() -> None:
    """測定アプリの依存に採点側のパッケージが入っていないこと。

    import が無くても依存宣言が残っていると、いつのまにか使い始める。
    「analytics と evalrunner を削除しても採点は動く」の裏返しとして、
    測定側から採点側への依存も無いことを固定する（ADR 0007）。
    """
    manifest = REPO_ROOT / "apps" / "evalrunner" / "pyproject.toml"
    with manifest.open("rb") as handle:
        dependencies = tomllib.load(handle)["project"]["dependencies"]
    names = {item.split(">")[0].split("=")[0].split("[")[0].strip() for item in dependencies}
    forbidden = {
        "aijudge-core",
        "aijudge-grading",
        "aijudge-authoring",
        "aijudge-llm-gateway",
        "aijudge-sandbox",
    }
    leaked = names & forbidden
    assert not leaked, f"measurement app depends on grading packages: {sorted(leaked)}"


def test_core_declares_no_io_dependencies() -> None:
    """core の依存は pydantic だけ。ここが増えるのは設計が漏れた兆候。"""
    manifest = REPO_ROOT / "packages" / "core" / "pyproject.toml"
    with manifest.open("rb") as handle:
        dependencies = tomllib.load(handle)["project"]["dependencies"]
    names = {item.split(">")[0].split("=")[0].split("[")[0].strip() for item in dependencies}
    assert names == {"pydantic"}, f"core gained unexpected dependencies: {names}"
