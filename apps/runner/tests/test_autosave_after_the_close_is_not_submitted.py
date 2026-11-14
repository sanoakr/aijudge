"""受付終了後に届いた自動保存は自動提出しないこと（#705）。

一次の守りは受け口（`ide_save` が関門で断る）で、自動提出の側の判定は二重目の守り
である。web の時計と境目がずれて 1 件すべり込んだ保存を出すと、受付終了後に書いた
内容が `auto_closed`（遅延減点なし）の提出になる。`test_ide_autosubmit` の
`test_some_autosaves_are_not_submitted` はこの理由を試していなかった。

境目そのもの（受付終了の瞬間の保存）は出す側に入る
（`test_when_the_deadline_is_the_close_the_auto_submission_is_not_late`）。
ここでは 1 秒あとを出さないことを固定する。
"""

from __future__ import annotations

from datetime import timedelta

from test_ide_autosubmit import CLOSES, World
from test_ide_autosubmit import world as world  # フィクスチャを借りる


def test_an_autosave_after_the_close_is_skipped(world: World) -> None:
    world.autosave(at=CLOSES + timedelta(seconds=1))

    report = world.close(now=CLOSES + timedelta(minutes=1))

    assert report.submitted == 0
    assert report.skipped == {"受付終了後の保存": 1}
    assert world.submissions() == ()


def test_an_autosave_at_the_close_is_still_submitted(world: World) -> None:
    """境目の瞬間の保存は出す（取りこぼさない側を、上の判定で狭めない）。"""
    world.autosave(at=CLOSES)

    report = world.close(now=CLOSES + timedelta(minutes=1))

    assert report.submitted == 1
    assert report.skipped == {}
