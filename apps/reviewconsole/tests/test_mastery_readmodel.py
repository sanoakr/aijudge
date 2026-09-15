"""習熟度を画面の形に畳む規則（#328）。

ここで固定したいのは 4 つ。

母数を偽らない   記録の無い組は 0 として数えない（平均を押し下げる）。
状態を出す       推移はその日までの最新値を持ち越す（その日に動いた人の平均ではない）。
縦軸を詰めない   0–100% を固定で取る（小さな動きが大きく見える）。
コースで割る     根拠は「このコース」と「それ以外」に割り、外は件数だけ。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aijudge_core import MasteryModel, SkillEvidence, SkillPoint, SkillState
from aijudge_core.ids import CriterionScoreId, GradingRunId, KcId, TenantId, UserId
from aijudge_reviewconsole.mastery import BANDS, overview, polyline, split_evidence, trend

TENANT = TenantId("ten_" + "0" * 32)
A = UserId("usr_" + "1" * 32)
B = UserId("usr_" + "2" * 32)
KC1 = KcId("kc_" + "a" * 32)
KC2 = KcId("kc_" + "b" * 32)
NOW = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
KCS = ((str(KC1), "cs.loops", "くりかえし"), (str(KC2), "cs.array", "配列"))


def _state(learner: UserId, kc: KcId, mastery: float, *, evidence=()) -> SkillState:
    return SkillState(
        tenant_id=TENANT,
        learner_id=learner,
        kc_id=kc,
        mastery=mastery,
        model=MasteryModel.BKT,
        observation_count=len(evidence) or 1,
        evidence=evidence,
        updated_at=NOW,
    )


def _point(learner: UserId, kc: KcId, mastery: float, *, at: datetime = NOW) -> SkillPoint:
    return SkillPoint(
        tenant_id=TENANT,
        learner_id=learner,
        kc_id=kc,
        mastery=mastery,
        observation_count=1,
        model=MasteryModel.BKT,
        recorded_at=at,
    )


def test_a_kc_nobody_has_been_asked_about_does_not_drag_the_mean_down() -> None:
    """**記録の無い組を 0 として数えない。**

    数えると、まだ誰も問われていない KC が全員 0 点として平均に効き、
    「この科目は全然できていない」という読みになる。
    """
    view = overview([_state(A, KC1, 0.8)], [], kcs=KCS, learners=2)

    assert view.mean == 0.8, "問われていない KC が平均に混ざっている"
    rows = {row.key: row for row in view.rows}
    assert rows["cs.loops"].learners == 1
    assert rows["cs.array"].learners == 0
    assert rows["cs.array"].mean is None, "0.0 だと「全員 0 点」と区別が付かない"


def test_the_denominator_is_stated_not_assumed() -> None:
    """記録のある受講者と、受講者そのものは別。**画面は両方出す。**"""
    view = overview([_state(A, KC1, 0.5)], [], kcs=KCS, learners=30)

    assert (view.learners_with_data, view.learners) == (1, 30)
    assert view.measured is True


def test_nothing_recorded_is_not_zero() -> None:
    view = overview([], [], kcs=KCS, learners=30)

    assert view.mean is None
    assert view.measured is False
    assert view.recorded_since is None, "記録が無いのに「いつから」を名乗っている"


def test_a_full_mastery_lands_in_the_top_band() -> None:
    """1.0 は最上段。**素直に掛けると段から溢れる。**"""
    view = overview([_state(A, KC1, 1.0)], [], kcs=KCS, learners=1)

    assert len(view.bands) == BANDS
    assert view.bands[-1] == 1


def test_the_trend_carries_the_last_value_forward() -> None:
    """**その日時点のコースの状態**を出す。その日に動いた人の平均ではない。

    持ち越さないと、1 人が 1 日に何度も出せばその人だけで平均が決まる。
    """
    later = NOW + timedelta(days=1)
    points = [
        _point(A, KC1, 0.2),
        _point(B, KC1, 0.8),
        # 翌日は A だけが動く。B の 0.8 は持ち越される。
        _point(A, KC1, 0.6, at=later),
    ]

    line = trend(points)

    assert [p.day for p in line] == [NOW.date(), later.date()]
    assert line[0].mean == 0.5
    assert line[1].mean == 0.7, "動かなかった学習者が落ちている"
    assert [p.pairs for p in line] == [2, 2]


def test_the_trend_denominator_grows_as_records_appear() -> None:
    """母数は増える。**線だけ見て「下がった」と読ませない**ので画面に出す。"""
    later = NOW + timedelta(days=1)
    line = trend([_point(A, KC1, 0.9), _point(B, KC1, 0.1, at=later)])

    assert [p.pairs for p in line] == [1, 2]
    assert line[1].mean == 0.5


def test_the_chart_does_not_rescale_to_the_data() -> None:
    """**縦軸は 0–100% で固定。** 詰めると、0.62 → 0.64 が画面いっぱいの
    上昇に見える。
    """
    line = trend([_point(A, KC1, 0.62), _point(B, KC1, 0.64, at=NOW + timedelta(days=1))])

    drawn = polyline(line, width=100, height=40)
    ys = [float(pair.split(",")[1]) for pair in drawn.split()]

    # 0.62 と 0.63（2 点目は A と B の平均）なら、高さの差は 1 割にも満たない。
    assert abs(ys[0] - ys[1]) < 1.0, "縦軸がデータに合わせて伸びている"
    assert all(0 <= y <= 40 for y in ys)


def test_a_single_point_still_draws() -> None:
    """1 点でも「ある」ことは分かる。"""
    assert polyline(trend([_point(A, KC1, 0.5)]), width=100, height=40) != ""


def _evidence(run: str, *, verified: bool = False) -> SkillEvidence:
    return SkillEvidence(
        grading_run_id=GradingRunId("grn_" + run * 32),
        criterion_score_id=CriterionScoreId("cs_" + "d" * 32),
        score_ratio=1.0,
        human_verified=verified,
        observed_at=NOW,
    )


def test_evidence_is_split_by_course_and_the_others_are_only_counted() -> None:
    """**習熟度はコースを跨いで動く。**

    1 つの値に担当外の科目の観測も入っている。黙って混ぜると、教員は自分の
    課題では説明できない値を説明しようとする。外のものは件数だけ ── 担当して
    いないコースの課題名は成績に近い情報である。
    """
    here, there = "1", "2"
    state = _state(A, KC1, 0.7, evidence=(_evidence(here), _evidence(there)))

    row = split_evidence(
        state,
        course_of={"grn_" + here * 32: "crs_here", "grn_" + there * 32: "crs_there"},
        title_of={"grn_" + here * 32: "合計", "grn_" + there * 32: "他科目の課題"},
        course_id="crs_here",
        key="cs.loops",
        label="くりかえし",
    )

    assert [e.task_title for e in row.here] == ["合計"]
    assert row.elsewhere == 1
    assert "他科目の課題" not in [e.task_title for e in row.here]


def test_evidence_that_cannot_be_traced_is_not_called_someone_elses() -> None:
    """辿れなかったものを「他コース」と書かない。**数には入れる。**"""
    state = _state(A, KC1, 0.7, evidence=(_evidence("3"),))

    row = split_evidence(
        state,
        course_of={},
        title_of={},
        course_id="crs_here",
        key="cs.loops",
        label="くりかえし",
    )

    assert row.here == ()
    assert row.elsewhere == 1
