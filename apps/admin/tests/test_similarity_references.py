"""提出を、指定した相手（他の課題・過年度の束）と比べる（#653）。

固定したいこと:

指定      相手は課題の `similarity_references`。他の課題は導出した ID で引き、無ければ比べない
束        取り込むのは取り出した本文だけ。画面に出る要約に元のファイル名を出さない
比べ方    この課題の提出 × 相手だけ。相手どうし・提出どうしの組は残さない
出さない  相手の側は出どころと類似度だけ。Dolos の画面は開かない
後始末    束を消したら、それと比べた報告も消える。束の置き場所はコースとして消さない
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from test_code_similarity import (
    ABOUT_TCP,
    AT,
    SAID_AGAIN,
    FakeGateway,
    _report,
    _settings,
)
from test_code_similarity import database as database  # フィクスチャを借りる
from test_code_similarity import fake as fake
from test_code_similarity import root as root
from test_code_similarity import store as store
from test_code_similarity import task as task

from aijudge_core import ArtifactKind, SimilarityReference
from aijudge_course_admin import similarity_archive
from aijudge_course_admin.code_similarity import (
    SimilarityMode,
    latest_run,
    run_dir,
    sweep_orphans,
)
from aijudge_course_admin.reference_similarity import run_references


def _refer(database, task, *references: SimilarityReference) -> None:
    with database.unit_of_work() as uow:
        uow.tasks.save_task(task.model_copy(update={"similarity_references": references}))
        uow.commit()


def _bundle(tmp_path: Path, root: Path, name: str = "network-2025-r1") -> None:
    source = tmp_path / f"bundle-{name}"
    (source / "sub").mkdir(parents=True, exist_ok=True)
    (source / "y250001_report.md").write_text(SAID_AGAIN, encoding="utf-8")
    (source / "sub" / "y250002.md").write_text("DNS のキャッシュについて書いた。" * 5, "utf-8")
    (source / "memo.xlsx").write_bytes(b"ignored")
    similarity_archive.import_archive(
        root, name=name, label="2025 年度 ネットワーク レポート 1", source=source
    )


def test_a_reference_names_exactly_one_kind() -> None:
    SimilarityReference(archive="a")
    SimilarityReference(task="ex01/p1")
    SimilarityReference(course="network", term="2025-後期", task="r1")
    for bad in (
        {},
        {"task": "a", "archive": "b"},
        {"archive": "a", "course": "x", "term": "y"},
        {"course": "network", "task": "r1"},
    ):
        with pytest.raises(ValueError):
            SimilarityReference(**bad)


def test_an_archive_keeps_only_text_and_hides_file_names(tmp_path: Path, root: Path) -> None:
    _bundle(tmp_path, root)
    manifest = similarity_archive.read_manifest(root, "network-2025-r1")
    assert manifest is not None and [d.id for d in manifest.documents] == ["D-001", "D-002"]
    stored = sorted(
        p.name for p in (root / "archives" / "network-2025-r1").rglob("*") if p.is_file()
    )
    assert stored == ["D-001.txt", "D-002.txt", "manifest.json"], "元のファイルは置かない"
    assert "y250001" not in similarity_archive.manifest_json(manifest), "学籍番号を出さない"

    with pytest.raises(similarity_archive.ArchiveError):
        _bundle(tmp_path, root)  # 同じ名前は入れ直さない（先に消す）
    with pytest.raises(similarity_archive.ArchiveError):
        similarity_archive.import_archive(root, name="../x", label="x", source=tmp_path)
    assert similarity_archive.delete_archive(root, "network-2025-r1")
    assert similarity_archive.list_archives(root) == []


def test_text_against_an_archive_keeps_only_learner_reference_pairs(
    database, store, task, root, fake, tmp_path: Path
) -> None:
    _bundle(tmp_path, root)
    _refer(database, task, SimilarityReference(archive="network-2025-r1"))
    _report(database, store, task, "1", ABOUT_TCP)
    _report(database, store, task, "2", ABOUT_TCP)
    fake.pairs = (
        "id,leftFilePath,rightFilePath,similarity,longestFragment\n"
        "0,ds/S-001.txt,ds/S-002.txt,0.99,50\n"  # 提出どうし（ふだんの回が見る）
        "1,ds/R-001.txt,ds/S-002.txt,0.61,30\n"  # 相手が左でも、提出を左にそろえる
        "2,ds/R-001.txt,ds/R-002.txt,0.20,5"  # 相手どうし
    )

    run = run_references(
        database,
        task.id,
        artifact_store=store,
        root=root,
        mode=SimilarityMode.TEXT,
        now=AT,
        sandbox_factory=fake,
    )

    assert run is not None and run.measured and run.against_references
    assert [r.label for r in run.references] == [
        "2025 年度 ネットワーク レポート 1 の D-001",
        "2025 年度 ネットワーク レポート 1 の D-002",
    ]
    files = fake.seen[0]
    assert sorted(files) == [
        "ds/R-001.txt",
        "ds/R-002.txt",
        "ds/S-001.txt",
        "ds/S-002.txt",
        "ds/info.csv",
    ]
    data = run_dir(root, run) / "data"
    assert sorted(p.name for p in data.iterdir()) == ["pairs.csv"], "相手の本文は持ち出さない"
    rows = list(csv.DictReader((data / "pairs.csv").open()))
    assert [(r["leftFilePath"], r["rightFilePath"]) for r in rows] == [("S-002.txt", "R-001.txt")]
    # ふだんの回（提出どうし）とは別に残る。
    assert latest_run(root, task.course_id, task.id, SimilarityMode.TEXT) is None
    assert latest_run(root, task.course_id, task.id, SimilarityMode.TEXT, against=True) == run

    # 束を消せば、比べた報告も消える。束の置き場所はコースとして消さない。
    _bundle(tmp_path, root, name="other-bundle")
    similarity_archive.delete_archive(root, "network-2025-r1")
    removed = sweep_orphans(database, root)
    assert run_dir(root, run) in removed
    assert (root / "archives" / "other-bundle").is_dir()


def test_content_against_another_task_and_the_cache(
    database, store, task, root, fake, tmp_path: Path
) -> None:
    """他の課題（ここでは同じコースの別の課題）とも比べる。束の埋め込みは使い回す。"""
    _bundle(tmp_path, root)
    _report(database, store, task, "1", ABOUT_TCP)
    gateway = FakeGateway()
    settings = _settings(gateway)
    _refer(
        database,
        task,
        SimilarityReference(archive="network-2025-r1"),
        SimilarityReference(task="ex01/none"),  # 無い課題は比べない（壊れない）
    )
    kwargs = {
        "artifact_store": store,
        "root": root,
        "mode": SimilarityMode.CONTENT,
        "content": settings,
        "sandbox_factory": fake,
    }

    run = run_references(database, task.id, now=AT, **kwargs)

    assert run is not None and run.measured and fake.argv == []
    assert "ex01/none" in run.detail
    rows = list(csv.DictReader((run_dir(root, run) / "data" / "pairs.csv").open()))
    # 束はパスの順に番号が付く（`sub/` が先）。TCP のレポートは 2 番目。
    assert rows[0]["rightFilePath"] == "R-002.txt" and float(rows[0]["similarity"]) > 0.99
    cached = list((root / "archives" / "network-2025-r1" / "embeddings").rglob("*.npy"))
    assert len(cached) == 2
    sent = sum(len(texts) for texts, _ in gateway.seen)
    run_references(database, task.id, now=AT, force=True, **kwargs)
    again = sum(len(texts) for texts, _ in gateway.seen) - sent
    assert again == 1, "束の文書は送り直さない（この課題の提出だけ）"


def test_no_references_means_no_run(database, store, task, root, fake) -> None:
    _report(database, store, task, "1", ABOUT_TCP, kind=ArtifactKind.MARKDOWN)
    assert (
        run_references(
            database,
            task.id,
            artifact_store=store,
            root=root,
            mode=SimilarityMode.TEXT,
            now=AT,
            sandbox_factory=fake,
        )
        is None
    )


def test_course_yaml_carries_references_and_a_copy_pins_them(tmp_path: Path) -> None:
    from test_code_similarity import AUTHOR, DEFINITION, PROFILES, TENANT

    from aijudge_course_admin.course_copy import duplicate_course
    from aijudge_course_admin.course_definition import apply_course_definition
    from aijudge_persistence import Database

    definition = tmp_path / "course.yaml"
    definition.write_text(
        DEFINITION.replace(
            "    accepted_suffixes: [.c, .py]\n",
            "    accepted_suffixes: [.c, .py]\n"
            "    similarity_references:\n"
            "      - {task: ex01/p0}\n"
            "      - {archive: sim-2025-r1}\n",
        ),
        encoding="utf-8",
    )
    db = Database.connect(f"sqlite+pysqlite:///{tmp_path}/yaml.db", create=True)
    try:
        course = apply_course_definition(
            db, path=definition, tenant_id=TENANT, profiles_dir=PROFILES, authored_by=AUTHOR
        ).course
        with db.unit_of_work() as uow:
            (task,) = uow.tasks.list_for_course(course.id)
        assert task.similarity_references == (
            SimilarityReference(task="ex01/p0"),
            SimilarityReference(archive="sim-2025-r1"),
        )

        copied = duplicate_course(
            db,
            source_id=course.id,
            code="sim-demo",
            title="次の年度",
            term="2027-後期",
            authored_by=AUTHOR,
            profiles_dir=PROFILES,
        )
        with db.unit_of_work() as uow:
            (moved,) = uow.tasks.list_for_course(copied.course.id)
        # 写した先の同じ課題ではなく、**元のコースの課題**を指す。束はそのまま。
        assert moved.similarity_references == (
            SimilarityReference(course="sim-demo", term=course.term, task="ex01/p0"),
            SimilarityReference(archive="sim-2025-r1"),
        )
    finally:
        db.dispose()
