"""伴走プロセスの宣言（`companion.yaml`）を読む。

クライアント／サーバ課題は `in/` `out/` の形に乗らない（ADR 0008）。標準入力を
与えて標準出力を比べるのではなく、伴走プロセスを立てて通信させる必要がある。
その段取りを課題ディレクトリの `companion.yaml` に宣言する。

    # ex4/p1/companion.yaml
    role: client                    # client: 提出が接続する / server: 提出が待ち受ける
    companion: echoServer.py        # 同じディレクトリのファイル
    port: 50007
    cases:
      - name: case1
        input: "{host}\\n{port}\\n"   # 提出への標準入力
        expected_contains:
          - "Send b'Hello, world'"
          - "Received b'Hello, world'"

`{host}` と `{port}` は採点時に伴走プロセスの値へ差し替わる。課題の入力には
本番のホスト・ポート（`133.83.80.110` など）が書かれているので、そのままでは
採点機の設置場所に依存する。

**伴走プロセスは教材のファイルを指す。** 生成しない（ADR 0008）。
`echoServer.py` は課題文が名指ししている相手で、それに対して採点することが
「課題の指示どおりか」の定義そのものになる。

期待値は**部分一致**（`expected_contains`）にしてある。サーバの出力には
接続元の一時ポート（`('127.0.0.1', 53578)`）のように毎回変わる値が混ざるため、
完全一致では常に落ちる。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from aijudge_core import TestCase

COMPANION_FILE = "companion.yaml"
EVALUATOR_ID = "network_test_runner"

ROLES = ("client", "server")


class CompanionError(Exception):
    """宣言が壊れている。行や項目名を添えて返す。"""


def has_companion(problem_dir: Path) -> bool:
    return (problem_dir / COMPANION_FILE).is_file()


def load_companion_cases(problem_dir: Path) -> tuple[TestCase, ...]:
    """`companion.yaml` を読んで TestCase にする。

    **黙って空を返さない。** 宣言があるのに読めないなら例外にする。空を返すと
    「テストケースが 0 件の課題」として取り込まれ、宣言を書いたのに
    自動採点されない状態が静かに生まれる。
    """
    path = problem_dir / COMPANION_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CompanionError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CompanionError(f"{path} does not contain a mapping")

    role = str(data.get("role", "")).strip().lower()
    if role not in ROLES:
        raise CompanionError(f"{path}: role must be one of {ROLES}, got {role!r}")

    companion_name = str(data.get("companion", "")).strip()
    if not companion_name:
        raise CompanionError(f"{path}: 'companion' names the companion program file")
    companion_path = problem_dir / companion_name
    if not companion_path.is_file():
        raise CompanionError(
            f"{path}: companion {companion_name!r} is not in {problem_dir}. "
            "伴走プロセスは教材のファイルを指すこと（生成しない、ADR 0008）"
        )
    companion_source = companion_path.read_text(encoding="utf-8", errors="replace")

    port = data.get("port")
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise CompanionError(f"{path}: port must be an int in [1024, 65535], got {port!r}")

    fixtures: dict[str, str] = {}
    for name in data.get("fixtures", ()) or ():
        fixture_path = problem_dir / str(name)
        if not fixture_path.is_file():
            raise CompanionError(f"{path}: fixture {name!r} is not in {problem_dir}")
        fixtures[str(name)] = fixture_path.read_text(encoding="utf-8", errors="replace")

    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise CompanionError(f"{path}: 'cases' must be a non-empty list")

    cases: list[TestCase] = []
    for index, raw in enumerate(raw_cases, 1):
        if not isinstance(raw, dict):
            raise CompanionError(f"{path}: case {index} is not a mapping")
        cases.append(
            _case(
                path=path,
                index=index,
                raw=raw,
                role=role,
                companion_name=companion_name,
                companion_source=companion_source,
                port=port,
                fixtures=fixtures,
            )
        )
    return tuple(cases)


def _case(
    *,
    path: Path,
    index: int,
    raw: dict[str, Any],
    role: str,
    companion_name: str,
    companion_source: str,
    port: int,
    fixtures: dict[str, str],
) -> TestCase:
    expected = _as_strings(raw.get("expected_contains"))
    companion_expected = _as_strings(raw.get("companion_expected_contains"))
    if not expected and not companion_expected:
        # 何も照合しないケースは、どんな提出でも通る。
        raise CompanionError(
            f"{path}: case {index} checks nothing; give expected_contains "
            "or companion_expected_contains"
        )
    return TestCase(
        name=str(raw.get("name") or f"case{index}"),
        evaluator_id=EVALUATOR_ID,
        payload={
            "role": role,
            "companion": companion_source,
            "companion_name": companion_name,
            "port": int(raw.get("port", port)),
            "input": str(raw.get("input", "")),
            "companion_input": str(raw.get("companion_input", "")),
            "fixtures": fixtures,
            "expected_contains": list(expected),
            "companion_expected_contains": list(companion_expected),
        },
        hidden=bool(raw.get("hidden", True)),
        weight=float(raw.get("weight", 1.0)),
    )


def _as_strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    raise CompanionError(f"expected a string or a list of strings, got {type(value).__name__}")
