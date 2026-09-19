"""Evaluator の発見と登録。

entry point（グループ `aijudge.evaluators`）から自動発見する。
採点エンジンが個々の Evaluator を import しないことが重要で、
これにより「科目を足してもエンジンは無変更」が構造的に保証される。
"""

from __future__ import annotations

from importlib.metadata import entry_points

from aijudge_core import EvaluatorKind, Extractor

from .protocol import Evaluator

ENTRY_POINT_GROUP = "aijudge.evaluators"
EXTRACTOR_ENTRY_POINT_GROUP = "aijudge.extractors"


class EvaluatorRegistry:
    """評価器の名前解決。"""

    def __init__(self) -> None:
        self._evaluators: dict[str, Evaluator] = {}

    def register(self, evaluator: Evaluator) -> None:
        existing = self._evaluators.get(evaluator.evaluator_id)
        if existing is not None and existing is not evaluator:
            raise ValueError(f"duplicate evaluator id: {evaluator.evaluator_id!r}")
        self._evaluators[evaluator.evaluator_id] = evaluator

    def replace(self, evaluator: Evaluator) -> None:
        """登録済みの評価器を差し替える。

        テストで実 LLM の代わりにスタブを挿すための口。`register` が
        重複を拒むのは設定ミスを早く落とすためなので、意図した上書きは
        別のメソッドにして区別する。
        """
        self._evaluators[evaluator.evaluator_id] = evaluator

    def get(self, evaluator_id: str) -> Evaluator:
        try:
            return self._evaluators[evaluator_id]
        except KeyError:
            known = ", ".join(sorted(self._evaluators)) or "(none)"
            raise KeyError(
                f"unknown evaluator {evaluator_id!r}; registered evaluators: {known}"
            ) from None

    def __contains__(self, evaluator_id: object) -> bool:
        return evaluator_id in self._evaluators

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._evaluators))

    def ids_of_kind(self, kind: EvaluatorKind) -> tuple[str, ...]:
        return tuple(sorted(name for name, ev in self._evaluators.items() if ev.kind is kind))

    def load_installed(self) -> EvaluatorRegistry:
        """インストール済みパッケージから Evaluator を発見して登録する。"""
        for entry_point in entry_points(group=ENTRY_POINT_GROUP):
            factory = entry_point.load()
            evaluator = factory()
            if not isinstance(evaluator, Evaluator):
                raise TypeError(f"entry point {entry_point.name!r} did not produce an Evaluator")
            if evaluator.evaluator_id != entry_point.name:
                raise ValueError(
                    f"entry point name {entry_point.name!r} does not match "
                    f"evaluator_id {evaluator.evaluator_id!r}"
                )
            self.register(evaluator)
        return self


def default_registry() -> EvaluatorRegistry:
    return EvaluatorRegistry().load_installed()


class ExtractorRegistry:
    """抽出プラグインの名前解決。

    評価器と同じ仕組みにしてある（ADR 0002）。**採点エンジンも受付も個々の
    Extractor を import しない** ので、扱える提出物の種類を増やす作業が
    「パッケージを 1 つ足して YAML に名前を書く」で済む。

    PDF から本文を抜く実装（`document_text`）と、画像を読む実装
    （`image_text`）が同じ名前空間に並ぶ ── 対象が違うだけで仕事は同じ
    である（`aijudge_core.extraction`）。
    """

    def __init__(self) -> None:
        self._extractors: dict[str, Extractor] = {}

    def register(self, extractor: Extractor) -> None:
        existing = self._extractors.get(extractor.extractor_id)
        if existing is not None and existing is not extractor:
            raise ValueError(f"duplicate extractor id: {extractor.extractor_id!r}")
        self._extractors[extractor.extractor_id] = extractor

    def replace(self, extractor: Extractor) -> None:
        """登録済みを差し替える（テストでスタブを挿す口）。"""
        self._extractors[extractor.extractor_id] = extractor

    def get(self, extractor_id: str) -> Extractor:
        try:
            return self._extractors[extractor_id]
        except KeyError:
            known = ", ".join(sorted(self._extractors)) or "(none)"
            raise KeyError(f"unknown extractor {extractor_id!r}; registered: {known}") from None

    def __contains__(self, extractor_id: object) -> bool:
        return extractor_id in self._extractors

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._extractors))

    def load_installed(self) -> ExtractorRegistry:
        for entry_point in entry_points(group=EXTRACTOR_ENTRY_POINT_GROUP):
            extractor = entry_point.load()()
            if not isinstance(extractor, Extractor):
                raise TypeError(f"entry point {entry_point.name!r} did not produce a Extractor")
            if extractor.extractor_id != entry_point.name:
                raise ValueError(
                    f"entry point name {entry_point.name!r} does not match "
                    f"extractor_id {extractor.extractor_id!r}"
                )
            self.register(extractor)
        return self


def default_extractors() -> ExtractorRegistry:
    return ExtractorRegistry().load_installed()
