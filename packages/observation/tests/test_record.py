"""観測レコードの選別規則を固定する。

数えてはいけないものを数えない、が要点。ここが緩むと κ が実力より高く出る
（ADR 0005）。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aijudge_observation import Observation


def make(**overrides: object) -> Observation:
    base: dict[str, object] = {
        "subject_profile": "cs_intro_c",
        "task_name": "example-task",
        "submission": "s001.c",
        "criterion_code": "readability",
        "levels": (0, 1, 2, 3),
        "machine_level": 3,
        "human_level": 1,
        "blind": True,
        "observed_at": datetime(2026, 8, 28, tzinfo=UTC),
    }
    base.update(overrides)
    return Observation.model_validate(base)


def test_a_blind_mark_against_a_machine_level_is_a_sample() -> None:
    assert make().usable_for_agreement


def test_a_mark_made_after_seeing_the_ai_is_not_a_sample() -> None:
    """AI に引きずられた採点は正解データではない。"""
    assert not make(blind=False).usable_for_agreement


def test_a_missing_instructor_mark_is_not_a_sample() -> None:
    """運用の大多数がこれ。抽出されなかった提出。"""
    assert not make(human_level=None, blind=False).usable_for_agreement


def test_a_missing_machine_level_is_not_a_sample() -> None:
    """採点がまだ届いていない提出。AI 評価は非同期なので普通に起きる。"""
    assert not make(machine_level=None).usable_for_agreement


def test_a_conclusive_criterion_is_not_a_sample() -> None:
    """テスト実行で確定した観点は AI の精度を表さない。"""
    assert not make(conclusive=True).usable_for_agreement


def test_an_unscored_criterion_is_neither_agreement_nor_disagreement() -> None:
    assert not make(unscored=True).usable_for_agreement


def test_a_single_level_criterion_is_refused() -> None:
    """段階が 1 つでは一致度を測れない。構成の誤りとして落とす。"""
    with pytest.raises(ValidationError):
        make(levels=(0,))


def test_the_record_is_immutable() -> None:
    """観測は書き直すもの（投影）だが、1 つのレコードは書き換えない。"""
    with pytest.raises(ValidationError):
        make().machine_level = 0  # type: ignore[misc]


def test_the_submission_key_groups_by_task_and_submission() -> None:
    """提出単位の指標（レビュー行き率・見逃し率）はこのキーで畳む。"""
    assert make().submission_key == "example-task/s001.c"
