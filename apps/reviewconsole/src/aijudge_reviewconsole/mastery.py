"""習熟度を教員の画面に載せる形にする（#328）。

**読み出しの形を作るだけで、推定はしない。** BKT は `packages/skill` の中に
閉じており、ここがするのは「コースの受講者ぶんを集めて、分布と推移に畳む」
ことだけである ── 採点の分布（`submissions.distribution_for`）と同じ役回り。

## この画面が言えること・言えないこと

**習熟度はテナント単位で積み上がる。** 学習者 × KC で 1 つの値であって、
コース別には持っていない（`skill_states` の主キー）。だからここで「コースの
分布」と呼んでいるのは、**このコースの受講者を、このコースが使う KC で
切った断面**である。値そのものには、担当外の科目で得た観測も入っている。

**推移は記録がある範囲でしか描けない**（`SkillPoint`）。記録は移行を当てた
時点から積み上がるので、それ以前は空になる ── 空の図を「まだ動いていない」
と読ませないために、いつからの記録かを画面に出す。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from aijudge_core import SkillPoint, SkillState
from aijudge_skill.portfolio import Evidence, LearnerKc, confidence_of, split_evidence

#: 分布の段数。5 段（0–20 / …／80–100%）。
#:
#: **10 段にしない。** 受講者 30 名を 10 段に割ると 1 段あたり 3 名で、
#: 図が分布ではなく個人の並びになる。採点の分布（`.hist`・10 段）は 1 課題に
#: 数百件の提出が集まるので事情が違う。
BANDS = 5


@dataclass(frozen=True)
class KcRow:
    """1 つの KC についての断面。"""

    kc_id: str
    key: str
    label: str
    #: この KC に記録のある受講者の数。**受講者数ではない** ── まだ誰も
    #: この KC を問われていない課題があるうちは、母数が人数より小さい。
    learners: int
    mean: float | None
    bands: tuple[int, ...]


@dataclass(frozen=True)
class TrendPoint:
    """ある日の断面。"""

    day: date
    mean: float
    #: 母数（学習者 × KC の組のうち、その日までに記録のあるもの）。
    #: **増えていく。** 母数が動く図なので、線だけ見て「下がった」と
    #: 読まれないよう画面に出す。
    pairs: int


@dataclass(frozen=True)
class Overview:
    rows: tuple[KcRow, ...]
    bands: tuple[int, ...]
    mean: float | None
    #: 記録のある受講者 / 受講者
    learners_with_data: int
    learners: int
    trend: tuple[TrendPoint, ...]
    #: いつからの記録か。None なら 1 件も無い。
    recorded_since: datetime | None

    @property
    def measured(self) -> bool:
        return self.learners_with_data > 0


def _band(mastery: float) -> int:
    """習熟度を段に割る。1.0 は最上段に入れる（`int(1.0 * 5)` は 5 で溢れる）。"""
    return min(BANDS - 1, int(mastery * BANDS))


def band_label(index: int) -> str:
    """段の見出し（`0–20` のような形）。"""
    width = 100 // BANDS
    return f"{index * width}–{(index + 1) * width}"


def _mean(values: list[float]) -> float | None:
    """**空なら None。** 0.0 を返すと「全員が 0」と区別が付かない。"""
    return sum(values) / len(values) if values else None


def overview(
    states: Iterable[SkillState],
    points: Iterable[SkillPoint],
    *,
    kcs: tuple[tuple[str, str, str], ...],
    learners: int,
) -> Overview:
    """分布と推移に畳む。`kcs` は (kc_id, key, label) の並び（表示の順）。

    `states` と `points` は**呼ぶ側が絞ってから渡す** ── このコースの受講者、
    このコースが使う KC。ここで絞ると、絞り方が 2 か所に分かれる。
    """
    wanted = {kc_id for kc_id, _, _ in kcs}
    by_kc: dict[str, list[float]] = {kc_id: [] for kc_id in wanted}
    seen: set[str] = set()
    for state in states:
        kc_id = str(state.kc_id)
        if kc_id not in wanted:
            continue
        by_kc[kc_id].append(state.mastery)
        seen.add(str(state.learner_id))

    rows = []
    everything: list[float] = []
    for kc_id, key, label in kcs:
        values = by_kc.get(kc_id, [])
        everything.extend(values)
        bands = [0] * BANDS
        for value in values:
            bands[_band(value)] += 1
        rows.append(
            KcRow(
                kc_id=kc_id,
                key=key,
                label=label,
                learners=len(values),
                mean=_mean(values),
                bands=tuple(bands),
            )
        )

    total_bands = [0] * BANDS
    for value in everything:
        total_bands[_band(value)] += 1

    trail = tuple(point for point in points if str(point.kc_id) in wanted)
    return Overview(
        rows=tuple(rows),
        bands=tuple(total_bands),
        mean=_mean(everything),
        learners_with_data=len(seen),
        learners=learners,
        trend=trend(trail),
        recorded_since=trail[0].recorded_at if trail else None,
    )


def trend(points: Iterable[SkillPoint]) -> tuple[TrendPoint, ...]:
    """日ごとの平均習熟度。**その日までの最新値を持ち越して数える。**

    その日に動いた点だけを平均すると、「その日に採点された人たちの平均」に
    なってしまい、コースの状態にはならない ── 1 人が 1 日に何度も出せば
    その人だけで平均が決まる。持ち越せば「その日時点でのコースの状態」になる。

    母数（`pairs`）は増えていく。**記録の無い組は数に入らない** ── 0 として
    数えると、まだ誰も問われていない KC が全員 0 点として平均を押し下げる。

    `points` は古い順で渡す（保存層がそう返す）。
    """
    latest: dict[tuple[str, str], float] = {}
    out: list[TrendPoint] = []
    day: date | None = None
    for point in points:
        moment = point.recorded_at.date()
        if day is not None and moment != day:
            out.append(_snapshot(day, latest))
        day = moment
        latest[(str(point.learner_id), str(point.kc_id))] = point.mastery
    if day is not None:
        out.append(_snapshot(day, latest))
    return tuple(out)


def _snapshot(day: date, latest: dict[tuple[str, str], float]) -> TrendPoint:
    values = list(latest.values())
    return TrendPoint(day=day, mean=sum(values) / len(values), pairs=len(values))


def polyline(points: tuple[TrendPoint, ...], *, width: int, height: int) -> str:
    """推移を SVG の `points` 属性に直す。**0–100% を縦いっぱいに取る。**

    自動で縦軸を詰めない ── 0.62 から 0.64 への動きが画面いっぱいの上昇に
    見えると、読む人は実際より大きく動いたと受け取る。

    1 点しか無いときは線にならないので、同じ高さの短い線を引く（点が 1 つ
    あることは分かる）。
    """
    if not points:
        return ""
    if len(points) == 1:
        y = height - points[0].mean * height
        return f"0,{y:.1f} {width},{y:.1f}"
    step = width / (len(points) - 1)
    return " ".join(
        f"{index * step:.1f},{height - point.mean * height:.1f}"
        for index, point in enumerate(points)
    )


__all__ = [
    "BANDS",
    "Evidence",
    "KcRow",
    "LearnerKc",
    "Overview",
    "TrendPoint",
    "band_label",
    "confidence_of",
    "overview",
    "polyline",
    "split_evidence",
    "trend",
]
