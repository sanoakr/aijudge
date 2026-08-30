"""ルーブリック（観点の並び）を画面の言葉と模型のあいだで往復させる。

観点は 2 か所で決まる。

    コースの共通ルーブリック（`Course.rubric`）… その科目に共通の観点
    課題の宣言（`TaskSpec.criteria`）………………… 個別に変えたい課題だけ

**課題の宣言が勝つ。** 宣言が無ければコースの共通、それも無ければ組み込みの
既定（正しさ＋読みやすさ）。レポートのコースなら構成・実験設計・考察が全課題に
共通で、課題ごとに書き写すのは写し間違いを増やすだけである。

**段階は 1 行 1 段で書かせる。** 観点 1 つにつき 4 段、それぞれ名前・説明・
割合があるので、入力欄に分けると 1 画面に 12 個以上並ぶ。`名前 | 説明 | 割合`
の行なら、既存の段階を読んで直すのも貼り付けるのもできる。

**重みの合計は 1.0。** 模型が要求する（`TaskVersion._check_weights`）ので、
ここで先に確かめて教員に言葉で返す ── 保存の瞬間に pydantic の英語の例外を
見せても直しようがない。

**並びは評価順である。** 集約が AND のとき、上から評価して 0% が出た時点で
打ち切る（`aijudge_core.gate_skipped`）。画面では観点ごとに数値で持たせ、
保存のときにその順に並べ替える ── 上下ボタンだと 1 手ごとに保存が要り、
10 観点を並べ替えるのに 10 回の往復になる。
"""

from __future__ import annotations

from aijudge_authoring import CriterionSpec, LevelSpec

from .operations import AdminError

# 段階を書かなかった観点に与える既定。4 段にしてあるのは部分点を表すため
# （0/1 の二値にすると「動くが読めない」が満点か 0 点かになる）。
DEFAULT_LEVELS: tuple[tuple[str, str, float], ...] = (
    ("未達", "満たしていない", 0.0),
    ("一部", "一部を満たす", 0.34),
    ("概ね", "おおむね満たす", 0.67),
    ("達成", "満たしている", 1.0),
)

# 重みの合計に許す誤差。模型と同じ（`TaskVersion._check_weights`）。
WEIGHT_TOLERANCE = 1e-6


def default_levels() -> tuple[LevelSpec, ...]:
    return tuple(
        LevelSpec(level=index, label=label, descriptor=descriptor, score_ratio=ratio)
        for index, (label, descriptor, ratio) in enumerate(DEFAULT_LEVELS)
    )


def parse_levels(text: str) -> tuple[LevelSpec, ...]:
    """`名前 | 説明 | 割合` の行を段階にする。空なら既定。

    割合は 0〜1。**上限は必ず 1.0 でなければならない**（模型の要求で、
    最上位の段階が満点でないと総合点が 100% に届かない）。
    """
    rows = [line.strip() for line in text.splitlines() if line.strip()]
    if not rows:
        return default_levels()

    levels: list[LevelSpec] = []
    for number, row in enumerate(rows):
        parts = [part.strip() for part in row.split("|")]
        if len(parts) != 3:
            raise AdminError(
                f"段階の {number + 1} 行目は「名前 | 説明 | 割合」の形で書いてください: {row!r}"
            )
        label, descriptor, raw = parts
        try:
            ratio = float(raw)
        except ValueError:
            raise AdminError(f"段階の割合が数値ではありません: {raw!r}") from None
        if not 0.0 <= ratio <= 1.0:
            raise AdminError(f"段階の割合は 0〜1 で書いてください: {ratio}")
        if not label or not descriptor:
            raise AdminError(f"段階の名前と説明は空にできません: {row!r}")
        levels.append(
            LevelSpec(level=number, label=label, descriptor=descriptor, score_ratio=ratio)
        )
    if len(levels) < 2:
        raise AdminError("段階は 2 つ以上必要です（達成と未達を区別できません）")
    if max(level.score_ratio for level in levels) != 1.0:
        raise AdminError("いちばん上の段階は割合 1.0 にしてください（満点に届かなくなります）")
    return tuple(levels)


def format_levels(levels) -> str:
    """段階を画面の行に戻す。`LevelSpec` と `RubricLevel` の両方を受ける。"""
    return "\n".join(
        f"{level.label} | {level.descriptor} | {level.score_ratio}"
        for level in sorted(levels, key=lambda item: item.level)
    )


