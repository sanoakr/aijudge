"""Evaluator の発見と登録。

entry point（グループ `aijudge.evaluators`）から自動発見する。
採点エンジンが個々の Evaluator を import しないことが重要で、
これにより「科目を足してもエンジンは無変更」が構造的に保証される。
"""

from __future__ import annotations

from importlib.metadata import entry_points

from aijudge_core import EvaluatorKind

from .protocol import Evaluator, Normalizer

ENTRY_POINT_GROUP = "aijudge.evaluators"
NORMALIZER_ENTRY_POINT_GROUP = "aijudge.normalizers"


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


class NormalizerRegistry:
    """正規化プラグインの名前解決。

    評価器と同じ仕組みにしてある（ADR 0002）。**採点エンジンは個々の
    Normalizer を import しない** ので、レポート課題を足す作業が
    「パッケージを 1 つ足して YAML に名前を書く」で済む。
    """

    def __init__(self) -> None:
        self._normalizers: dict[str, Normalizer] = {}

    def register(self, normalizer: Normalizer) -> None:
        existing = self._normalizers.get(normalizer.normalizer_id)
        if existing is not None and existing is not normalizer:
            raise ValueError(f"duplicate normalizer id: {normalizer.normalizer_id!r}")
        self._normalizers[normalizer.normalizer_id] = normalizer

    def replace(self, normalizer: Normalizer) -> None:
        """登録済みを差し替える（テストでスタブを挿す口）。"""
        self._normalizers[normalizer.normalizer_id] = normalizer

    def get(self, normalizer_id: str) -> Normalizer:
        try:
            return self._normalizers[normalizer_id]
        except KeyError:
            known = ", ".join(sorted(self._normalizers)) or "(none)"
            raise KeyError(
                f"unknown normalizer {normalizer_id!r}; registered: {known}"
            ) from None

    def __contains__(self, normalizer_id: object) -> bool:
        return normalizer_id in self._normalizers

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._normalizers))

    def load_installed(self) -> NormalizerRegistry:
        for entry_point in entry_points(group=NORMALIZER_ENTRY_POINT_GROUP):
            normalizer = entry_point.load()()
            if not isinstance(normalizer, Normalizer):
                raise TypeError(f"entry point {entry_point.name!r} did not produce a Normalizer")
            if normalizer.normalizer_id != entry_point.name:
                raise ValueError(
                    f"entry point name {entry_point.name!r} does not match "
                    f"normalizer_id {normalizer.normalizer_id!r}"
                )
            self.register(normalizer)
        return self


def default_normalizers() -> NormalizerRegistry:
    return NormalizerRegistry().load_installed()
