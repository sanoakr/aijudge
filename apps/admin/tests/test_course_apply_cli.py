"""コースの定義を流す CLI の入口（`course apply`、#707）。

「最新の版と内容が違えば `--revise` なしでは断る」という規則そのものは
`apply_course_definition` にあり、`test_course_apply.py` と
`test_revision_is_content_based.py` が `revise` を自分で渡して確かめている。
ここで確かめるのは**入口の側**だけ ── `--revise` が引数から関数まで届くか、
断られたときに exit 2 で終わるか。

`revise=args.revise` を `True` に固定すると、通常の流し直しが食い違った課題の
版を黙って上げる（P8 の「断る」関門が CLI から消える）。`False` に固定すると
`--revise` が効かなくなる。関数直呼びのテストはどちらでも通るので、ここで
版の数を DB まで見て両方を落とす。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aijudge_admin.cli import main
from aijudge_core.ids import TenantId
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
TASK_TITLE = "認定証を提出する"
STATEMENT_LINE = "認定証の画面キャプチャを提出してください。"

# 課題ディレクトリを要らない最小の定義（人が採点する 1 問だけ）。
DEFINITION = f"""
course:
  code: cli_apply
  title: 入口の検査
  term: 2026-後期
  subject_profile: cs_network_python
units:
  ex1:
    opens_at: 2026-09-18T13:00:00+09:00
    due_at: 2026-09-25T23:59:00+09:00
tasks:
  - key: ex1/cert
    unit: ex1
    session: 1
    position: 1
    statement: |
      ## [必須] {TASK_TITLE} ##

      {STATEMENT_LINE}
    accepted_suffixes: [.png]
    criteria:
      - code: shown
        title: 認定証が写っているか
        description: 講座名が読めるか。
        weight: 1.0
        evaluator: __human__
        levels:
          - {{level: 0, label: 不足, descriptor: 読めない, score_ratio: 0.0}}
          - {{level: 1, label: 十分, descriptor: 読める, score_ratio: 1.0}}
"""
REVISED = DEFINITION.replace(STATEMENT_LINE, STATEMENT_LINE + "（直した）")


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    url = f"sqlite+pysqlite:///{tmp_path}/apply_cli.db"
    Database.connect(url, create=True).dispose()
    return url


def _apply(db_url: str, tmp_path: Path, text: str, *extra: str) -> int:
    # 流すたびに別のファイルにする（上書きの順序に結果が依らないように）。
    path = tmp_path / f"course-{len(list(tmp_path.glob('course-*.yaml')))}.yaml"
    path.write_text(text, encoding="utf-8")
    return main(
        [
            "--database-url",
            db_url,
            "--tenant",
            str(TENANT),
            "--profiles",
            str(PROFILES),
            "course",
            "apply",
            "--file",
            str(path),
            *extra,
        ]
    )


def _versions(db_url: str) -> list[int]:
    database = Database.connect(db_url)
    try:
        with database.unit_of_work() as uow:
            course = next(c for c in uow.identity.list_courses(TENANT) if c.code == "cli_apply")
            task = next(t for t in uow.tasks.list_for_course(course.id) if t.title == TASK_TITLE)
            return sorted(v.version for v in uow.tasks.list_versions(task.id))
    finally:
        database.dispose()


def test_a_changed_definition_without_revise_exits_2_and_keeps_the_version(
    db_url: str, tmp_path: Path, capsys
) -> None:
    """**`--revise` なしの食い違いは断る。** exit 2 で、版は増えない。

    `revise=True` に固定する変異はここで版 2 ができて落ちる。
    """
    assert _apply(db_url, tmp_path, DEFINITION) == 0
    capsys.readouterr()

    assert _apply(db_url, tmp_path, REVISED) == 2
    assert "--revise" in capsys.readouterr().err
    assert _versions(db_url) == [1]


def test_revise_raises_the_version_by_exactly_one(db_url: str, tmp_path: Path) -> None:
    """`--revise` を付ければ版がちょうど 1 つ上がる。

    `revise=False` に固定する変異はここで exit 2 になって落ちる。
    """
    assert _apply(db_url, tmp_path, DEFINITION) == 0
    assert _apply(db_url, tmp_path, REVISED, "--revise") == 0
    assert _versions(db_url) == [1, 2]


def test_reapplying_the_same_definition_needs_no_revise(db_url: str, tmp_path: Path) -> None:
    """同じ定義の流し直しは `--revise` なしで通り、版も増えない（冪等）。"""
    assert _apply(db_url, tmp_path, DEFINITION) == 0
    assert _apply(db_url, tmp_path, DEFINITION) == 0
    assert _versions(db_url) == [1]
