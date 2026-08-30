"""コースごとの採点設定の上書き。

`subjects/*.yaml` は**雛形**である。同じ雛形を複数のコースが使い、コースは
そこからの差分だけを持つ。

    実効設定 = 雛形（ファイル） ← コースの上書き（DB）

**上書きはそのコースにしか効かない。** だから教員が画面から触ってよい ──
ADR 0002 が避けたかったのは「1 人の操作で全員の採点が止まる」ことで、
それはプロファイルそのものを書き換えられる場合の話である。評価器は
コースにも言語にも依存しない共有部品（`code_test_runner` は `language` で
言語を選ぶ 1 つの評価器、`rubric_ai_judge` は言語を知らない）なので、
コースごとに要るのは**組み合わせと値**だけで、そこに新しいコードは要らない。

**何でも上書きできるようにはしない。** 触ってよいのは、壊しても被害が
そのコースに閉じるものだけである。`kc_namespaces` は他のコースと共有する
語彙の範囲なので外す ── コース単位で変えられると、`cs` と `csci` の分裂が
別の経路から起きる。

検証は保存時に行う。`SubjectProfile` の模型検証に加えて、評価器が実在し
種別が宣言と一致することを確かめる（起動時と同じ検査）。**これで捕まらない
誤りが 1 つある** ── `language` の取り違えは設定として正しく、結果は
「全員 0 点」で原因が提出側に見える。関門にはできないので、実際に 1 件
走らせて確かめる道具（`aijudge_admin.grading_settings.try_settings`）を
別に置いてある。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .profile import SubjectProfile
from .registry import EvaluatorRegistry

# 上書きしてよい項目。**ここに無いものは黙って捨てる** ── 知らないキーを
# 通すと、雛形の意味が画面から分からないまま変わる。
ALLOWED_KEYS: tuple[str, ...] = (
    "deterministic",
    "ai_evaluators",
    "normalizers",
    "timeout_seconds",
    "evaluator_options",
    "measurement",
    # 人の確認を求める条件。**コースごとの運用値**である（合否境界は
    # 科目によって違い、確信度の水準は教員がレビューに割ける時間で決まる）。
    "review_policy",
)

# 上書きできない項目とその理由。画面に出す。
LOCKED_KEYS: dict[str, str] = {
    "kc_namespaces": (
        "知識要素の語彙は他のコースと共有します（コース単位で変えると体系が分裂します）"
    ),
    "name": "雛形の名前です",
    "aggregation": "集約の方式は採点エンジンの前提です",
}


class OverrideError(ValueError):
    """上書きが不正。呼び出し側は教員に理由を返せる。"""


def effective(
    base: SubjectProfile,
    overrides: dict[str, Any] | None,
    registry: EvaluatorRegistry | None = None,
) -> SubjectProfile:
    """雛形にコースの上書きを重ねた実効設定。

    上書きが空なら**雛形そのもの**を返す（既存のコースは今までと同じ挙動）。
    """
    if not overrides:
        return base
    data = base.model_dump()
    for key, value in overrides.items():
        if key not in ALLOWED_KEYS:
            continue
        if key in ("evaluator_options", "measurement", "review_policy") and isinstance(value, dict):
            merged = deepcopy(data.get(key) or {})
            for name, option in value.items():
                if isinstance(option, dict) and isinstance(merged.get(name), dict):
                    merged[name] = {**merged[name], **option}
                else:
                    merged[name] = option
            data[key] = merged
        else:
            data[key] = value
    try:
        profile = SubjectProfile.model_validate(data)
    except Exception as exc:
        raise OverrideError(f"採点設定が不正です: {exc}") from None
    if registry is not None:
        try:
            profile.validate_against(registry)
        except Exception as exc:
            # 存在しない評価器名・種別違い。起動時と同じ検査をここでもする。
            raise OverrideError(f"採点設定が不正です: {exc}") from None
    return profile


def diff(base: SubjectProfile, overrides: dict[str, Any] | None) -> dict[str, tuple[Any, Any]]:
    """雛形から何が変わっているか。画面に「雛形のまま／変更あり」を出すのに使う。"""
    if not overrides:
        return {}
    reference = base.model_dump()
    applied = effective(base, overrides).model_dump()
    return {
        key: (reference[key], applied[key])
        for key in ALLOWED_KEYS
        if reference.get(key) != applied.get(key)
    }


__all__ = ["ALLOWED_KEYS", "LOCKED_KEYS", "OverrideError", "diff", "effective"]
