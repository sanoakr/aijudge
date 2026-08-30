"""重複検出を実際に走らせる（S2、設計方針 §5）。

固定したいのは 4 つ。

埋め込みを使う   モデルがあればコサインで測り、そう結果に残す。
落ちたら字面へ   **黙って「重複なし」にしない。** 測り方を結果に残す。
モデルを跨がない 次元の違うベクトルは比較しない。
課題文だけ送る   学習者のデータは含まない（P7）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from aijudge_admin import DuplicateChecker
from aijudge_authoring.similarity import SimilarityMethod
from aijudge_core import Provenance, RubricCriterion, RubricLevel, TaskVersion
from aijudge_core.ids import CriterionId, TaskId, TaskVersionId
from aijudge_llm_gateway import LlmError, LlmGateway, ScriptedProvider
from aijudge_persistence import Database

MINE = TaskVersionId("tsv_" + "1" * 32)
TWIN = TaskVersionId("tsv_" + "2" * 32)
OTHER = TaskVersionId("tsv_" + "3" * 32)
STATEMENT = "## 2 数の和 ##\n\n2 つの整数を読み、その和を出力しなさい。"


def _version(vid: TaskVersionId, statement: str) -> TaskVersion:
    return TaskVersion(
        id=vid,
        task_id=TaskId("tsk_" + "5" * 32),
        version=1,
        subject_profile="cs_intro_c",
        statement=statement,
        criteria=(
            RubricCriterion(
                id=CriterionId("crt_" + "6" * 32),
                code="correctness",
                title="正しさ",
                description="テスト実行で判定する。",
                weight=1.0,
                levels=(
                    RubricLevel(level=0, label="未達", descriptor="x", score_ratio=0.0),
                    RubricLevel(level=1, label="達成", descriptor="y", score_ratio=1.0),
                ),
            ),
        ),
        max_score=100.0,
        provenance=Provenance(generated_by="stub"),
        created_at=datetime(2026, 8, 29, tzinfo=UTC),
    )


class NoEmbeddings(ScriptedProvider):
    """埋め込みを出せないプロバイダ。実在する構成である。"""

    def embed(self, request):  # type: ignore[override]
        raise LlmError("この構成に埋め込みモデルはありません")


def _existing() -> dict:
    return {
        TWIN: ("既存の双子", STATEMENT),
        OTHER: ("行列", "## 行列 ##\n\n行列の積を求めなさい。"),
    }


def test_an_embedding_model_finds_the_twin() -> None:
    database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
    try:
        with database.unit_of_work() as uow:
            checker = DuplicateChecker(
                uow.tasks, LlmGateway(ScriptedProvider([])), embedding_model="emb"
            )
            report = checker.check(_version(MINE, STATEMENT), _existing())
            uow.commit()
    finally:
        database.dispose()

    assert report.method is SimilarityMethod.EMBEDDING
    assert report.compared == 2
    assert report.too_close
    assert report.too_close[0].task_version_id == str(TWIN)


def test_a_provider_without_embeddings_falls_back_and_says_so() -> None:
    """**黙って「重複なし」にしない。**

    埋め込みが使えないことと、近い課題が無いことは別である。字面に落として、
    そう測ったことを結果に残す（言い換えは見つからない、と教員に伝わる）。
    """
    database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
    try:
        with database.unit_of_work() as uow:
            checker = DuplicateChecker(
                uow.tasks, LlmGateway(NoEmbeddings([])), embedding_model="emb"
            )
            report = checker.check(_version(MINE, STATEMENT), _existing())
    finally:
        database.dispose()

    assert report.method is SimilarityMethod.LEXICAL
    assert report.too_close, "同じ課題文なのに見つかっていない"
    assert "言い換えた重複は見つかりません" in report.summary()


def test_vectors_from_another_model_are_not_compared() -> None:
    """次元が同じでも意味空間が違う。混ぜると無関係な課題が似ていることになる。"""
    database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
    try:
        with database.unit_of_work() as uow:
            # 別のモデルで作った、次元の違うベクトルを既存として置く。
            uow.tasks.save_embedding(
                TWIN, model="emb", subject_profile="cs_intro_c", vector=(1.0, 0.0)
            )
            checker = DuplicateChecker(
                uow.tasks, LlmGateway(ScriptedProvider([])), embedding_model="emb"
            )
            report = checker.check(_version(MINE, STATEMENT), {TWIN: ("双子", STATEMENT)})
    finally:
        database.dispose()

    # 比較できるものが無いので埋め込みでは測れず、字面に落ちる。
    assert report.method is SimilarityMethod.LEXICAL


def test_nothing_to_compare_against_is_not_a_clean_bill() -> None:
    database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
    try:
        with database.unit_of_work() as uow:
            checker = DuplicateChecker(uow.tasks, LlmGateway(ScriptedProvider([])))
            report = checker.check(_version(MINE, STATEMENT), {})
    finally:
        database.dispose()

    assert not report.checked
    assert "検査していません" in report.summary()


def test_only_statements_are_sent() -> None:
    """課題文だけを送る。学習者のデータは含まない（P7）。"""
    provider = ScriptedProvider([])
    database = Database.connect("sqlite+pysqlite:///:memory:", create=True)
    try:
        with database.unit_of_work() as uow:
            DuplicateChecker(uow.tasks, LlmGateway(provider), embedding_model="emb").check(
                _version(MINE, STATEMENT), _existing()
            )
    finally:
        database.dispose()

    sent = provider.embed_calls[0].texts
    assert STATEMENT in sent
    assert all("usr_" not in text for text in sent)
