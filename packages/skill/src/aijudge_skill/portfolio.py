"""習熟度を人に見せる形にする（#335）。

**推定はしない。** BKT はこのパッケージの `service.py` が持ち、ここがするのは
「保存されている状態を、画面が読める形に畳む」ことだけである。

**学生の画面と教員の画面が同じものを読む。** 教員が受講者 1 人を見る画面と、
学習者が自分を見る画面は同じ問いに答える ── どの知識要素がどれだけ身に
付いているか、その根拠は何か。別々に書くと、片方を直した日にもう片方だけが
古くなる。だからここに 1 つ置き、両方の合成ルートから読む。

**コースを知らない。** 根拠がどのコースから来たかは呼び出し側が解決して
渡す（`course_of` / `title_of`）── 習熟度はテナント単位で積み上がる値で、
コースの概念を S7 に持ち込まない（P6）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aijudge_core import SkillState

__all__ = ["Evidence", "LearnerKc", "confidence_of", "split_evidence"]


@dataclass(frozen=True)
class Evidence:
    """1 件の根拠。**どのコースの提出かを解決してある**（#328）。"""

    grading_run_id: str
    course_id: str | None
    task_title: str | None
    score_ratio: float
    human_verified: bool
    observed_at: datetime

    @property
    def resolved(self) -> bool:
        """由来を辿れたか。**辿れないものを「他コース」と書かない。**"""
        return self.course_id is not None


def confidence_of(state: SkillState) -> float | None:
    """この KC の確信度（#335）。根拠が持つ値の平均。

    **値を持つ根拠だけで平均する。** この欄より前に積まれた根拠は
    `confidence` を持たない ── 0 として数えると、古い記録ほど自信が無かった
    ことになる。1 つも持っていなければ `None`（「記録が無い」）で、画面は
    数字を出さずにそう言う。
    """
    values = [item.confidence for item in state.evidence if item.confidence is not None]
    return sum(values) / len(values) if values else None


@dataclass(frozen=True)
class LearnerKc:
    key: str
    label: str
    mastery: float
    #: 根拠が持つ確信度の平均（#335）。**None は 0 ではない。**
    confidence: float | None
    observation_count: int
    updated_at: datetime
    #: このコースの提出から来た根拠。
    here: tuple[Evidence, ...]
    #: 他のコース（または辿れないもの）から来た根拠の件数。
    #: **件数だけ出す** ── 担当していないコースの課題名を見せない。
    elsewhere: int


def split_evidence(
    state: SkillState,
    *,
    course_of: dict[str, str | None],
    title_of: dict[str, str | None],
    course_id: str,
    key: str,
    label: str,
) -> LearnerKc:
    """根拠を「このコース」と「それ以外」に割る。

    **習熟度はコースを跨いで動く。** 1 つの値に、担当していない科目の観測も
    入っている ── そこを黙って混ぜると、教員は自分の課題で説明できない値を
    説明しようとすることになる。

    それ以外は**件数だけ**にする。担当していないコースの課題名は、成績に
    近い情報である。辿れなかったものもこちらに数える（`resolved`）。
    """
    here: list[Evidence] = []
    elsewhere = 0
    for item in state.evidence:
        run_id = str(item.grading_run_id)
        where = course_of.get(run_id)
        if where == course_id:
            here.append(
                Evidence(
                    grading_run_id=run_id,
                    course_id=where,
                    task_title=title_of.get(run_id),
                    score_ratio=item.score_ratio,
                    human_verified=item.human_verified,
                    observed_at=item.observed_at,
                )
            )
        else:
            elsewhere += 1
    # 新しい順。根拠は「最近どうだったか」から読む。
    here.sort(key=lambda item: item.observed_at, reverse=True)
    return LearnerKc(
        key=key,
        label=label,
        mastery=state.mastery,
        confidence=confidence_of(state),
        observation_count=state.observation_count,
        updated_at=state.updated_at,
        here=tuple(here),
        elsewhere=elsewhere,
    )
