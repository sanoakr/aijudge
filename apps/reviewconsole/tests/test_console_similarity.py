"""教員が提出どうしの類似を見る画面（#203・ADR 0029）。

固定したいのは次のこと。

教員だけ     TA には開けない。他のコースの報告は開けない。
見たら残る   入口も Dolos の画面も、開いたことが監査ログに残る。
判定しない   「盗用」と書かない。仮の名前と学習者の対応は教員にだけ出す。
外へ出ない   Dolos の画面には外への通信を塞ぐ CSP と no-referrer を付ける。
抜けない     data/ はその回の 4 ファイルだけ。配布物の外のファイルは返さない。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_manage import World, _import_example
from test_manage import world as world  # フィクスチャを借りる

from aijudge_core import (
    Artifact,
    ArtifactKind,
    ArtifactRole,
    Role,
    Submission,
    SubmissionState,
)
from aijudge_core.ids import ArtifactId, CourseId, SubmissionId, TaskId
from aijudge_course_admin.code_similarity import (
    DATA_DIR,
    DOLOS_IMAGE,
    REPORT_FILES,
    RunSubmission,
    SimilarityMode,
    SimilarityRun,
    run_dir,
)
from aijudge_reviewconsole.similarity import VIEWER_CSP

NOW = datetime(2026, 10, 2, 1, 0, tzinfo=UTC)
PAIRS = (
    "id,leftFileId,leftFilePath,rightFileId,rightFilePath,similarity,totalOverlap,"
    "longestFragment,leftCovered,rightCovered\n"
    "1,0,ds/S-001.c,1,ds/S-002.c,0.97,40,30,20,20\n"
)


@pytest.fixture
def places(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "similarity"
    web = tmp_path / "dolos-web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<html>dolos</html>", encoding="utf-8")
    (web / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    (web / "assets" / "escape.txt").symlink_to(tmp_path / "secret.txt")
    monkeypatch.setenv("AIJUDGE_SIMILARITY_DIR", str(root))
    monkeypatch.setenv("AIJUDGE_DOLOS_WEB_DIR", str(web))
    return root, web


def _submission(world: World, task_id: str, learner) -> SubmissionId:
    submission_id = SubmissionId("sub_" + str(learner.user_id)[4:])
    with world.database.unit_of_work() as uow:
        if uow.submissions.get(submission_id) is not None:
            return submission_id
        version = uow.tasks.latest_version(TaskId(task_id))
        uow.submissions.save(
            Submission(
                id=submission_id,
                task_version_id=version.id,
                learner_id=learner.user_id,
                state=SubmissionState.SUBMITTED,
                submitted_at=NOW,
                artifacts=(
                    Artifact(
                        id=ArtifactId("art_" + str(learner.user_id)[4:]),
                        submission_id=submission_id,
                        role=ArtifactRole.ORIGINAL,
                        kind=ArtifactKind.CODE,
                        filename="main.c",
                        storage_key=f"code/{learner.user_id}.c",
                        content_hash="0" * 64,
                        byte_size=1,
                        created_at=NOW,
                    ),
                ),
                created_at=NOW,
            )
        )
        uow.commit()
    return submission_id


def _report(
    world: World,
    root: Path,
    course_id: CourseId,
    task_id: str,
    learners,
    *,
    mode: SimilarityMode = SimilarityMode.CODE,
    pairs: str = PAIRS,
) -> SimilarityRun:
    run = SimilarityRun(
        run_id="20261002T010000000000Z" + ("" if mode is SimilarityMode.CODE else "-text"),
        mode=mode,
        course_id=course_id,
        task_id=TaskId(task_id),
        unit="ex01",
        task_title="合計を出す",
        created_at=NOW,
        image=DOLOS_IMAGE,
        language="c" if mode is SimilarityMode.CODE else "char",
        input_hash="h",
        submissions=tuple(
            RunSubmission(
                submission_id=_submission(world, task_id, learner),
                pseudonym=f"S-{i:03d}",
                filename="main.c",
                content_hash="0" * 64,
            )
            for i, learner in enumerate(learners, start=1)
        ),
    )
    directory = run_dir(root, run)
    (directory / DATA_DIR).mkdir(parents=True)
    (directory / "run.json").write_text(run.model_dump_json(), encoding="utf-8")
    for name in REPORT_FILES:
        (directory / DATA_DIR / name).write_text(
            pairs if name == "pairs.csv" else f"{name}\n", encoding="utf-8"
        )
    return run


def _viewed(world: World) -> list:
    with world.database.unit_of_work() as uow:
        return [
            e
            for e in uow.audit.list_recent(world.course.tenant_id, limit=50)
            if e.action.value == "similarity.viewed"
        ]


def test_an_instructor_sees_the_pairs_with_names(world: World, places) -> None:
    root, _web = places
    world.register("teacher", Role.INSTRUCTOR)
    a = world.register("s2400001", Role.LEARNER)
    b = world.register("s2400002", Role.LEARNER)
    task_id = _import_example(world)
    run = _report(world, root, world.course.id, task_id, [a, b])

    page = world.client("teacher").get(f"/courses/{world.course.id}/similarity")

    assert page.status_code == 200
    assert "97%" in page.text
    assert "s2400001" in page.text and "s2400002" in page.text  # 仮の名前の対応
    assert "不正の判定ではありません" in page.text
    assert "盗用" not in page.text
    assert f"/similarity/{task_id}/{run.run_id}/" in page.text
    assert [e.target_id for e in _viewed(world)] == [str(world.course.id)]


def test_the_page_switches_between_code_and_text(world: World, places) -> None:
    """**比べ方を切り替える**（#569）。同じ組のもう一方の類似度も並べる。"""
    root, _web = places
    world.register("teacher", Role.INSTRUCTOR)
    a = world.register("s2400001", Role.LEARNER)
    b = world.register("s2400002", Role.LEARNER)
    task_id = _import_example(world)
    code = _report(world, root, world.course.id, task_id, [a, b])
    text = _report(
        world,
        root,
        world.course.id,
        task_id,
        [a, b],
        mode=SimilarityMode.TEXT,
        pairs=PAIRS.replace("S-001.c", "S-001.txt")
        .replace("S-002.c", "S-002.txt")
        .replace("0.97", "0.41"),
    )
    client = world.client("teacher")

    as_code = client.get(f"/courses/{world.course.id}/similarity").text
    assert as_code.index("97%") < as_code.index("41%"), "選んだ方が先の列"
    assert f"/similarity/{task_id}/{code.run_id}/" in as_code
    assert "Dolos で開く（コードとして）" in as_code and "最長の一致（トークン）" in as_code

    as_text = client.get(f"/courses/{world.course.id}/similarity?mode=text").text
    assert as_text.index("41%") < as_text.index("97%")
    assert f"/similarity/{task_id}/{text.run_id}/" in as_text
    assert "Dolos で開く（テキストとして）" in as_text and "最長の一致（文字）" in as_text
    assert (
        client.get(f"/courses/{world.course.id}/similarity/{task_id}/{text.run_id}/").status_code
        == 200
    )

    # 知らない値は「コードとして」で開く（落とさない）。
    assert (
        "Dolos で開く（コードとして）"
        in client.get(f"/courses/{world.course.id}/similarity?mode=x").text
    )


