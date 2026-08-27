"""課題定義の用意。

Sharif Judge の問題ディレクトリから `TaskVersion` を組み立てる。表示にも
採点にも同じ定義が要るので、1 か所に置いて使い回す（別々に組み立てると、
表示している観点と採点した観点が食い違いうる）。
"""

from __future__ import annotations

from pathlib import Path

from aijudge_authoring.importers import sharif_judge
from aijudge_core import TaskVersion
from aijudge_core.ids import UserId

from .store import QueueEntry

IMPORTER = UserId("usr_" + "0" * 32)


class TaskLoader:
    """課題定義を必要になった時点で組み立て、以後は使い回す。"""

    def __init__(self, *, readability_weight: float = 0.3) -> None:
        # AI 観点の重み。本来は課題側のルーブリックで宣言すべきもので、
        # Sharif Judge の問題ディレクトリにはその情報が無いため暫定で置いている。
        self.readability_weight = readability_weight
        self._cache: dict[str, TaskVersion] = {}

    def task_for(self, entry: QueueEntry) -> TaskVersion:
        key = f"{entry.subject_profile}/{entry.task_name}"
        if key not in self._cache:
            self._cache[key] = sharif_judge.import_problem(
                entry.task_dir,
                subject_profile=entry.subject_profile,
                authored_by=IMPORTER,
                readability_weight=self.readability_weight,
            )
        return self._cache[key]


def source_kind_path(entry: QueueEntry) -> Path:
    return entry.source_path
