"""保存期間を過ぎた動画の消去（ADR 0020）。

固定したいこと:

起点   締切から 6 ヶ月。**提出日でも成績確定でもない。**
締切なし 締切の無い課題だけは提出から 1 年（共通の起点が他に無い）。
まとまり 同じ問題セットの動画は同じ日に期限を迎える（締切が揃っているため）。
安全   期限の来ていない回には触らない。起点が無いもの（未提出）は消さない。
順序   ファイルを消してから印を付ける ── 途中で落ちても次の実行が続けられる。
記録   消した件数と容量が監査ログに残る（ADR 0016）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from aijudge_admin.course_definition import apply_course_definition
from aijudge_admin.video_purge import plan_video_purge, purge_videos
from aijudge_audit import AuditAction
from aijudge_core import Artifact, ArtifactKind, ArtifactRole, Submission, SubmissionState
from aijudge_core.ids import ArtifactId, SubmissionId, TenantId, UserId
from aijudge_persistence import Database
from aijudge_submission import FilesystemArtifactStore

# 締切は日本時間で書かれている（`units:` の `+09:00`）。**期限はその時刻の
# 6 ヶ月後**なので、比べるときも同じ帯で書く ── UTC に直して比べると、
# 実装が時差を落としても気づけない。
JST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).resolve().parents[3]
PROFILES = REPO_ROOT / "subjects"
TENANT = TenantId("ten_" + "0" * 32)
AUTHOR = UserId("usr_" + "a" * 32)
LEARNER = UserId("usr_" + "b" * 32)

# 締切は 2026-10-01（ex01）と 2027-01-14（ex02）。期限はそれぞれ
# 2027-04-01 と 2027-07-14 になる。ex99 は締切を持たない。
DEFINITION = """
course:
  code: video-demo
  title: 動画の課題
  term: 2026-後期
  subject_profile: report_ja
  description: 動画提出の保存期間を確かめるためのコース。
units:
  ex01:
    opens_at: 2026-09-18T13:00:00+09:00
    due_at: 2026-10-01T23:59:00+09:00
  ex02:
    opens_at: 2026-12-01T13:00:00+09:00
    due_at: 2027-01-14T23:59:00+09:00
tasks:
  - key: ex01/p1
    unit: ex01
    session: 1
    position: 1
    title: 発表を録画して出す
    accepted_suffixes: [.mp4]
    statement: |
      ## [必須] 発表を録画して出す ##

      発表の様子を録画して提出してください。
    criteria:
      - code: delivery
        title: 話し方
        description: 聞き取れるか。
        weight: 1.0
        evaluator: __human__
        levels:
          - {level: 0, label: 未達, descriptor: 聞き取れない, score_ratio: 0.0}
          - {level: 1, label: 十分, descriptor: 聞き取れる, score_ratio: 1.0}
  - key: ex02/p1
    unit: ex02
    session: 2
    position: 1
    title: 実演を録画して出す
    accepted_suffixes: [.mp4]
    statement: |
      ## [必須] 実演を録画して出す ##

      実演の様子を録画して提出してください。
    criteria:
      - code: delivery
        title: 話し方
        description: 聞き取れるか。
        weight: 1.0
        evaluator: __human__
        levels:
          - {level: 0, label: 未達, descriptor: 聞き取れない, score_ratio: 0.0}
          - {level: 1, label: 十分, descriptor: 聞き取れる, score_ratio: 1.0}
  - key: ex99/p1
    unit: ex99
    session: 99
    position: 1
    title: 締切の無い課題
    accepted_suffixes: [.mp4]
    statement: |
      ## [任意] 自習用 ##

      いつ出しても構いません。
    criteria:
      - code: delivery
        title: 話し方
        description: 聞き取れるか。
        weight: 1.0
        evaluator: __human__
        levels:
          - {level: 0, label: 未達, descriptor: 聞き取れない, score_ratio: 0.0}
          - {level: 1, label: 十分, descriptor: 聞き取れる, score_ratio: 1.0}
