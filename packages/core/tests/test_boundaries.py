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


def _workspace_modules(*kinds: str) -> set[str]:
    """`packages/*` などのうち、pyproject を持つものの import 名。"""
    modules: set[str] = set()
    for kind in kinds:
        for manifest in sorted((REPO_ROOT / kind).glob("*/pyproject.toml")):
            with manifest.open("rb") as handle:
                modules.add(_distribution_to_module(tomllib.load(handle)["project"]["name"]))
    return modules


def _listed(section: str, key: str) -> set[str]:
    return set(_import_linter_config()[f"importlinter:contract:{section}"][key].split())


def test_every_workspace_member_is_a_root_package() -> None:
    """**評価器と抽出器も登録する**（#433）。

    以前は `packages/*` しか照合しておらず、`checklist_ai_judge` と
    `submission_compliance` が `.importlinter` のどこにも無いまま、どの契約も
    掛からずに入っていた ── lint-imports は緑で、何も保証していなかった。
    """
    config = _import_linter_config()
    roots = set(config["importlinter"]["root_packages"].split())
    members = _workspace_modules("packages", "evaluators", "extractors", "apps")
    missing = members - roots
    assert not missing, f"not registered in .importlinter root_packages: {sorted(missing)}"


def test_every_evaluator_is_under_the_evaluator_contracts() -> None:
    plugins = _workspace_modules("evaluators", "extractors")
    for section, key in (
        ("evaluators-depend-on-core-and-protocol-only", "source_modules"),
        ("evaluators-do-not-know-each-other", "modules"),
        ("engine-does-not-know-evaluators", "forbidden_modules"),
        ("grading-does-not-know-ide", "source_modules"),
        ("subsystems-do-not-know-the-store", "source_modules"),
    ):
        missing = plugins - _listed(section, key)
        assert not missing, f"{section}.{key} is missing {sorted(missing)}"


def test_no_package_below_the_apps_may_import_an_app() -> None:
    """合成ルート（apps）より下の層は、全部が apps 禁止の対象に入る（#434）。"""
    below = _workspace_modules("packages", "evaluators", "extractors")
    missing = below - _listed("subsystems-do-not-depend-on-apps", "source_modules")
    assert not missing, f"subsystems-do-not-depend-on-apps is missing {sorted(missing)}"
    apps = _workspace_modules("apps")
    assert apps <= _listed("subsystems-do-not-depend-on-apps", "forbidden_modules")


def test_only_the_apps_may_import_the_shared_web_parts() -> None:
    """`webapp`（両 Web アプリの共有部品）より下の層は、全部が禁止の対象に入る（#437）。

    パッケージを足したときに契約へ足し忘れると、そのパッケージだけ HTTP の
    部品を import できてしまう。`subsystems-do-not-depend-on-apps` と同じ照合。
    """
    below = _workspace_modules("packages", "evaluators", "extractors") - {"aijudge_webapp"}
    missing = below - _listed("webapp-is-for-the-apps", "source_modules")
    assert not missing, f"webapp-is-for-the-apps is missing {sorted(missing)}"
    assert _listed("webapp-is-for-the-apps", "forbidden_modules") == {"aijudge_webapp"}


def test_contracts_name_only_modules_that_exist() -> None:
    """存在しないモジュールを名指しする契約は、何も守っていない（#434）。"""
    config = _import_linter_config()
    known = _workspace_modules("packages", "evaluators", "extractors", "apps")
    for section in config.sections():
        if not section.startswith("importlinter:contract:"):
            continue
        for key in ("source_modules", "forbidden_modules", "modules"):
            if key not in config[section]:
                continue
            for module in config[section][key].split():
                if not module.startswith("aijudge_"):
                    continue
                assert module.split(".")[0] in known, f"{section} names {module}, which is gone"


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


