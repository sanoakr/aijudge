"""デモコースの提出は数えない（#194）。

**述語は 1 つのままにする。** 「数えるかどうか」を訊く道が 2 つあると、
片方だけ直る日が来る ── 実際に来た（#197 で、観測レコードと習熟度が
`is_trial` を見ていなかった）。だからここで足すのは理由であって、判定では
ない。
"""

from __future__ import annotations

from datetime import UTC, datetime

from aijudge_core import Role, Submission, SubmissionState
from aijudge_core.ids import SubmissionId, TaskVersionId, UserId

NOW = datetime(2026, 9, 11, tzinfo=UTC)


def _submission(**overrides) -> Submission:
    base = {
        "id": SubmissionId("sub_" + "1" * 32),
        "task_version_id": TaskVersionId("tsv_" + "2" * 32),
        "learner_id": UserId("usr_" + "3" * 32),
        "state": SubmissionState.DRAFT,
        "created_at": NOW,
    }
    return Submission(**{**base, **overrides})


def test_a_learner_in_a_real_course_counts() -> None:
    assert not _submission().is_trial


def test_a_learner_in_the_demo_course_does_not() -> None:
    """**デモでは学習者の提出も数えない。**

    誰でも入れる場所なので、数えると観測レコードの大半がデモの提出になる。
    """
    assert _submission(is_demo=True).is_trial


def test_an_instructors_own_check_still_does_not() -> None:
    assert _submission(submitted_as=Role.INSTRUCTOR).is_trial


def test_the_flag_is_recorded_not_recomputed() -> None:
    """**そのときの事実として焼き付ける。**

    測定時にコースの設定を引き直すと、デモコースの指名を変えた瞬間に
    過去の提出の意味が変わる ── 指名は環境変数なので、配置のたびに
    変わりうる。`submitted_as` を焼き付けているのと同じ理由である
    （ADR 0013 で遅延減点が踏んだ罠と同じ形）。
    """
    stored = _submission(is_demo=True)
    # 記録そのものに載っていること（あとから引き直す余地が無いこと）。
    assert stored.model_dump()["is_demo"] is True
    assert Submission.model_validate(stored.model_dump()).is_trial
