"""伴走プロセス宣言の読み込みを検証する（ADR 0008）。

固定したいのは「黙って通さない」こと。宣言の誤りが静かに
「自動採点されない課題」や「どんな提出でも通るケース」になってはならない。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aijudge_authoring.importers import companion, sharif_judge
from aijudge_core.ids import UserId

AUTHOR = UserId("usr_" + "a" * 32)

ECHO_SERVER = "import socket\n# 伴走サーバ（教材の echoServer.py 相当）\n"

CLIENT_YAML = """\
role: client
companion: echoServer.py
port: 50007
cases:
  - name: case1
    input: "{host}\\n{port}\\n"
    expected_contains:
      - "Send b'Hello, world'"
      - "Received b'Hello, world'"
  - name: case2
    input: "{host}\\n{port}\\n"
    expected_contains: "Received b'Hello, world'"
"""

SERVER_YAML = """\
role: server
companion: httpCheckClient.py
port: 8080
fixtures:
  - server.html
cases:
  - name: case1
    input: "{port}\\nserver.html\\n"
    companion_input: "{port}\\nserver.html\\n"
    expected_contains: "port=8080"
    companion_expected_contains: "これは server.html です"
"""


def _problem(tmp_path: Path, yaml_text: str, *, extra: dict[str, str] | None = None) -> Path:
    problem = tmp_path / "ex4" / "p1"
    problem.mkdir(parents=True)
    (problem / "desc.md").write_text("## echoClient2.py\n\n接続しなさい\n", encoding="utf-8")
    (problem / "companion.yaml").write_text(yaml_text, encoding="utf-8")
    (problem / "echoServer.py").write_text(ECHO_SERVER, encoding="utf-8")
    for name, content in (extra or {}).items():
        (problem / name).write_text(content, encoding="utf-8")
    return problem


# --------------------------------------------------------------------------
# 読める形
# --------------------------------------------------------------------------


def test_a_client_declaration_becomes_test_cases(tmp_path: Path) -> None:
    cases = companion.load_companion_cases(_problem(tmp_path, CLIENT_YAML))
    assert [case.name for case in cases] == ["case1", "case2"]
    assert all(case.evaluator_id == "network_test_runner" for case in cases)
    payload = cases[0].payload
    assert payload["role"] == "client"
    assert payload["port"] == 50007
    # 伴走プロセスの中身が焼き込まれている（採点時にファイルを探さない）。
    assert payload["companion"] == ECHO_SERVER
    assert payload["companion_name"] == "echoServer.py"


def test_a_single_string_expectation_is_accepted(tmp_path: Path) -> None:
    """1 つだけ書くときに角括弧を強制しない。"""
    cases = companion.load_companion_cases(_problem(tmp_path, CLIENT_YAML))
    assert cases[1].payload["expected_contains"] == ["Received b'Hello, world'"]


def test_a_server_declaration_carries_fixtures(tmp_path: Path) -> None:
    problem = _problem(
        tmp_path,
        SERVER_YAML,
        extra={
            "httpCheckClient.py": "# 伴走クライアント\n",
            "server.html": "<html>これは server.html です</html>",
        },
    )
    cases = companion.load_companion_cases(problem)
    payload = cases[0].payload
    assert payload["role"] == "server"
    assert "server.html" in payload["fixtures"]
    assert payload["companion_expected_contains"] == ["これは server.html です"]


def test_the_importer_uses_the_companion_declaration(tmp_path: Path) -> None:
    """`in/` `out/` が無くても自動採点できる課題になること。"""
    problem = _problem(tmp_path, CLIENT_YAML)
    version = sharif_judge.import_problem(problem, subject_profile="net_python", authored_by=AUTHOR)
    assert len(version.test_cases) == 2
    assert [c.evaluator_id for c in version.criteria] == ["network_test_runner"], (
        "AI 観点に落ちている（伴走プロセスの宣言が使われていない）"
    )


def test_the_importer_still_prefers_in_out_when_there_is_no_declaration(
    tmp_path: Path,
) -> None:
    problem = tmp_path / "ex1" / "p1"
    (problem / "in").mkdir(parents=True)
    (problem / "out").mkdir(parents=True)
    (problem / "desc.md").write_text("## sum.py\n\n和を出せ\n", encoding="utf-8")
    (problem / "in" / "input1.txt").write_text("1\n2\n", encoding="utf-8")
    (problem / "out" / "output1.txt").write_text("3\n", encoding="utf-8")

    version = sharif_judge.import_problem(problem, subject_profile="net_python", authored_by=AUTHOR)
    assert [c.evaluator_id for c in version.criteria] == ["code_test_runner"]


# --------------------------------------------------------------------------
# 黙って通さない
# --------------------------------------------------------------------------


def test_an_unknown_role_is_refused(tmp_path: Path) -> None:
    with pytest.raises(companion.CompanionError, match="role must be"):
        companion.load_companion_cases(
            _problem(tmp_path, CLIENT_YAML.replace("role: client", "role: peer"))
        )


def test_a_missing_companion_file_is_refused(tmp_path: Path) -> None:
    """伴走プロセスは教材のファイルを指す。生成しない（ADR 0008）。"""
    with pytest.raises(companion.CompanionError, match="is not in"):
        companion.load_companion_cases(
            _problem(tmp_path, CLIENT_YAML.replace("echoServer.py", "generated.py"))
        )


def test_a_privileged_port_is_refused(tmp_path: Path) -> None:
    """1024 未満は課題側の要求（well-known は使えない）でもあり、
    コンテナ内の非 root では bind できない。"""
    with pytest.raises(companion.CompanionError, match="port must be"):
        companion.load_companion_cases(
            _problem(tmp_path, CLIENT_YAML.replace("port: 50007", "port: 80"))
        )


def test_a_case_that_checks_nothing_is_refused(tmp_path: Path) -> None:
    """何も照合しないケースは、どんな提出でも通る。"""
    stripped = """\
