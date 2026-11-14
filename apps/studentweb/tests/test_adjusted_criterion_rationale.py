"""教員が段階を直した観点の見え方（2026-11-14）。

直す前の判定の理由（例: AI の「説明できている」）が、直した段階の隣にそのまま
並ぶと、段階と理由が食い違って見えた。固定したいのは 3 つ。

出どころ    直した観点の「判定」は担当教員（直す前が AI かテスト実行かも添える）
食い違わない 機械の理由を成績の説明として出さず、「担当教員から」へ案内する
消さない    直す前の理由は、そうと分かる見出しの下に折りたたんで残す（P8）
"""

from __future__ import annotations

import re

from aijudge_core import RubricCriterion, RubricLevel
from aijudge_core.ids import CriterionId
from aijudge_studentweb.app import TEMPLATES
from aijudge_studentweb.visibility import CriterionView

AI_RATIONALE = "コメントは手順の意味を説明できています。"


def _criterion() -> RubricCriterion:
    return RubricCriterion(
        id=CriterionId("crt_" + "1" * 32),
        code="comment_min",
        title="int min = i; のコメント",
        description="最小値の位置を仮定することを説明しているか。",
        weight=1.0,
        evaluator_id="comment_slot_judge",
        levels=(
            RubricLevel(
                level=0, label="できていない", descriptor="説明されていない", score_ratio=0.0
            ),
            RubricLevel(level=1, label="できている", descriptor="説明している", score_ratio=1.0),
        ),
    )


def _render(row: CriterionView) -> str:
    return TEMPLATES.env.get_template("_criterion_row.html").render(row=row)


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def test_an_adjusted_criterion_is_attributed_to_the_instructor() -> None:
    row = CriterionView(
        criterion=_criterion(),
        level=0,
        rationale=AI_RATIONALE,
        evidence_lines=(9,),
        pending=False,
        adjusted=True,
        by_ai=True,
    )

    html = _render(row)

    assert "担当教員" in html
    assert "AIの判定を調整" in _text(html).replace(" ", "")
    assert 'class="pill ai"' not in html


def test_the_machine_rationale_is_folded_under_a_before_adjustment_heading() -> None:
    row = CriterionView(
        criterion=_criterion(),
        level=0,
        rationale=AI_RATIONALE,
        evidence_lines=(9,),
        pending=False,
        adjusted=True,
        by_ai=True,
    )

    html = _render(row)

    assert "担当教員から" in html
    # 理由は消さずに、直す前のものだと分かる <details> の中に置く
    details = re.search(r"<details[^>]*>(.*?)</details>", html, re.DOTALL)
    assert details is not None
    assert "調整する前の判定の説明" in details.group(1)
    assert AI_RATIONALE in details.group(1)
    assert "該当行: 9" in details.group(1)
    # <details> の外には機械の理由を出さない
    outside = html.replace(details.group(0), "")
    assert AI_RATIONALE not in outside


def test_an_unadjusted_criterion_still_shows_its_rationale_inline() -> None:
    row = CriterionView(
        criterion=_criterion(),
        level=1,
        rationale=AI_RATIONALE,
        evidence_lines=(9,),
        pending=False,
        adjusted=False,
        by_ai=True,
    )

    html = _render(row)

    assert 'class="pill ai"' in html
    assert "<details" not in html
    assert AI_RATIONALE in html
    assert "担当教員から" not in html
