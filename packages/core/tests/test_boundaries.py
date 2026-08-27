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


def test_the_grading_side_declares_no_measurement_dependencies() -> None:
    """採点・レビュー側が測定パッケージに依存していないこと。

    契約が「測定 → 採点」の片方向だけだと、逆向きの import が通ってしまう。
    実際に通っていた: レビューコンソールが観測の型を `aijudge_analytics` から
    import していたため、測定を削除すると採点が起動しなくなった
    （2026-08-28 の削除実験で判明）。記録の型は `aijudge_observation` に分けた。
    """
    config = _import_linter_config()
    section = "importlinter:contract:grading-does-not-depend-on-measurement"
    assert config.has_section(section), (
        "採点側が測定に依存しないことを保証する契約が .importlinter から消えている"
    )
    sources = set(config[section]["source_modules"].split())
    forbidden = set(config[section]["forbidden_modules"].split())
    assert {"aijudge_grading", "aijudge_reviewconsole"} <= sources
    assert {"aijudge_analytics", "aijudge_evalrunner"} <= forbidden

    manifest = REPO_ROOT / "apps" / "reviewconsole" / "pyproject.toml"
    with manifest.open("rb") as handle:
        dependencies = tomllib.load(handle)["project"]["dependencies"]
    names = {item.split(">")[0].split("=")[0].split("[")[0].strip() for item in dependencies}
    leaked = names & {"aijudge-analytics", "aijudge-evalrunner"}
    assert not leaked, f"the grading side depends on measurement packages: {sorted(leaked)}"


def test_the_observation_record_declares_nothing_but_pydantic() -> None:
    """記録の形式は Phase 0 の側に置き、何にも依存させない（ADR 0007）。

    ここに採点側か測定側の依存が入った瞬間、「記録は残るが測定は任意」が
    成立しなくなる。
    """
    manifest = REPO_ROOT / "packages" / "observation" / "pyproject.toml"
    with manifest.open("rb") as handle:
        dependencies = tomllib.load(handle)["project"]["dependencies"]
    names = {item.split(">")[0].split("=")[0].split("[")[0].strip() for item in dependencies}
    assert names == {"pydantic"}, f"observation gained unexpected dependencies: {names}"


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


def test_neither_core_nor_the_engine_knows_a_language() -> None:
    """科目を足しても採点エンジンが変わらないこと（ADR 0002）。

    2 つめの科目（ネットワーク演習・Python）を足したときの実測では、
    `packages/core` と `packages/grading` の変更行数は **0 行**だった。
    変わったのは評価器（言語定義）と科目 YAML だけ。

    この性質は行数では守れないので、言語固有の語がエンジンに現れないことを
    見る。ここに `python` や `cc` が入り始めたら、科目の追加がエンジンの
    改修になっている。
    """
    # 言語処理系・拡張子・言語名。コメントも含めて現れないこと。
    forbidden = ("cc ", "gcc", "-std=c11", "main.c", "main.py", "python3", "javac")
    checked = 0
    for package in ("core", "grading"):
        for path in (REPO_ROOT / "packages" / package / "src").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            checked += 1
            for token in forbidden:
                assert token not in text, (
                    f"{path.relative_to(REPO_ROOT)} に言語固有の語 {token!r} がある。"
                    "科目の追加が採点エンジンの改修になっていないか確認すること"
                )
    assert checked > 5, "検査対象が少なすぎる（走査に失敗している）"


def test_the_engine_names_no_subject() -> None:
    """特定の科目名がエンジンに現れないこと。

    科目プロファイルは名前で指名される。エンジンが名前を知っていたら、
    その科目だけ特別扱いする経路がある。
    """
    subjects = {path.stem for path in (REPO_ROOT / "subjects").glob("*.yaml")}
    assert subjects, "科目プロファイルが見つからない"
    for package in ("core", "grading"):
        for path in (REPO_ROOT / "packages" / package / "src").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for name in subjects:
                assert name not in text, (
                    f"{path.relative_to(REPO_ROOT)} が科目 {name!r} を名指ししている"
                )


def test_core_declares_no_io_dependencies() -> None:
    """core の依存は pydantic だけ。ここが増えるのは設計が漏れた兆候。"""
    manifest = REPO_ROOT / "packages" / "core" / "pyproject.toml"
    with manifest.open("rb") as handle:
        dependencies = tomllib.load(handle)["project"]["dependencies"]
    names = {item.split(">")[0].split("=")[0].split("[")[0].strip() for item in dependencies}
    assert names == {"pydantic"}, f"core gained unexpected dependencies: {names}"
