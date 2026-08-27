"""観測レコードの置き場所。

**DB ではなくファイルに置く。** 観測は測定側だけが読む公開用の投影で、
運用の正本（提出・採点結果）は DB にある。DB に同居させると、測定側が
運用テーブルを読むために採点の語彙を必要とし、「測定を消しても採点は動く」
が成立しなくなる（ADR 0007。契約 `measurement-does-not-depend-on-grading`
が実際にそれを禁じている）。

    <root>/<subject_profile>/<task_name>/observations/<submission>.json

この配置は測定側（`aijudge_evalrunner.observations`）が読む形と同じ。
DB に移すなら、運用テーブルではなく**公開用の読み取りモデル**として
別テーブルを切ること。

.. warning::

   観測には学習者の提出名と教員の採点が含まれる。個人情報なので
   リポジトリに置かない。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

from aijudge_observation import Observation

OBSERVATIONS_DIR = "observations"

# パスに使える文字だけ残す。ID とプロファイル名しか来ない想定だが、
# 「来ないはず」はパス・トラバーサルの防止にならない。
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _segment(value: str) -> str:
    cleaned = _SAFE.sub("_", value).strip("._")
    if not cleaned:
        raise ValueError(f"cannot build a path segment from {value!r}")
    return cleaned


class ObservationFileStore:
    """観測を書き、読む。

    投影なので上書きしてよい（正本は DB 側）。教員の採点が後から付いた
    ときは、同じファイルを書き直す。
    """

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def path_for(self, subject_profile: str, task_name: str, submission: str) -> Path:
        return (
            self.root
            / _segment(subject_profile)
            / _segment(task_name)
            / OBSERVATIONS_DIR
            / f"{_segment(submission)}.json"
        )

    def save(self, observations: Iterable[Observation]) -> Path | None:
        """1 提出ぶんの観測を書く。空なら何もしない。"""
        items = list(observations)
        if not items:
            return None
        first = items[0]
        if any(item.submission_key != first.submission_key for item in items):
            raise ValueError("save() takes the observations of a single submission")

        path = self.path_for(first.subject_profile, first.task_name, first.submission)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            [item.model_dump(mode="json") for item in items], ensure_ascii=False, indent=2
        )
        # 書き込み途中の中身を読ませない。壊れた JSON は測定側で例外になり、
        # そこで初めて気づくことになる。
        temporary = path.with_name(f".{path.name}.partial")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)
        return path

    def load(
        self, subject_profile: str, task_name: str, submission: str
    ) -> tuple[Observation, ...]:
        path = self.path_for(subject_profile, task_name, submission)
        if not path.is_file():
            return ()
        data = json.loads(path.read_text(encoding="utf-8"))
        return tuple(Observation.model_validate(item) for item in data)