"""


@pytest.fixture
def database(tmp_path: Path):
    db = Database.connect(f"sqlite+pysqlite:///{tmp_path}/purge.db", create=True)
    yield db
    db.dispose()


@pytest.fixture
def video_store(tmp_path: Path) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(tmp_path / "video")


@pytest.fixture
def course(database: Database, tmp_path: Path):
    definition = tmp_path / "course.yaml"
    definition.write_text(DEFINITION, encoding="utf-8")
    return apply_course_definition(
        database, path=definition, tenant_id=TENANT, profiles_dir=PROFILES, authored_by=AUTHOR
    ).course


def a_video(
    database: Database,
    video_store: FilesystemArtifactStore,
    course,
    *,
    unit: str,
    marker: str,
) -> tuple[SubmissionId, str]:
    """この問題セットの課題に動画を 1 件出しておく。"""
    submission_id = SubmissionId("sub_" + marker * 32)
    key = f"video/{marker}.mp4"
    video_store.put(key, b"MP4" + marker.encode())
    with database.unit_of_work() as uow:
        task = next(t for t in uow.tasks.list_for_course(course.id) if t.unit == unit)
        version = uow.tasks.latest_version(task.id)
        uow.submissions.save(
            Submission(
                id=submission_id,
                task_version_id=version.id,
                learner_id=LEARNER,
                state=SubmissionState.SUBMITTED,
                submitted_at=datetime(2026, 9, 30, tzinfo=UTC),
                artifacts=(
                    Artifact(
                        id=ArtifactId("art_" + marker * 32),
                        submission_id=submission_id,
                        role=ArtifactRole.ORIGINAL,
                        kind=ArtifactKind.VIDEO,
                        filename=f"{marker}.mp4",
                        storage_key=key,
                        content_hash="0" * 64,
                        byte_size=7,
                        created_at=datetime(2026, 9, 30, tzinfo=UTC),
                    ),
                ),
                created_at=datetime(2026, 9, 30, tzinfo=UTC),
            )
        )
        uow.commit()
    return submission_id, key


def test_nothing_is_due_before_six_months_have_passed(database, video_store, course) -> None:
    a_video(database, video_store, course, unit="ex01", marker="1")

    # 締切（2026-10-01）の 5 ヶ月後。まだ疑義の最中でもある。
    plan = plan_video_purge(database, tenant_id=TENANT, now=datetime(2027, 3, 1, tzinfo=UTC))

    assert plan.candidates == ()
    # **いつ来るかを言う。** 「0 件」だけだと、対象が無いのか設定が違うのか
    # 分からない。
    assert plan.next_expires_at == datetime(2027, 4, 1, 23, 59, tzinfo=JST)


def test_only_the_units_past_their_window_are_picked_up(database, video_store, course) -> None:
    """**回ごとにまとまって消える。** 同じ問題セットの締切は揃っている。"""
    a_video(database, video_store, course, unit="ex01", marker="1")
    a_video(database, video_store, course, unit="ex02", marker="2")

    plan = plan_video_purge(database, tenant_id=TENANT, now=datetime(2027, 4, 2, tzinfo=UTC))

    assert [candidate.unit for candidate in plan.candidates] == ["ex01"]
    # ex02 の期限はまだ先（2027-07-14）。
    assert plan.next_expires_at == datetime(2027, 7, 14, 23, 59, tzinfo=JST)


def test_a_task_without_a_deadline_counts_from_the_submission(
    database, video_store, course
) -> None:
    """提出（2026-09-30）から 1 年。**6 ヶ月では消えない。**"""
    a_video(database, video_store, course, unit="ex99", marker="9")

    half_a_year = plan_video_purge(database, tenant_id=TENANT, now=datetime(2027, 4, 2, tzinfo=UTC))
    a_year = plan_video_purge(database, tenant_id=TENANT, now=datetime(2027, 10, 1, tzinfo=UTC))

    assert half_a_year.candidates == ()
    assert [candidate.unit for candidate in a_year.candidates] == ["ex99"]
    # **締切のあるものと区別して数える** ── 下見を読む人に、なぜこれが
    # 挙がっているのかが分かる必要がある。
    assert a_year.without_deadline == 1
    assert a_year.candidates[0].due_at is None


def test_purging_removes_the_file_and_leaves_the_record(database, video_store, course) -> None:
    submission_id, key = a_video(database, video_store, course, unit="ex01", marker="1")
    now = datetime(2027, 4, 2, tzinfo=UTC)
    plan = plan_video_purge(database, tenant_id=TENANT, now=now)

    outcome = purge_videos(database, plan, video_store=video_store, tenant_id=TENANT)

    assert outcome.deleted == 1
    assert outcome.freed_bytes == 7
    assert outcome.failed == ()
    assert video_store.exists(key) is False
    with database.unit_of_work() as uow:
        loaded = uow.submissions.get(submission_id)
    assert loaded is not None
    (artifact,) = loaded.artifacts
    # **行は残る。** 消えたのはファイルだけ（P8）。
    assert artifact.is_purged is True
    assert artifact.purged_at == now
    assert artifact.storage_key == key


def test_the_second_run_finds_nothing_left(database, video_store, course) -> None:
    """冪等。**「また N 件消した」と報告しない。**"""
    a_video(database, video_store, course, unit="ex01", marker="1")
    now = datetime(2027, 4, 2, tzinfo=UTC)
    purge_videos(
        database,
        plan_video_purge(database, tenant_id=TENANT, now=now),
        video_store=video_store,
        tenant_id=TENANT,
    )

    again = plan_video_purge(database, tenant_id=TENANT, now=now)

    assert again.candidates == ()


def test_a_file_deleted_without_its_mark_is_picked_up_again(database, video_store, course) -> None:
    """**2 段の順序が効いていること。**

    ファイルだけ先に消えた状態（印を付ける前に落ちた）は、次の実行が
    もう一度拾って印を付ける ── ストアの `delete` は無いキーを成功として
    扱うので、同じ手順のまま続けられる。
    """
    submission_id, key = a_video(database, video_store, course, unit="ex01", marker="1")
    video_store.delete(key)  # 印を付ける前に落ちた状態を作る
    now = datetime(2027, 4, 2, tzinfo=UTC)

    outcome = purge_videos(
        database,
        plan_video_purge(database, tenant_id=TENANT, now=now),
        video_store=video_store,
        tenant_id=TENANT,
    )

    assert outcome.deleted == 1
    with database.unit_of_work() as uow:
        loaded = uow.submissions.get(submission_id)
    assert loaded is not None
    assert loaded.artifacts[0].is_purged is True


def test_the_purge_is_written_to_the_audit_log(database, video_store, course) -> None:
    a_video(database, video_store, course, unit="ex01", marker="1")
    now = datetime(2027, 4, 2, tzinfo=UTC)

    purge_videos(
        database,
        plan_video_purge(database, tenant_id=TENANT, now=now),
        video_store=video_store,
        tenant_id=TENANT,
    )

    with database.unit_of_work() as uow:
        events = uow.audit.list_recent(TENANT, action=AuditAction.VIDEO_PURGED)
    assert len(events) == 1
    # **学習者データは入れない**（P7）。入るのは件数と容量だけ。
    assert events[0].detail["deleted"] == 1
    assert events[0].detail["freed_bytes"] == 7


def test_another_tenants_course_is_not_touched(database, video_store, course) -> None:
    a_video(database, video_store, course, unit="ex01", marker="1")

    plan = plan_video_purge(
        database,
        tenant_id=TenantId("ten_" + "9" * 32),
        now=datetime(2027, 4, 2, tzinfo=UTC),
    )

    assert plan.candidates == ()
