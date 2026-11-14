"""本番系がハーネス（`evals/report_ja`）のモジュールを import しないことを、AST で確かめる（#708）。

`test_evals_stay_out_of_production.py` の正規表現は `import rubric` の 1 行形しか拾わない。
次の書き方はすべてすり抜けていた。

    HARNESS = ROOT / "evals" / "report_ja"
    sys.path.insert(0, str(HARNESS))   # 引数に "evals" の文字が無い
    from rubric import RUBRIC          # from 形
    import grade                       # rubric 以外のモジュール名

ハーネスは教員の採点表と学生の個人情報を読む。本番系がそのコードを取り込めば、
ADR 0007（測定を消しても採点は動く）と個人情報の境界（P7）が崩れる。import-linter は
素のスクリプトを見られないので、ここで本文を構文として読む。

- ハーネスのモジュール名は、`evals/report_ja/*.py` の stem を実行時に列挙する
  （ファイルが増えても、テストを直さずに禁止の対象へ入る）
- `sys.path` の書き換えは、本番系では引数によらず禁止する（変数経由の書き換えを
  文字列で見分けることはできない）
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS_DIR = REPO_ROOT / "evals" / "report_ja"

# 本番系。`test_evals_stay_out_of_production.py` と同じ範囲。
PRODUCTION_TREES = ("packages", "evaluators", "extractors", "apps")

# ハーネスの置き場そのものを指す最上位の名前。
HARNESS_PACKAGE_NAMES = frozenset({"evals", "report_ja"})

# `sys.path` を書き換えるメソッド。
SYS_PATH_MUTATORS = frozenset({"insert", "append", "extend", "remove", "clear", "pop"})


def harness_module_names() -> frozenset[str]:
    """ハーネスのモジュール名（`*.py` の stem）と、置き場の名前。"""
    stems = {path.stem for path in HARNESS_DIR.glob("*.py")}
    return frozenset(stems) | HARNESS_PACKAGE_NAMES


def _is_sys_path(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "path"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def find_violations(source: str, forbidden: frozenset[str]) -> list[str]:
    """ハーネスの import と `sys.path` の書き換えを、行番号付きで返す。"""
    violations: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in forbidden:
                    violations.append(f"{node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            # 相対 import（level > 0）はパッケージの中を指すので、ハーネスには届かない。
            if node.level == 0 and node.module is not None:
                top = node.module.split(".")[0]
                if top in forbidden:
                    violations.append(f"{node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in SYS_PATH_MUTATORS
                and _is_sys_path(func.value)
            ):
                violations.append(f"{node.lineno}: sys.path.{func.attr}(...)")
        elif isinstance(node, ast.AugAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AugAssign) else node.targets
            for target in targets:
                base = target.value if isinstance(target, ast.Subscript) else target
                if _is_sys_path(base):
                    violations.append(f"{node.lineno}: sys.path への代入")
    return violations


def _production_sources() -> list[Path]:
    return [
        path
        for tree in PRODUCTION_TREES
        for path in (REPO_ROOT / tree).rglob("*.py")
        if ".venv" not in path.parts and "__pycache__" not in path.parts
    ]


def test_harness_module_names_are_enumerated() -> None:
    """列挙が空なら、下の検査は何も禁止しない。置き場が動いたら気づけるようにする。"""
    names = harness_module_names()
    assert {"rubric", "grade", "measure"} <= names, sorted(names)


def test_the_detector_catches_the_forms_the_regex_missed() -> None:
    """#708 で正規表現をすり抜けた書き方を、この検査は捕まえる。"""
    forbidden = harness_module_names()
    evasive = textwrap.dedent(
        """\
        import sys
        from pathlib import Path
        ROOT = Path(__file__).parent
        HARNESS = ROOT / "evals" / "report_ja"
        sys.path.insert(0, str(HARNESS))
        from rubric import RUBRIC
        import grade
        """
    )
    found = find_violations(evasive, forbidden)
    assert any("sys.path.insert" in v for v in found), found
    assert any("from rubric" in v for v in found), found
    assert any("import grade" in v for v in found), found

    # 変数経由の追加・代入も同じく断る。
    assert find_violations("import sys\nsys.path += [p]\n", forbidden)
    assert find_violations("import sys\nsys.path[:0] = [p]\n", forbidden)
    assert find_violations("import sys\nsys.path.append(p)\n", forbidden)


def test_the_detector_leaves_ordinary_imports_alone() -> None:
    """名前が同じでも、パッケージの中の相対 import や別名の属性は咎めない。"""
    forbidden = harness_module_names()
    ordinary = textwrap.dedent(
        """\
        import os.path
        from . import grade
        from .rubric import RUBRIC
        from aijudge_core import measure_thing
        path = os.path.join("a", "b")
        """
    )
    assert find_violations(ordinary, forbidden) == []


def test_production_code_does_not_import_harness_modules() -> None:
    forbidden = harness_module_names()
    offenders: list[str] = []
    for path in _production_sources():
        source = path.read_text(encoding="utf-8")
        for violation in find_violations(source, forbidden):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{violation}")
    assert not offenders, "本番系がハーネスを取り込んでいる:\n" + "\n".join(offenders)
