"""`course apply` の `problem_dir:` は `companion.yaml` も読む（2026-10-02）。

クライアント・サーバの課題の段取りは `companion.yaml` に書く（ADR 0008）。以前は
`task import` だけが読み、`course apply` は `in/` `out/` しか探さなかったので、
宣言があっても入出力 0 件として取り込まれ、正しさの観点が AI 判定に落ちていた。
固定したいのは 3 つ。

読む         `companion.yaml` のケースが、評価器と payload ごと入る
担当を合わせる 観点を書かなければ、正しさの観点は network_test_runner が担当する
黙らない     観点を書いていてどれもケースを読まないなら、取り込みを断る
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_course_apply import AUTHOR, PROFILES, TENANT, _tasks
from test_course_apply import database as database  # フィクスチャを借りる

from aijudge_course_admin.course_definition import apply_course_definition, load_course_definition
from aijudge_course_admin.operations import AdminError
from aijudge_persistence import Database

RUNNER = "network_test_runner"
SERVER = "import socket\nHOST = ''\nPORT = 50007\n"
COMPANION = """
role: client
companion: echoServer.py
port: 50007
cases:
  - name: IP アドレスで接続
    input: "{host}\\n{port}\\n"
    expected_contains:
      - "Received b'Hello, world'"
"""
HEAD = """
course:
  code: network
  title: ネットワーク及び演習
  term: 2026-後期
  subject_profile: cs_network_python
  upload_suffixes: [.py]
tasks:
  - problem_dir: ex4/p1
"""
DECLARED_CODE_RUNNER = """
    criteria:
      - code: correctness
        title: 動作
        description: 仕様どおりに動くか。
        weight: 1.0
        evaluator: code_test_runner
        levels:
          - {level: 0, label: 未達, descriptor: 動かない, score_ratio: 0.0}
          - {level: 1, label: 達成, descriptor: 動く, score_ratio: 1.0}
"""


def _write(root: Path, text: str = HEAD) -> Path:
    problem = root / "ex4" / "p1"
    problem.mkdir(parents=True)
    (problem / "desc.md").write_text(
        "## [必須] echoClient2.py ##\n\n接続先を入力から読む。\n", encoding="utf-8"
    )
    (problem / "echoServer.py").write_text(SERVER, encoding="utf-8")
    (problem / "companion.yaml").write_text(COMPANION, encoding="utf-8")
    path = root / "course.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_the_companion_cases_are_read_with_their_evaluator(tmp_path: Path) -> None:
    (task,) = load_course_definition(_write(tmp_path)).tasks

    assert task.evaluator == RUNNER
    (case,) = task.test_cases
    assert case.evaluator == RUNNER
    assert case.payload["companion"] == SERVER
    assert case.payload["port"] == 50007 and case.payload["role"] == "client"


def test_applying_makes_the_correctness_criterion_run_the_network_cases(
    database: Database, tmp_path: Path
) -> None:
    applied = apply_course_definition(
        database, _write(tmp_path), tenant_id=TENANT, profiles_dir=PROFILES, authored_by=AUTHOR
    )

    _tasks_by_title, versions = _tasks(database, applied.course.id)
    (version,) = versions.values()
    assert [case.evaluator_id for case in version.test_cases] == [RUNNER]
    assert any(criterion.evaluator_id == RUNNER for criterion in version.criteria), (
        "正しさの観点がケースを読まないと、ケースは保存で外れて AI 判定に落ちる"
    )


def test_declared_criteria_that_never_read_the_cases_are_refused(tmp_path: Path) -> None:
    with pytest.raises(AdminError, match=RUNNER):
        load_course_definition(_write(tmp_path, HEAD + DECLARED_CODE_RUNNER))
