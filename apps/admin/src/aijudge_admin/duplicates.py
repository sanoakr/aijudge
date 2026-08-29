"""生成した課題が既存のものと重なっていないかを見る（S2、設計方針 §5）。

**教員レビューの前に走らせ、判断材料にする。** 近い課題があること自体は
欠陥ではない ── 同じ単元を扱えば似るのが当然で、捨てるかどうかは教員が
決める（設計原則 P5）。

埋め込みが使えるならコサイン、使えなければ字面で測る。**どちらで測ったかを
必ず結果に残す** ── 字面だけでは言い換えた重複が見つからないので、
「検査した」とだけ言うと、見つからなかったことが安全の証拠に読める。
"""

from __future__ import annotations

import logging

from aijudge_authoring.similarity import (
    DEFAULT_SIMILARITY_THRESHOLD,
    DuplicateReport,
    SimilarityMethod,
    cosine,
    lexical,
    rank,
)
from aijudge_core import TaskVersion
from aijudge_core.ids import TaskVersionId
from aijudge_llm_gateway import DataClass, LlmError, LlmGateway, default_gateway

logger = logging.getLogger(__name__)


class DuplicateChecker:
    def __init__(
        self,
        repository,
        gateway: LlmGateway | None = None,
        *,
        embedding_model: str | None = None,
        threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        self._repository = repository
        self._gateway = gateway or default_gateway()
        # None なら埋め込みを使わない（字面だけで測る）。
        self._model = embedding_model
        self._threshold = threshold

    def check(
        self, version: TaskVersion, existing: dict[TaskVersionId, tuple[str, str]]
    ) -> DuplicateReport:
        """`existing` は `{課題版 ID: (題名, 課題文)}`。

        既存を呼び出し側から渡すのは、**どこまでを「既存」と見なすかが
        運用の判断だから**である（同じコースだけか、科目全体か、過去年度も含むか）。
        """
        if not existing:
            return DuplicateReport(
                method=SimilarityMethod.LEXICAL, threshold=self._threshold, compared=0
            )

        if self._model is not None:
            report = self._by_embedding(version, existing)
            if report is not None:
                return report

        return rank(
            {
                str(vid): (title, lexical(version.statement, statement))
                for vid, (title, statement) in existing.items()
            },
            method=SimilarityMethod.LEXICAL,
            threshold=self._threshold,
        )

    # -- internals ---------------------------------------------------------

    def _by_embedding(
        self, version: TaskVersion, existing: dict[TaskVersionId, tuple[str, str]]
    ) -> DuplicateReport | None:
        """埋め込みで測る。使えなければ None を返し、呼び出し側が字面に落とす。"""
        assert self._model is not None
        stored = self._repository.list_embeddings(
            model=self._model, subject_profile=version.subject_profile
        )
        missing = [vid for vid in existing if str(vid) not in stored]

        try:
            # **課題文だけを送る。** 学習者のデータは含まない（P7）。
            texts = (version.statement, *(existing[vid][1] for vid in missing))
            vectors = self._gateway.embed(
                texts, model=self._model, data_class=DataClass.NON_PERSONAL
            )
        except LlmError:
            # 埋め込みモデルが無い、落ちている。**黙って「重複なし」にしない** ──
            # 字面に落として、そう測ったことを結果に残す。
            logger.warning("埋め込みが使えないので字面で測ります", exc_info=True)
            return None

        mine, rest = vectors[0], vectors[1:]
        for vid, vector in zip(missing, rest, strict=True):
            self._repository.save_embedding(
                vid,
                model=self._model,
                subject_profile=version.subject_profile,
                vector=vector,
            )
            stored[str(vid)] = vector

        scores: dict[str, tuple[str, float]] = {}
        for vid, (title, _statement) in existing.items():
            vector = stored.get(str(vid))
            if vector is None or len(vector) != len(mine):
                # 次元が違う＝別のモデルで作ったもの。**比較しない。**
                continue
            scores[str(vid)] = (title, cosine(mine, vector))

        if not scores:
            return None
        report = rank(scores, method=SimilarityMethod.EMBEDDING, threshold=self._threshold)
        # 自分自身の埋め込みも残す。次に作る課題がこれと比べられるように。
        self._repository.save_embedding(
            version.id,
            model=self._model,
            subject_profile=version.subject_profile,
            vector=mine,
        )
        return report