role: client
companion: echoServer.py
port: 50007
cases:
  - name: case1
    input: "x\\n"
"""
    with pytest.raises(companion.CompanionError, match="checks nothing"):
        companion.load_companion_cases(_problem(tmp_path, stripped))


def test_an_empty_case_list_is_refused(tmp_path: Path) -> None:
    empty = "role: client\ncompanion: echoServer.py\nport: 50007\ncases: []\n"
    with pytest.raises(companion.CompanionError, match="non-empty list"):
        companion.load_companion_cases(_problem(tmp_path, empty))


def test_a_missing_fixture_is_refused(tmp_path: Path) -> None:
    """採点時に「ファイルが無い」で全員落ちるのを取り込み時に止める。"""
    problem = _problem(tmp_path, SERVER_YAML, extra={"httpCheckClient.py": "# 伴走クライアント\n"})
    with pytest.raises(companion.CompanionError, match="fixture"):
        companion.load_companion_cases(problem)


def test_broken_yaml_is_refused_not_silently_skipped(tmp_path: Path) -> None:
    """宣言があるのに読めないなら例外にする。

    空を返すと「テストケースが 0 件の課題」として取り込まれ、宣言を書いたのに
    自動採点されない状態が静かに生まれる。
    """
    with pytest.raises(companion.CompanionError):
        companion.load_companion_cases(_problem(tmp_path, "role: client\n  bad indent\n"))


# --------------------------------------------------------------------------
# 回のまとまり（何回目の何問目か）
# --------------------------------------------------------------------------


def test_the_unit_and_position_come_from_the_directory(tmp_path: Path) -> None:
    """`ex06/p3` から「第 6 回・3 問目」を取る。

    1 回の授業で複数問出るので、これが分からないと一覧が平らになり、
    何回目の分を見ているのか分からなくなる。
    """
    problem = tmp_path / "ex06" / "p3"
    problem.mkdir(parents=True)
    unit, session, position = sharif_judge.parse_unit(problem)
    assert (unit, session, position) == ("ex06", 6, 3)


def test_a_re_run_year_keeps_its_session(tmp_path: Path) -> None:
    """`ex07-2023` は第 7 回の別年度版。"""
    problem = tmp_path / "ex07-2023" / "p1"
    problem.mkdir(parents=True)
    assert sharif_judge.parse_unit(problem)[:2] == ("ex07-2023", 7)


def test_a_unit_without_a_session_keeps_its_name(tmp_path: Path) -> None:
    """`exam08` は回に対応しない。名前だけ残す。"""
    problem = tmp_path / "exam08" / "p2"
    problem.mkdir(parents=True)
    unit, session, position = sharif_judge.parse_unit(problem)
    assert (unit, session, position) == ("exam08", None, 2)