def test_the_logging_contracts_are_declared() -> None:
    """ログの層分けが `.importlinter` に残っていること（ADR 0016）。

    契約ごと消せば依存を足せてしまうので、契約の存在そのものを固定する
    （測定の独立を固定しているのと同じ理由）。

    - 運用ログの設定を触ってよいのは合成ルート（apps/*）だけ
    - 運用ログは他のどのサブシステムも知らない

    後者が破れると「ログを外しても採点は動く」が成立しなくなる。
    """
    config = _import_linter_config()

    only_apps = "importlinter:contract:only-apps-configure-logging"
    assert config.has_section(only_apps), "ログ設定の責務を縛る契約が .importlinter から消えている"
    sources = set(config[only_apps]["source_modules"].split())
    assert {"aijudge_core", "aijudge_grading", "aijudge_persistence"} <= sources
    assert set(config[only_apps]["forbidden_modules"].split()) == {"aijudge_telemetry"}

    standalone = "importlinter:contract:telemetry-knows-nothing"
    assert config.has_section(standalone), (
        "運用ログの独立を保証する契約が .importlinter から消えている"
    )
    forbidden = set(config[standalone]["forbidden_modules"].split())
    assert {"aijudge_core", "aijudge_grading", "aijudge_persistence"} <= forbidden


def test_the_operational_log_declares_no_dependencies() -> None:
    """運用ログは標準 logging だけで書く。

    ここに依存が増えるのは、運用ログが業務の語彙を持ち始めた兆候であり、
    そのときログは「消えてよい層」ではなくなっている。消えては困る記録は
    監査ログ（DB）か採点記録（`GradingRun`）に置くこと。
    """
    manifest = REPO_ROOT / "packages" / "telemetry" / "pyproject.toml"
    with manifest.open("rb") as handle:
        dependencies = tomllib.load(handle)["project"]["dependencies"]
    assert dependencies == [], f"telemetry gained unexpected dependencies: {dependencies}"


def test_the_audit_contract_is_declared() -> None:
    """監査記録が保存先にも採点にも依存しない契約があること（ADR 0016）。

    破れると 2 つ壊れる。実装を差し替えられなくなる（PostgreSQL が全テストの
    前提になる）ことと、監査を消すと採点が起動しなくなること ──
    後者は `aijudge_observation` を分ける前に実際に起きた（ADR 0007）。
    """
    config = _import_linter_config()
    section = "importlinter:contract:audit-knows-no-store"
    assert config.has_section(section), (
        "監査記録の独立を保証する契約が .importlinter から消えている"
    )
    forbidden = set(config[section]["forbidden_modules"].split())
    assert {"aijudge_persistence", "sqlalchemy", "aijudge_grading"} <= forbidden
    # 運用ログにも依存しない。2 つを繋ぐのは合成ルートの仕事である。
    assert "aijudge_telemetry" in forbidden


def test_the_audit_record_declares_only_core_and_pydantic() -> None:
    """監査記録の依存はここで止める。

    増えるのは、監査が業務の実装を知り始めた兆候であり、そのとき監査は
    「その実装を消すと壊れるもの」になっている。
    """
    manifest = REPO_ROOT / "packages" / "audit" / "pyproject.toml"
    with manifest.open("rb") as handle:
        dependencies = tomllib.load(handle)["project"]["dependencies"]
    names = {item.split(">")[0].split("=")[0].split("[")[0].strip() for item in dependencies}
    assert names == {"aijudge-core", "pydantic"}, f"audit gained unexpected dependencies: {names}"


def test_the_activity_record_cannot_reach_grades() -> None:
    """IDE の行動記録と印が成績に届かないことを保証する契約があること（ADR 0023 §4）。

    契約ごと消せば、採点ワーカーや成績の確定から印を読めてしまう。
    契約の存在と中身を固定する。
    """
    config = _import_linter_config()
    for section, sources in (
        ("importlinter:contract:grading-does-not-know-ide", {"aijudge_grading"}),
        (
            "importlinter:contract:grades-do-not-read-activity",
            {"aijudge_grader", "aijudge_admin.finalization"},
        ),
    ):
        assert config.has_section(section), f"{section} が .importlinter から消えている"
        assert sources <= set(config[section]["source_modules"].split())
        assert "aijudge_ide" in config[section]["forbidden_modules"].split()
