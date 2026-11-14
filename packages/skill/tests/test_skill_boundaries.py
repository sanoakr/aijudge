"""S7 の境目と向きを固定する（Issue #685）。

`test_skill.py` の観測は 0.0・0.1・1.0 の点と 0.1・0.3・1.0 の確信度だけで、
境目そのものを踏まない。閾値や比較が 1 つずれても全学習者の習熟度が黙って
変わるので、ちょうど境目の値で確かめる。

部分点の閾値     0.6（及第）は正答、0.59 は誤答として畳む。
確信度の下限     0.5 は数え、0.49 は数えない。
根拠の上限の向き 上限で切るときは**新しい提出を残す**（service.py 冒頭の仕様）。
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_skill import KC, LEARNER, NOW, TENANT, _event, _service

from aijudge_skill import (
    DEFAULT_CORRECT_THRESHOLD,
    MIN_CONFIDENCE,
    InMemorySkillRepository,
    SkillService,
    is_correct,
    posterior,
)
from aijudge_skill.bkt import PRACTICE_PARAMETERS


def test_the_threshold_is_the_pass_mark() -> None:
    """「及第ならできたと見なす」という説明に結び付いた値（bkt.py 冒頭）。"""
    assert DEFAULT_CORRECT_THRESHOLD == 0.6
    assert is_correct(0.6) is True
    assert is_correct(0.59) is False


@pytest.mark.parametrize(("ratio", "correct"), [(0.6, True), (0.59, False)])
def test_a_partial_score_folds_at_the_pass_mark(ratio: float, correct: bool) -> None:
    """サービスを通しても、ちょうど及第は正答・その手前は誤答として畳む。"""
    service, repository = _service()
    service.apply(_event(ratio=ratio))

    state = repository.get_state(TENANT, LEARNER, KC)
    assert state is not None
    expected = posterior(PRACTICE_PARAMETERS.prior, correct, PRACTICE_PARAMETERS)
    assert state.mastery == pytest.approx(expected)


def test_a_judgement_at_the_confidence_floor_counts() -> None:
    assert MIN_CONFIDENCE == 0.5
    service, repository = _service()
    assert len(service.apply(_event(confidence=0.5))) == 1
    state = repository.get_state(TENANT, LEARNER, KC)
    assert state is not None
    assert state.observation_count == 1


def test_a_judgement_just_below_the_confidence_floor_does_not_count() -> None:
    service, repository = _service()
    assert service.apply(_event(confidence=0.49)) == ()
    assert repository.get_state(TENANT, LEARNER, KC) is None


def test_the_evidence_cap_keeps_the_newest_submissions() -> None:
    """**上限で切るときは新しいものを残す。**

    最古を残す向きにすると、ポートフォリオの根拠が学期初めの提出で固定され、
    新しい提出が出なくなる。課題 ID の文字列順が提出の順と逆になるように
    並べ、提出の時刻で並べていることも同時に確かめる。
    """
    repository = InMemorySkillRepository()
    service = SkillService(repository, max_evidence=3)
    runs = "fedcba"  # 先に出したものほど課題 ID が大きい
    for index, run in enumerate(runs):
        service.apply(_event(run=run, submitted_at=NOW + timedelta(days=index)))

    state = repository.get_state(TENANT, LEARNER, KC)
    assert state is not None
    kept = [str(item.grading_run_id) for item in state.evidence]
    assert kept == ["grn_" + run * 32 for run in runs[-3:]]
    assert state.observation_count == 6
