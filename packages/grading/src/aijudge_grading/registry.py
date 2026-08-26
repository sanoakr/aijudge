"""Evaluator の発見と登録。

entry point（グループ `aijudge.evaluators`）から自動発見する。
採点エンジンが個々の Evaluator を import しないことが重要で、
これにより「科目を足してもエンジンは無変更」が構造的に保証される。
"""

from __future__ import annotations

from importlib.metadata import entry_points

from aijudge_core import EvaluatorKind

from .protocol import Evaluator

ENTRY_POINT_GROUP = "aijudge.evaluators"


class EvaluatorRegistry:
    """評価器の名前解決。"""

    def __init__(self) -> None:
        self._evaluators: dict[str, Evaluator] = {}

    def register(self, evaluator: Evaluator) -> None:
        existing = self._evaluators.get(evaluator.evaluator_id)
        if existing is not None and existing is not evaluator:
            raise ValueError(f"duplicate evaluator id: {evaluator.evaluator_id!r}")
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
