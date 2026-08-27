"""科目プロファイル（subjects/*.yaml）。

科目の違いはコードではなくこの宣言で表す（ADR 0002）。
存在しない Evaluator 名を書けてしまうため、ロード時に必ず検証する。
起動時に落ちれば運用中に落ちない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from aijudge_core import EvaluatorKind, ReviewPolicy

from .registry import EvaluatorRegistry


class InputPolicy(BaseModel):
    """提出の受け付け方。手書き画像を許すかどうかはここで決まる。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allow_handwriting: bool = False
    transcription: str | None = None


class MeasurementPolicy(BaseModel):
    """採点性能の測定に関する宣言。

    **採点エンジンはこの値を読まない。** 科目プロファイルは科目の宣言文書で
    あって採点エンジンの設定ファイルではない（既に `input` は S3 の関心事、
    `review_policy` は HITL の関心事を宣言している）。測定の設定も、運用者が
    探す場所は同じであるべきなのでここに置く。読むのはレビュー側（ADR 0007）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # blind 採点（AI を見る前に教員が付ける）を求める提出の割合。
    # 0.0 なら求めない = 測定用データを集めない。
    #
    # 全件 blind にしないのは、レビュー 1 件ごとに 2 段階の入力を強制する
    # ことになり、測定コストを日常業務に転嫁するため。教員の任意選択にも
    # しない（難しい提出だけ blind にする等の選択バイアスが入ると、
    # 一致度がその分だけ意味を失う）。
    blind_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)


class SubjectProfile(BaseModel):
    """1 科目分の採点構成。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    description: str | None = None
    input: InputPolicy = InputPolicy()
    normalizers: tuple[str, ...] = ()
    deterministic: tuple[str, ...] = ()
    ai_evaluators: tuple[str, ...] = ()
    aggregation: Literal["weighted_sum"] = "weighted_sum"
    review_policy: ReviewPolicy = ReviewPolicy()
    # 測定の宣言。採点エンジンは参照しない（MeasurementPolicy 参照）。
    measurement: MeasurementPolicy = MeasurementPolicy()
    # 1 テストケースあたりの実行上限。科目によって妥当な値が違う
    # （入門課題は 1 秒で十分、数値計算課題はもっと要る）ため設定にする。
    timeout_seconds: float = Field(default=10.0, gt=0.0)
    # 評価器ごとの追加設定。キーは evaluator_id。
    evaluator_options: dict[str, dict[str, object]] = Field(default_factory=dict)

    def validate_against(self, registry: EvaluatorRegistry) -> None:
        """宣言された Evaluator が実在し、種別が宣言と一致することを確かめる。"""
        for evaluator_id in self.deterministic:
            evaluator = registry.get(evaluator_id)
            if evaluator.kind is not EvaluatorKind.DETERMINISTIC:
                raise ValueError(
                    f"{evaluator_id!r} is listed under 'deterministic' "
                    f"but declares kind={evaluator.kind}"
                )
        for evaluator_id in self.ai_evaluators:
            evaluator = registry.get(evaluator_id)
            if evaluator.kind is not EvaluatorKind.AI:
                raise ValueError(
                    f"{evaluator_id!r} is listed under 'ai_evaluators' "
                    f"but declares kind={evaluator.kind}"
                )
        declared = set(self.deterministic) | set(self.ai_evaluators)
        unknown = set(self.evaluator_options) - declared
        if unknown:
            raise ValueError(
                f"profile {self.name!r} sets options for evaluators it does not run: "
                f"{sorted(unknown)}"
            )
        if self.input.allow_handwriting and not self.input.transcription:
            raise ValueError(f"profile {self.name!r} allows handwriting but names no transcriber")


def load_profile(path: Path, registry: EvaluatorRegistry | None = None) -> SubjectProfile:
    """YAML を読み、必要なら Evaluator の実在を検証する。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a mapping")
    data.setdefault("name", path.stem)
    profile = SubjectProfile.model_validate(data)
    if registry is not None:
        profile.validate_against(registry)
    return profile


def load_profiles(
    directory: Path, registry: EvaluatorRegistry | None = None
) -> dict[str, SubjectProfile]:
    """ディレクトリ内の全プロファイルを読む。1 つでも壊れていれば例外。"""
    profiles: dict[str, SubjectProfile] = {}
    for path in sorted(directory.glob("*.yaml")):
        profile = load_profile(path, registry)
        if profile.name in profiles:
            raise ValueError(f"duplicate subject profile name: {profile.name!r}")
        profiles[profile.name] = profile
    return profiles
