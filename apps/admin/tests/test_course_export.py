"""コースを定義ファイルの木へ書き出す／ファイルと DB の差を見る（#363）。

**書き出しは往復で確かめる。** 逆写像（DB → 宣言）が正しいかどうかは、
順写像（宣言 → DB）に通してみれば分かる。ここでのテストはその形を取り、
「書き出したものをもう一度流したら同じ課題になる」を確かめる ── これが
成り立たない書き出しは、正本として使えない。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from aijudge_admin import AdminError, DiffState, course_id_for, diff_course, export_course
from aijudge_admin.course_definition import apply_course_definition
from aijudge_admin.operations import _IMPORTER
from aijudge_authoring import TaskSpec, build_task_version
from aijudge_core.ids import TenantId
from aijudge_persistence import Database

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)

COURSE = {
    "course": {
        "code": "prog2",
        "title": "プログラミング及び実習2",
        "term": "2026-後期",
        "subject_profile": "cs_lang_c_intro",
        "description": "C 言語入門の演習。",
        "upload_suffixes": [".c"],
    },
    "units": {
        "ex01": {"opens_at": "2026-09-24T09:00:00+09:00", "due_at": "2026-10-01T23:59:00+09:00"},
        "ex02": {"opens_at": "2026-10-01T09:00:00+09:00", "due_at": "2026-10-08T23:59:00+09:00"},
    },
    "tasks": [
        # 画像で出す課題。**宣言した観点を持ち、人が採点する**（`__human__`）。
        {
            "key": "ex01/p1",
            "unit": "ex01",
            "session": 1,
            "position": 1,
            "accepted_suffixes": [".png", ".jpg", ".pdf"],
            "statement": "## [必須] 環境構築 ##\n\n`cc` を実行した画面を提出してください。\n",
            "criteria": [
                {
                    "code": "cc_shown",
                    "title": "cc の実行が確認できる",
                    "description": "ターミナルで `cc` を実行した結果が読み取れるか。",
                    "weight": 1.0,
                    "evaluator": "__human__",
                    "levels": [
                        {
                            "level": 0,
                            "label": "未達",
                            "descriptor": "写っていない",
                            "score_ratio": 0.0,
                        },
                        {
                            "level": 1,
                            "label": "達成",
                            "descriptor": "読み取れる",
                            "score_ratio": 1.0,
                        },
                    ],
                }
            ],
        },
        # ふつうのコード課題。問題ディレクトリ（`in/` `out/` と参照解答）を持つ。
        {"problem_dir": "ex02/p1", "readability_weight": 0.3},
        # 自動採点が無い課題（ネットワーク演習の常駐サーバ課題がこの形）。
        {
            "key": "ex02/p2",
            "unit": "ex02",
            "session": 2,
            "position": 2,
            "statement": "## [任意] サーバを書く ##\n\n動かして確かめてください。\n",
        },
    ],
}


def _write_source(root: Path) -> Path:
    """取り込み元の木を作る。実運用の `assignments/` と同じ形。"""
    problem = root / "ex02" / "p1"
    (problem / "in").mkdir(parents=True)
    (problem / "out").mkdir(parents=True)
    (problem / "desc.md").write_text(
        "## [必須] 整数の入出力 ##\n\n整数を読んでそのまま出力してください。\n", encoding="utf-8"
    )
    (problem / "in" / "input1.txt").write_text("3\n", encoding="utf-8")
    (problem / "out" / "output1.txt").write_text("3\n", encoding="utf-8")
    (problem / "in" / "input2.txt").write_text("-12\n", encoding="utf-8")
    (problem / "out" / "output2.txt").write_text("-12\n", encoding="utf-8")
    (problem / "echo.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")

    path = root / "course.yaml"
    path.write_text(yaml.safe_dump(COURSE, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _database(path: Path) -> Database:
    return Database.connect(f"sqlite+pysqlite:///{path}", create=True)


@pytest.fixture
def applied(tmp_path: Path):
    """定義を 1 つ流した DB と、その定義ファイル。"""
    source = tmp_path / "source"
    source.mkdir()
    path = _write_source(source)
    database = _database(tmp_path / "a.db")
    apply_course_definition(
        database, path, tenant_id=TENANT, profiles_dir=PROFILES, authored_by=_IMPORTER
    )
    yield database, path
    database.dispose()


def _specs(path: Path) -> dict[str, TaskSpec]:
    from aijudge_admin.course_definition import load_course_definition

    return {spec.key: spec for spec in load_course_definition(path).tasks}


def test_exported_tree_reproduces_the_same_tasks(applied, tmp_path: Path) -> None:
    """**往復して同じになる。** 書き出しを正本にできる条件がこれである。"""
    database, source = applied
    course_id = course_id_for(TENANT, "prog2", "2026-後期")

    out = tmp_path / "exported"
    result = export_course(database, course_id=course_id, out_dir=out)

    assert result.skipped == ()
    assert {task.key for task in result.tasks} == {"ex01/p1", "ex02/p1", "ex02/p2"}

    before, after = _specs(source), _specs(result.path)
    assert set(before) == set(after)
    for key, spec in before.items():
        # 宣言そのものが一致する。**書き方が変わっても中身は変わらない。**
        #
        # 受け付ける拡張子だけは並びが揃わない ── 保存の側が正規化する
        # （`normalize_suffixes`）ので、DB から読めば必ず整列済みになる。
        # 中身は同じなので、比較の前に同じ正規化をかける。
        assert after[key] == spec.model_copy(
            update={"accepted_suffixes": tuple(sorted(spec.accepted_suffixes))}
        ), key


def test_exported_tree_applies_into_an_empty_database(applied, tmp_path: Path) -> None:
    """書き出した木を別の DB に流すと、同じ課題版ができる。"""
    database, _ = applied
    course_id = course_id_for(TENANT, "prog2", "2026-後期")
    out = tmp_path / "exported"
    exported = export_course(database, course_id=course_id, out_dir=out)

    fresh = _database(tmp_path / "b.db")
    try:
        apply_course_definition(
            fresh, exported.path, tenant_id=TENANT, profiles_dir=PROFILES, authored_by=_IMPORTER
        )
        difference = diff_course(fresh, exported.path, tenant_id=TENANT)
        assert difference.in_sync
        # 元の DB とも一致する（流し込み先を変えても同じ課題になる）。
        assert diff_course(database, exported.path, tenant_id=TENANT).in_sync
    finally:
        fresh.dispose()


def test_exported_code_task_keeps_its_problem_directory(applied, tmp_path: Path) -> None:
    """コード課題は `in/` `out/` と参照解答のまま書き出す。

    YAML にテストケースを畳み込むと、既存の課題ディレクトリと形が変わり、
    **講義リポジトリの差分が全面書き換えになる。**
    """
    database, _ = applied
    out = tmp_path / "exported"
    export_course(database, course_id=course_id_for(TENANT, "prog2", "2026-後期"), out_dir=out)

    assert (out / "ex02" / "p1" / "desc.md").is_file()
    assert (out / "ex02" / "p1" / "in" / "input2.txt").read_text(encoding="utf-8") == "-12\n"
    assert (out / "ex02" / "p1" / "out" / "output2.txt").read_text(encoding="utf-8") == "-12\n"
    # 参照解答は拡張子をコースの提出形式から決める（`_reference_name`）。
    assert (out / "ex02" / "p1" / "solution.c").read_text(encoding="utf-8").startswith("int main")

    document = yaml.safe_load((out / "course.yaml").read_text(encoding="utf-8"))
    entry = next(task for task in document["tasks"] if task.get("problem_dir") == "ex02/p1")
    assert "test_cases" not in entry
    assert entry["readability_weight"] == pytest.approx(0.3)


def test_export_refuses_to_overwrite_without_force(applied, tmp_path: Path) -> None:
    database, _ = applied
    course_id = course_id_for(TENANT, "prog2", "2026-後期")
    out = tmp_path / "exported"
    export_course(database, course_id=course_id, out_dir=out)

    with pytest.raises(AdminError, match="空ではありません"):
        export_course(database, course_id=course_id, out_dir=out)
    # 上書きは明示すれば通る（講義リポジトリへの書き戻しは毎回上書きになる）。
    export_course(database, course_id=course_id, out_dir=out, force=True)


def test_export_refuses_a_public_repository_checkout(applied, tmp_path: Path) -> None:
    """公開リポジトリの作業ツリーには書き出さない。

    書き出すのは問題文・テストケース・参照解答で、未公開の回を含む。
    運用機には aijudge（公開）のチェックアウトがあるので、`--out` の
    打ち間違いで参照解答が公開リポジトリに載りうる。
    """
    database, _ = applied
    checkout = tmp_path / "aijudge"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/sanoakr/aijudge.git"],
        cwd=checkout,
        check=True,
    )

    with pytest.raises(AdminError, match="公開リポジトリ"):
        export_course(
            database,
            course_id=course_id_for(TENANT, "prog2", "2026-後期"),
            out_dir=checkout / "subjects" / "assignments",
        )


def test_export_reports_a_task_it_cannot_declare(applied, tmp_path: Path) -> None:
    """宣言に戻せない課題は**書かずに報告する**。

    欠けたまま書くと、その木を正本として流し直した日に課題が静かに変わる。
    `allow_handwriting` は `TaskSpec` に欄が無いので、その形の版がここに来る。
    """
    database, _ = applied
    course_id = course_id_for(TENANT, "prog2", "2026-後期")
    task_id = build_task_version(
        TaskSpec(key="ex02/p2", statement="x", unit="ex02"),
        course_id=course_id,
        subject_profile="cs_lang_c_intro",
        authored_by=_IMPORTER,
    ).task_id

    with database.unit_of_work() as uow:
        version = uow.tasks.latest_version(task_id)
        # 版を 1 つ上げた上で、宣言に欄の無い値を立てる。**ID は版番号から
        # 導かれる**ので、番号だけ変えた写しは前の版と同じ ID を指す。
        handwritten = build_task_version(
            TaskSpec(key="ex02/p2", statement=version.statement, unit="ex02"),
            course_id=course_id,
            subject_profile=version.subject_profile,
            authored_by=_IMPORTER,
            version=version.version + 1,
        ).model_copy(update={"allow_handwriting": True})
        uow.tasks.save_version(handwritten)
        uow.commit()

    result = export_course(database, course_id=course_id, out_dir=tmp_path / "exported")
    assert [skipped.key for skipped in result.skipped] == ["ex02/p2"]
    assert [task.key for task in result.tasks] == ["ex01/p1", "ex02/p1"]


def test_diff_finds_what_only_the_database_has(applied, tmp_path: Path) -> None:
    """コンソールで作った課題は「DB のみ」として出る ── `export` の出番。"""
    database, source = applied
    course_id = course_id_for(TENANT, "prog2", "2026-後期")

    from aijudge_admin import save_task

    save_task(
        database,
        course_id=course_id,
        spec=TaskSpec(key="ex03/p1", statement="## 画面で作った課題 ##\n本文\n", unit="ex03"),
        subject_profile="cs_lang_c_intro",
        authored_by=_IMPORTER,
    )

    difference = diff_course(database, source, tenant_id=TENANT)
    assert not difference.in_sync
    assert [(d.key, d.state) for d in difference.differences] == [("ex03/p1", DiffState.ONLY_DB)]


def test_diff_finds_a_statement_edited_in_the_console(applied, tmp_path: Path) -> None:
    database, source = applied
    course_id = course_id_for(TENANT, "prog2", "2026-後期")

    from aijudge_admin import save_task

    save_task(
        database,
        course_id=course_id,
        spec=TaskSpec(
            key="ex02/p2",
            statement="## [任意] サーバを書く ##\n\n画面で書き直した本文。\n",
            unit="ex02",
            session=2,
            position=2,
        ),
        subject_profile="cs_lang_c_intro",
        authored_by=_IMPORTER,
        revise=True,
    )

    difference = diff_course(database, source, tenant_id=TENANT)
    assert [(d.key, d.state) for d in difference.differences] == [("ex02/p2", DiffState.CHANGED)]


def test_diff_finds_a_task_that_was_never_applied(applied, tmp_path: Path) -> None:
    database, source = applied
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    document["tasks"].append(
        {
            "key": "ex02/p3",
            "unit": "ex02",
            "session": 2,
            "position": 3,
            "statement": "## [任意] まだ流していない課題 ##\n本文\n",
        }
    )
    source.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    difference = diff_course(database, source, tenant_id=TENANT)
    assert [(d.key, d.state) for d in difference.differences] == [("ex02/p3", DiffState.ONLY_FILE)]


def test_diff_says_the_course_is_missing_rather_than_in_sync(tmp_path: Path) -> None:
    """**「判定できない」を「一致」にしない。** 公開の手順がこれを門に使う。"""
    source = tmp_path / "source"
    source.mkdir()
    path = _write_source(source)
    database = _database(tmp_path / "empty.db")
    try:
        difference = diff_course(database, path, tenant_id=TENANT)
        assert not difference.course_exists
        assert not difference.in_sync
    finally:
        database.dispose()


def test_applying_an_export_twice_does_not_raise_the_versions(applied, tmp_path: Path) -> None:
    """書き出した木を `--revise` で流し直しても版は増えない。

    `course apply --revise` を公開の手順に置く以上、**流すたびに全課題の版が
    増える**ようでは使えない（訂正の記録が読めなくなる）。
    """
    database, _ = applied
    course_id = course_id_for(TENANT, "prog2", "2026-後期")
    exported = export_course(database, course_id=course_id, out_dir=tmp_path / "exported")

    def versions() -> dict[str, int]:
        with database.unit_of_work() as uow:
            return {
                str(uow.tasks.latest_version(task.id).source_key): uow.tasks.latest_version(
                    task.id
                ).version
                for task in uow.tasks.list_for_course(course_id)
            }

    before = versions()
    for _ in range(2):
        apply_course_definition(
            database,
            exported.path,
            tenant_id=TENANT,
            profiles_dir=PROFILES,
            authored_by=_IMPORTER,
            revise=True,
        )
    assert versions() == before


def test_re_exporting_into_the_lecture_repository_keeps_the_existing_layout(
    applied, tmp_path: Path
) -> None:
    """既にある課題ディレクトリへ書き戻しても、読み直しの結果が変わらない。

    `find_reference_solution` は拡張子ごとに `sorted(glob)` の先頭を採る。
    `echo.c` がある所へ `solution.c` を足すと、**書いたのは `solution.c` なのに
    読まれるのは `echo.c`** になり、参照解答を直したつもりが反映されない。
    実運用の書き戻し先は常に「既にある `assignments/`」なので、この形が既定。
    """
    database, source = applied
    course_id = course_id_for(TENANT, "prog2", "2026-後期")
    out = source.parent

    exported = export_course(database, course_id=course_id, out_dir=out, force=True)

    assert (out / "ex02" / "p1" / "echo.c").is_file()
    assert not (out / "ex02" / "p1" / "solution.c").exists()
    assert diff_course(database, exported.path, tenant_id=TENANT).in_sync


def test_re_export_drops_a_reference_solution_that_was_removed(applied, tmp_path: Path) -> None:
    """参照解答を消した課題を書き戻すと、ファイルも消える。

    `find_reference_solution` は拡張子ごとに `sorted(glob)` の先頭を拾うので、
    書き出しが古いファイルを残すと、**消したはずの参照解答が読み直しで
    生き返る**。実測（2026-09-22）で 2 度目の書き出しが自己検算で止まった。
    """
    from aijudge_admin import save_task

    database, _ = applied
    course_id = course_id_for(TENANT, "prog2", "2026-後期")
    out = tmp_path / "exported"
    export_course(database, course_id=course_id, out_dir=out)
    assert (out / "ex02" / "p1" / "solution.c").is_file()

    # コンソールで問題文だけを直すと、参照解答もテストケースも持たない版になる。
    save_task(
        database,
        course_id=course_id,
        spec=TaskSpec(
            key="ex02/p1",
            statement="## [必須] 整数の入出力 ##\n\n画面で直した。\n",
            unit="ex02",
            session=2,
            position=1,
            readability_weight=0.3,
        ),
        subject_profile="cs_lang_c_intro",
        authored_by=_IMPORTER,
        revise=True,
    )

    exported = export_course(database, course_id=course_id, out_dir=out, force=True)
    assert not (out / "ex02" / "p1" / "solution.c").exists()
    assert not (out / "ex02" / "p1" / "in").exists()
    assert diff_course(database, exported.path, tenant_id=TENANT).in_sync