def test_a_text_only_task_still_shows_under_code(world: World, places) -> None:
    """Markdown の課題はテキストだけ。切り替えてもカードを消さず「テキストだけ」と出す。"""
    root, _web = places
    world.register("teacher", Role.INSTRUCTOR)
    a = world.register("s2400001", Role.LEARNER)
    b = world.register("s2400002", Role.LEARNER)
    task_id = _import_example(world)
    _report(world, root, world.course.id, task_id, [a, b], mode=SimilarityMode.TEXT)

    page = world.client("teacher").get(f"/courses/{world.course.id}/similarity").text
    assert "テキストだけ" in page and "97%" in page


def test_an_assistant_cannot_open_it(world: World, places) -> None:
    root, _web = places
    world.register("ta", Role.ASSISTANT)
    a = world.register("s2400001", Role.LEARNER)
    b = world.register("s2400002", Role.LEARNER)
    task_id = _import_example(world)
    run = _report(world, root, world.course.id, task_id, [a, b])
    client = world.client("ta")

    for path in (
        f"/courses/{world.course.id}/similarity",
        f"/courses/{world.course.id}/similarity/{task_id}/{run.run_id}/",
        f"/courses/{world.course.id}/similarity/{task_id}/{run.run_id}/data/files.csv",
    ):
        assert client.get(path).status_code in (403, 404), path
    assert _viewed(world) == []


