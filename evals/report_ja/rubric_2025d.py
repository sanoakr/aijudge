"""教員から直接聞いた 3 つの規則を入れたルーブリック（2025c の次）。

`rubric_2025c` は採点コメントから段階を起こしたもので、**採点表に書かれて
いないことは入っていなかった**。2026-08-29 に教員に確かめて、3 つ分かった。

**1. 体裁は基礎点である。** 「レポート課題が任意提出課題であるため、
提出しただけでも意欲があると見做してある程度の点数を与えている。実質的に
基礎点は体裁に与える形で採点している」。データもそう言っている ── 提出された
47 件のどの品質観点にも 0 が付いておらず、2023 年度の体裁は 28 件中 20 件が
満点である。だから**体裁は既定を満点にし、事務的な不備だけで下げる**
（`submission_compliance`）。本文の構造は見ない。2023 年度で比べると:

    節・字数・数値・形式を見る（2025c まで）  完全一致 42.9%
    「提出があれば満点」                      完全一致 71.4%

**2. 独自性は「講義で使ったものを超えたか」である。** 「講義内のサンプル実験や
演習などで利用したものを超えた独自の実験設定を行っているか」。2025c は
サンプルコード（httpServer.py 系）を基準にしていたが、**演習で扱ったものも
基準に含める**。

**3. localhost の測定は評価が下がる ── ただし引くのは実験先 1 か所である。**
「条件は正確な測定ができない localhost での実験では点数が低くなる」と聞いたので
条件の上限を下げてみたが、**教員自身の採点がそうなっていなかった。**
2025 年度 19 件で、localhost のみの 12 件の条件は平均 1.67（うち 8 件が満点）、
外部を測った 7 件は 1.71 で、**差が無い。**

減点は既に実験先（3 点）が担っている ── その 12 件はすべて実験先 0 である。
**同じ事実で二度引かない。** 条件は再現性だけを見る。

## 変えていないもの

観点・配点・段階の上限は `rubric_2025` と同じ（体裁 4 / 実験先 3 / 条件 2 /
独自性 8 / 考察 4 / 結果 2 = 23 点）。実験先・考察・結果の記述は 2025c のまま。
**比較して意味が出るのは、変えた 3 つの効果だけである。**

## 未実装 ── 受理の判定

本文の構造検査（必須節・字数・測定値）は捨てない。**採点の観点ではなく
受理の判定**（読めるか、採点に足るか）に回すもので、そこはパイプライン側の
概念が要るのでまだ作っていない。いまは `report_structure` を採点から
外しただけである。
"""

from __future__ import annotations

from rubric_2025 import (  # noqa: F401
    HUMAN_COLUMN,
    HUMAN_CSV,
    MAX_LEVEL,
    POINTS,
    SOURCE,
    STUDENT_RE,
    TOTAL_MAX,
    TOTAL_POINTS,
    TOTAL_WEIGHT,
    load_human,
)
from rubric_2025c import CRITERIA as _BASE_CRITERIA
from rubric_2025c import STATEMENT  # noqa: F401  rubric.py が属性として読む

DATASET = "2025d"

DESCRIPTOR_SOURCE = (
    "2025 年度の採点コメントと講義資料から起こし、教員に確かめた 3 つの規則"
    "（体裁＝基礎点・独自性＝講義と演習で使ったものを超えたか・"
    "localhost の減点は実験先 1 か所）"
    "を入れたもの"
)

# **決定的に採点する評価器。** 体裁は本文ではなく提出そのものから決まる。
DETERMINISTIC = ("submission_compliance",)


def _levels(items: list[tuple[int, str, str]], top: int) -> list[dict]:
    return [
        {"level": lv, "label": label, "descriptor": desc, "score_ratio": lv / top}
        for lv, label, desc in items
    ]


