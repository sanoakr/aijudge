"""動画提出（別ルート）の規則を固定する。

固定したいこと:

別ルート   `POST /submit-video` は生バイト列を受け、通常の `/submit` とは分ける。
上限       `max_video_bytes` を超えたら 413。
形式       受付にない拡張子は 400。動画以外を送ったら「/submit で」と返す。
配信       ダウンロードは Range 対応（`<video>` のシークに 206）。
無効化     `video_store` 未設定なら 501。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from aijudge_core import (
    HUMAN_SCORED,
    Course,
    Provenance,
    ReviewState,
    Role,
    RubricCriterion,
    RubricLevel,
    Task,
    TaskVersion,
    new_id,
)
from aijudge_core.ids import CourseId, CriterionId, TaskId, TaskVersionId, TenantId, UserId
from aijudge_identity import AuthService
from aijudge_persistence import Database
from aijudge_studentweb import SESSION_COOKIE, StudentApp, create_app
from aijudge_submission import FilesystemArtifactStore

REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
COURSE = CourseId("crs_" + "1" * 32)
PASSWORD = "correct horse battery staple"


class World:
    def __init__(self, tmp_path: Path, *, with_video: bool = True) -> None:
        self.database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
        self.store = FilesystemArtifactStore(tmp_path / "artifacts")
        self.video_dir = tmp_path / "video"
        video_store = FilesystemArtifactStore(self.video_dir) if with_video else None
        self.app = StudentApp(
            self.database,
            self.store,
            profiles_dir=PROFILES,
            video_store=video_store,
            max_video_bytes=2000,
        )
        self.client = TestClient(create_app(self.app))

        with self.database.unit_of_work() as uow:
            uow.identity.save_course(
                Course(
                    id=COURSE,
                    tenant_id=TENANT,
                    code="media1",
                    title="メディア演習",
                    term="2026-前期",
                    subject_profile="cs_intro_c",
                    upload_suffixes=(".mp4",),
                )
            )
            uow.commit()

        self.task_version = TaskVersion(
            id=TaskVersionId(new_id("tsv")),
            task_id=TaskId(new_id("tsk")),
            version=1,
            subject_profile="cs_intro_c",
            statement="デモ動画を提出してください。",
            criteria=(
                RubricCriterion(
                    id=CriterionId(new_id("crt")),
                    code="demo",
                    title="デモの内容",
                    description="教員が視聴して採点する",
                    weight=1.0,
                    levels=(
                        RubricLevel(level=0, label="不可", descriptor="未達", score_ratio=0.0),
                        RubricLevel(level=3, label="達成", descriptor="達成", score_ratio=1.0),
                    ),
                    evaluator_id=HUMAN_SCORED,
                ),
            ),
            max_score=100.0,
            provenance=Provenance(
                authored_by=UserId("usr_" + "a" * 32),
                review_state=ReviewState.APPROVED,
            ),
            created_at=datetime(2026, 9, 4, tzinfo=UTC),
        )
        with self.database.unit_of_work() as uow:
            uow.tasks.save_task(Task(id=self.task_version.task_id, course_id=COURSE, title="デモ"))
            uow.tasks.save_version(self.task_version)
            uow.commit()

    def register_and_login(self, login: str = "s2400001") -> None:
        with self.database.unit_of_work() as uow:
            service = AuthService(uow.identity)
            principal = service.register(
                tenant_id=TENANT, login=login, display_name=login, password=PASSWORD
            )
            service.enroll(
                tenant_id=TENANT,
                course_id=COURSE,
                user_id=principal.user_id,
                role=Role.LEARNER,
            )
            uow.commit()
        res = self.client.post(
            "/login", data={"login": login, "password": PASSWORD}, follow_redirects=False
        )
        assert res.status_code == 303, res.text
        self.client.cookies.set(SESSION_COOKIE, res.cookies[SESSION_COOKIE])

    def post_video(self, body: bytes, *, filename: str = "demo.mp4", key: str | None = None):
        headers = {"Content-Type": "application/octet-stream"}
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.client.post(
            f"/tasks/{self.task_version.id}/submit-video?filename={filename}",
            content=body,
            headers=headers,
            follow_redirects=False,
        )

    def close(self) -> None:
        self.database.dispose()


@pytest.fixture
def world(tmp_path: Path):
    w = World(tmp_path)
    yield w
    w.close()


def test_video_submission_creates_a_submission_with_a_video_artifact(world: World) -> None:
    world.register_and_login()
    res = world.post_video(b"PRETEND-MP4-BYTES")
    assert res.status_code == 303, res.text
    sub_id = res.headers["location"].split("/submissions/")[1].split("?")[0]

    with world.database.unit_of_work() as uow:
        sub = uow.submissions.get(sub_id)
    assert sub is not None
    (artifact,) = sub.artifacts
    assert artifact.kind.value == "video"
    assert artifact.filename == "demo.mp4"
    # 実体は動画ストア（別ディスク相当）にあり、通常の artifacts には無い。
    assert [p.name for p in world.video_dir.rglob("demo.mp4")] == ["demo.mp4"]
    assert list((world.store.root).rglob("demo.mp4")) == []


def test_download_supports_range_requests(world: World) -> None:
    world.register_and_login()
    body = bytes(range(200))
    loc = world.post_video(body).headers["location"]
    sub_id = loc.split("/submissions/")[1].split("?")[0]
    with world.database.unit_of_work() as uow:
        artifact_id = str(uow.submissions.get(sub_id).artifacts[0].id)

    url = f"/submissions/{sub_id}/artifacts/{artifact_id}"
    full = world.client.get(url)
    assert full.status_code == 200
    assert full.headers["accept-ranges"] == "bytes"
    assert full.content == body

    part = world.client.get(url, headers={"Range": "bytes=10-19"})
    assert part.status_code == 206
    assert part.headers["content-range"] == "bytes 10-19/200"
    assert part.content == body[10:20]
    assert part.headers.get("content-type", "").startswith("video/mp4")


def test_oversize_video_is_rejected_with_413(world: World) -> None:
    world.register_and_login()
    res = world.post_video(b"x" * 5000)  # max_video_bytes=2000
    assert res.status_code == 413
    # 中途半端なファイルを残さない。
    assert list(world.video_dir.rglob("*.mp4")) == []
    assert list(world.video_dir.rglob("*.partial")) == []


def test_wrong_suffix_is_rejected(world: World) -> None:
    world.register_and_login()
    res = world.post_video(b"data", filename="notes.txt")
    assert res.status_code == 400


def test_non_streamed_suffix_is_sent_to_the_other_route(world: World) -> None:
    # `.c` は受付にあっても submit-video では受けない（通常ルートへ）。
    world.register_and_login()
    with world.database.unit_of_work() as uow:
        task = Task(
            id=TaskId(new_id("tsk")),
            course_id=COURSE,
            title="混在",
            accepted_suffixes=(".mp4", ".c"),
        )
        tv = world.task_version.model_copy(
            update={"id": TaskVersionId(new_id("tsv")), "task_id": task.id}
        )
        uow.tasks.save_task(task)
        uow.tasks.save_version(tv)
        uow.commit()
    res = world.client.post(
        f"/tasks/{tv.id}/submit-video?filename=main.c",
        content=b"int main(void){}",
        follow_redirects=False,
    )
    assert res.status_code == 400
    assert "/submit" in res.json()["detail"]


def test_idempotency_key_returns_the_same_submission(world: World) -> None:
    world.register_and_login()
    first = world.post_video(b"PRETEND-MP4", key="up-1")
    again = world.post_video(b"PRETEND-MP4", key="up-1")
    assert first.status_code == 303
    assert again.status_code == 303
    assert again.headers["location"].split("?")[0] == first.headers["location"].split("?")[0]


def test_video_disabled_when_no_store_configured(tmp_path: Path) -> None:
    w = World(tmp_path, with_video=False)
    try:
        w.register_and_login()
        res = w.post_video(b"data")
        assert res.status_code == 501
    finally:
        w.close()


def test_too_many_concurrent_uploads_get_429_with_retry_after(world: World) -> None:
    world.register_and_login()
    # 全スロットが埋まっている状況を作る（実際の並行は TestClient では作れない）。
    world.app.active_video_uploads = world.app.max_concurrent_video
    res = world.post_video(b"PRETEND-MP4")
    assert res.status_code == 429
    assert int(res.headers["retry-after"]) > 0
    # 空けば通る。
    world.app.active_video_uploads = 0
    assert world.post_video(b"PRETEND-MP4").status_code == 303


def test_a_finished_upload_frees_its_slot(world: World) -> None:
    world.register_and_login()
    assert world.app.active_video_uploads == 0
    assert world.post_video(b"PRETEND-MP4").status_code == 303
    assert world.app.active_video_uploads == 0