def test_the_viewer_is_served_under_a_strict_csp(world: World, places) -> None:
    root, _web = places
    world.register("teacher", Role.INSTRUCTOR)
    a = world.register("s2400001", Role.LEARNER)
    b = world.register("s2400002", Role.LEARNER)
    task_id = _import_example(world)
    run = _report(world, root, world.course.id, task_id, [a, b])
    client = world.client("teacher")
    base = f"/courses/{world.course.id}/similarity/{task_id}/{run.run_id}"

    # Dolos の画面は URL の末尾から data を引くので、`/` で終わらせる。
    redirect = client.get(base, follow_redirects=False)
    assert redirect.status_code == 307 and redirect.headers["location"].endswith(base + "/")

    page = client.get(base + "/")
    assert page.status_code == 200 and "dolos" in page.text
    assert page.headers["content-security-policy"] == VIEWER_CSP
    assert "connect-src 'self'" in VIEWER_CSP and "fonts.googleapis" not in VIEWER_CSP
    assert page.headers["referrer-policy"] == "no-referrer"
    assert [e.target_id for e in _viewed(world)] == [task_id]

    data = client.get(base + "/data/pairs.csv")
    assert data.status_code == 200 and "S-001" in data.text
    assert data.headers["content-security-policy"] == VIEWER_CSP
    assert client.get(base + "/assets/app.js").headers["content-type"].startswith("text/javascript")
    # 部品と CSV では監査を増やさない（1 回の閲覧で数十行になる）。
    assert len(_viewed(world)) == 1


def test_nothing_outside_the_report_and_the_viewer(world: World, places) -> None:
    root, _web = places
    world.register("teacher", Role.INSTRUCTOR)
    a = world.register("s2400001", Role.LEARNER)
    b = world.register("s2400002", Role.LEARNER)
    task_id = _import_example(world)
    run = _report(world, root, world.course.id, task_id, [a, b])
    (run_dir(root, run) / "run.json").read_text(encoding="utf-8")
    client = world.client("teacher")
    base = f"/courses/{world.course.id}/similarity/{task_id}/{run.run_id}"

    assert client.get(base + "/data/run.json").status_code == 404
    assert client.get(base + "/data/../run.json").status_code == 404
    assert client.get(base + "/assets/escape.txt").status_code == 404  # 外を指すリンク
    assert client.get(f"/courses/{world.course.id}/similarity/{task_id}/nope/").status_code == 404


def test_another_courses_report_cannot_be_opened(world: World, places) -> None:
    """担当しているコースの経路に、他のコースの報告を差し込んでも開けない。"""
    root, _web = places
    world.register("teacher", Role.INSTRUCTOR)
    a = world.register("s2400001", Role.LEARNER)
    b = world.register("s2400002", Role.LEARNER)
    task_id = _import_example(world)
    other = CourseId("crs_" + "7" * 32)
    run = _report(world, root, other, task_id, [a, b])
    # 自分のコースの下に、他のコースの run.json を置いた形。
    mine = root / str(world.course.id) / task_id / run.run_id
    mine.parent.mkdir(parents=True)
    run_dir(root, run).rename(mine)

    response = world.client("teacher").get(
        f"/courses/{world.course.id}/similarity/{task_id}/{run.run_id}/"
    )
    assert response.status_code == 404


def test_without_the_viewer_the_entry_still_opens(world: World, places, monkeypatch) -> None:
    root, _web = places
    monkeypatch.delenv("AIJUDGE_DOLOS_WEB_DIR")
    world.register("teacher", Role.INSTRUCTOR)
    a = world.register("s2400001", Role.LEARNER)
    b = world.register("s2400002", Role.LEARNER)
    task_id = _import_example(world)
    run = _report(world, root, world.course.id, task_id, [a, b])
    client = world.client("teacher")

    page = client.get(f"/courses/{world.course.id}/similarity")
    assert page.status_code == 200 and "97%" in page.text
    assert "Dolos で開く" not in page.text
    viewer = client.get(f"/courses/{world.course.id}/similarity/{task_id}/{run.run_id}/")
    assert viewer.status_code == 503