# **体裁 ── 提出そのものから決まる 3 段のはしご。**
# 教員が 2023 年度に使ったのは比率 1.0（20 件）/ 0.5（6 件）/ 0.1（2 件）の
# 3 つだけ、2025 年度は 1.0（11 件）/ 0.75（7 件）/ 0.5（1 件）。
# **連続した質の尺度ではなく、既定が満点で不備があれば落ちる形である。**
_FORMAT = {
    "code": "format",
    "title": "体裁（4 点）",
    "description": (
        "**提出したか、間に合ったか、指定の名前と形式か。文章の質ではない。**"
        "この課題は任意提出なので、**出したこと自体が基礎点になる**"
        "（教員の採点でも、提出された提出物に 0 は付いていない）。"
        "すべて S3 が持つ事実なので機械が確定させる。"
    ),
    "weight": POINTS["format"] / TOTAL_POINTS,
    "evaluator": "submission_compliance",
    "levels": [
        {
            "level": 0,
            "label": "採点できない",
            "descriptor": "採点できる提出物が無い、または不備が 2 つ以上ある",
            "score_ratio": 0.0,
        },
        {
            "level": 2,
            "label": "不備あり",
            "descriptor": "遅延・ファイル名・提出形式のいずれか 1 つが規則に反する",
            "score_ratio": 0.5,
        },
        {
            "level": 4,
            "label": "遵守",
            "descriptor": "出ていて、間に合っていて、名前と形式も規則どおり（既定）",
            "score_ratio": 1.0,
        },
    ],
}

# **条件 ── 再現できるか。localhost の減点はここではなく実験先が持つ。**
# 「localhost では正確な測定にならないので条件が下がる」と教員に聞いたが、
# 実採点は違った（上の docstring を見よ）。**聞いた規則でも、データが
# 否定したらデータを採る。** 記述は 2025c のままにしてある。
_CONDITIONS = {
    "code": "conditions",
    "title": "実験条件（2 点）",
    "description": (
        "他人が同じ測定を再現できるか。対象・変化させたパラメータ・"
        "固定した条件・試行回数が示されているか。\n\n"
        "**測定先の妥当性はここでは見ない。** localhost だけの測定は"
        "応答性能の測定として弱いが、それは実験先（3 点）が 0 として"
        "引いている。**同じ事実で二度引かない。**"
    ),
    "weight": POINTS["conditions"] / TOTAL_POINTS,
    "levels": _levels(
        [
            (0, "未達", "何をどう測ったのか読み取れない"),
            (
                1,
                "一部",
                "測定はしているが条件の記述が欠け、そのままでは再現できない。"
                "採点者が「実験の深さが不足」と書く提出はここになりやすい",
            ),
            (2, "達成", "条件が示され、他人が同じ測定を再現できる"),
        ],
        2,
    ),
}

# **独自性 ── 講義で配ったもの「と演習で扱ったもの」を超えたか。**
_ORIGINALITY_DESCRIPTION = (
    "**講義のサンプル実験や演習で使ったものを超えた、独自の実験設定を行って"
    "いるか。配点の 3 分の 1 を占める、この課題の本体である。**\n\n"
    "講義と演習で扱っているのは次である ── 逐次処理の httpServer.py、"
    "**スレッド化した httpThreadServer.py**、任意ファイルを返す httpServer3.py、"
    "および負荷ツールでこれらに負荷をかける手順。"
    "**したがって「スレッドの有無を比べた」は講義・演習の範囲内である。**\n\n"
    "見るのは**持ち込んだ要素の質**であって、変化させたパラメータの本数ではない。"
)


def _rebuilt() -> list[dict]:
    out = []
    for spec in _BASE_CRITERIA:
        if spec["code"] == "format":
            out.append(_FORMAT)
        elif spec["code"] == "conditions":
            out.append(_CONDITIONS)
        elif spec["code"] == "originality":
            out.append({**spec, "description": _ORIGINALITY_DESCRIPTION})
        else:
            out.append(spec)
    return out


CRITERIA = _rebuilt()

EVALUATOR_OPTIONS = {
    "submission_compliance": {
        # 課題文は PDF を指定している。**ファイル名の規則は入れない** ──
        # 2023 年度は 28 件中 9 件が規則に反しているのに減点は 1 件だけで、
        # 課題文には書かれていても採点には効いていない。
        "required_kinds": ["pdf"],
        # 締切は Task が持つもので、評価用のデータには入っていない。
        # 渡さなければ評価器は「遅延は見ていない」と理由に書く。
    },
}