def parse(rows: list[dict[str, str]]) -> tuple[CriterionSpec, ...]:
    """画面から来た行を観点にする。空行は捨てる。"""
    criteria: list[CriterionSpec] = []
    orders: list[float] = []
    codes: set[str] = set()
    for row in rows:

        def text(field: str, row: dict = row) -> str:
            # `to_rows` は重みを数値で返すので、画面から来た文字列と両方受ける。
            value = row.get(field)
            return "" if value is None else str(value).strip()

        code = text("code")
        title = text("title")
        if not code and not title:
            continue
        if not code or not title:
            raise AdminError("観点にはコードと題名の両方が要ります")
        if code in codes:
            raise AdminError(f"観点コード {code!r} が重複しています")
        codes.add(code)
        try:
            weight = float(text("weight") or 0)
        except ValueError:
            raise AdminError(f"{code}: 重みが数値ではありません") from None
        if not 0.0 < weight <= 1.0:
            raise AdminError(f"{code}: 重みは 0 より大きく 1 以下です")
        evaluator = text("evaluator") or None
        try:
            # 空欄は画面に出ていた順のまま（後ろに送らない）。
            order = float(text("order")) if text("order") else float(len(criteria))
        except ValueError:
            raise AdminError(f"{code}: 評価順が数値ではありません") from None
        orders.append(order)
        criteria.append(
            CriterionSpec(
                code=code,
                title=title,
                description=text("description") or title,
                weight=weight,
                evaluator=evaluator,
                levels=parse_levels(text("levels")),
            )
        )
    if not criteria:
        return ()
    # **評価順に並べ替える。** 同じ値なら画面の並びのまま（安定ソート）。
    criteria = [
        criterion
        for _order, _index, criterion in sorted(
            (order, index, criterion)
            for index, (order, criterion) in enumerate(zip(orders, criteria, strict=True))
        )
    ]
    total = sum(criterion.weight for criterion in criteria)
    if abs(total - 1.0) > WEIGHT_TOLERANCE:
        raise AdminError(
            f"重みの合計を 1.0 にしてください（いまは {total:.2f}）。"
            "観点ごとの重みが成績の配分そのものです。"
        )
    return tuple(criteria)


def to_rows(criteria) -> list[dict[str, object]]:
    """観点を画面の行にする。`CriterionSpec` と `RubricCriterion` の両方を受ける。"""
    rows: list[dict[str, object]] = []
    for criterion in criteria:
        evaluator = getattr(criterion, "evaluator", None) or getattr(
            criterion, "evaluator_id", None
        )
        rows.append(
            {
                # 画面に出す評価順。**1 から振り直す** ── 保存のたびに詰めて
                # おかないと、間に挿すために小数を書く運用になる。
                "order": len(rows) + 1,
                "code": criterion.code,
                "title": criterion.title,
                "description": criterion.description,
                "weight": criterion.weight,
                "evaluator": evaluator or "",
                "levels": format_levels(criterion.levels),
            }
        )
    return rows


def from_criteria(criteria) -> tuple[CriterionSpec, ...]:
    """保存済みの観点（`RubricCriterion`）を宣言（`CriterionSpec`）に戻す。

    訂正のときに要る ── 問題文だけ直す場合でも、**いまの観点をそのまま
    引き継がなければならない**。空で作り直すと、読みやすさの観点が黙って
    消えて次の版から採点されなくなる。
    """
    return tuple(
        CriterionSpec(
            code=criterion.code,
            title=criterion.title,
            description=criterion.description,
            weight=criterion.weight,
            evaluator=criterion.evaluator_id,
            levels=tuple(
                LevelSpec(
                    level=level.level,
                    label=level.label,
                    descriptor=level.descriptor,
                    score_ratio=level.score_ratio,
                )
                for level in criterion.levels
            ),
        )
        for criterion in criteria
    )


def from_stored(stored: tuple[dict, ...]) -> tuple[CriterionSpec, ...]:
    """保存されたコースの共通ルーブリックを模型に戻す。"""
    try:
        return tuple(CriterionSpec.model_validate(item) for item in stored)
    except Exception as exc:
        raise AdminError(f"保存されているルーブリックが不正です: {exc}") from None


__all__ = [
    "DEFAULT_LEVELS",
    "default_levels",
    "format_levels",
    "from_criteria",
    "from_stored",
    "parse",
    "parse_levels",
    "to_rows",
]
