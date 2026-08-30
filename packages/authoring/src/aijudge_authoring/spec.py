"""課題の宣言（`TaskSpec`）と、そこから版を作る規則。

**この型が課題を足す唯一の入口である。** 画面からも API からも CLI からも
ここを通す。通さない経路を作ると、ルーブリックの組み立て方が経路ごとに
分かれ、「画面から作った課題だけ観点が 1 つ足りない」が起きる（実際に
起きた ── zip 取り込みだけ `readability_weight` が既定 0.0 に固定されていて、
画面から入れた課題には AI 観点が付かなかった）。

Sharif Judge のディレクトリ形式を知っているのは `importers/sharif_judge.py`
だけで、それはこの型を組み立てる側に立つ。**移行元の形式を HTTP や画面の
語彙に持ち込まない。** 持ち込むと、移行が終わったあとも一生ついて回る。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aijudge_core import (
    HUMAN_SCORED,
    Aggregation,
    Provenance,
    QMatrixEntry,
    ReviewState,
    RubricCriterion,
    RubricLevel,
    TaskVersion,
    TestCase,
    derived_id,
    kc_id_for,
)
from aijudge_core.ids import TaskId, TaskVersionId, UserId

DEFAULT_EVALUATOR = "code_test_runner"
AI_EVALUATOR = "rubric_ai_judge"

# 課題キーに許す文字。パスにもファイル名にもならないが、ID の素材になり、
# 画面にも出るので、素性の知れない文字は入れない。
_KEY_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_/.")

# KC の正準キー（`namespace.path.path…`）。コアの KnowledgeComponent と
# 同じ規則で、こちらは文字列のまま検査する。
_KC_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class TestCaseSpec(BaseModel):
    """テストケース 1 件。入力と期待出力を**中身で**持つ。

    パスで持たない。サーバ上のパスを呼び出し元に指定させると、そこが
    読み取りの穴になる（zip 取り込みで同じ判断をしている）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    input: str = ""
    expected: str = ""
    # 学習者に中身を見せるか。既定は見せない（見せると答えを合わせられる）。
    hidden: bool = True
    weight: float = Field(default=1.0, gt=0.0)


class LevelSpec(BaseModel):
    """観点内の到達段階。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: int = Field(ge=0)
    label: str = Field(min_length=1)
    descriptor: str = Field(min_length=1)
    score_ratio: float = Field(ge=0.0, le=1.0)


class CriterionSpec(BaseModel):
    """課題が宣言する観点 1 つ。

    **プログラミング課題の 2 観点に固定できない。** レポート課題では教員が
    観点を決める（構成・実験設計・考察・引用など）ので、課題ごとに宣言できる
    必要がある。宣言しなければ従来どおり「正しさ（＋読みやすさ）」になる。

    `evaluator` を書かないと AI 評価器が担当する。決定的に判定できる観点
    （必須節が揃っているか、字数が足りているか）だけを評価器に指名する。

    **機械に採点させない観点**（画像のように AI 判定をまだ持たないもの）は
    `evaluator` に `HUMAN_SCORED` を書く。空とは別の状態で、空は「どの AI
    評価器からも対象」を意味する（ADR 0015）。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    weight: float = Field(gt=0.0, le=1.0)
    evaluator: str | None = None
    levels: tuple[LevelSpec, ...] = Field(min_length=2)

    @property
    def scored_by_human(self) -> bool:
        """機械に採点させない観点か（人が採点する）。"""
        return self.evaluator == HUMAN_SCORED


class TaskSpec(BaseModel):
    """課題 1 件の宣言。

    `key` が同一性の鍵である。同じ鍵で入れ直しても同じ課題 ID になるので、
    取り込みを何度流しても課題が増えない（`derived_id`）。移行では同じ
    ディレクトリを何度も流すことになるので、これは必須の性質である。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1, max_length=128)
    statement: str = Field(min_length=1)
    title: str | None = None
    # 何回目のまとまりか（例 "ex02"）と、その中の順序。一覧の階層化に使う。
    unit: str | None = None
    session: int | None = Field(default=None, ge=1)
    position: int | None = Field(default=None, ge=1)
    opens_at: datetime | None = None
    due_at: datetime | None = None
    max_score: float = Field(default=100.0, gt=0.0)
    # AI が担当する「読みやすさ」の重み。0 なら観点を作らない。
    readability_weight: float = Field(default=0.0, ge=0.0, lt=1.0)
    # 観点の畳み方（AND / OR）。**None ならコースの設定に従う。**
    # 課題ごとに例外を持てるようにしてあるのは、同じコースでも「動かなければ
    # そこで終わり」の課題と、部分点を積む課題が混ざるため。
    aggregation: Aggregation | None = None
    evaluator: str = DEFAULT_EVALUATOR
    reference_solution: str | None = None
    test_cases: tuple[TestCaseSpec, ...] = ()
    # 課題が観点を宣言する場合。空なら「正しさ（＋読みやすさ）」を組み立てる。
    criteria: tuple[CriterionSpec, ...] = ()
    # この課題が問う知識要素（KC）。正準キーで書く（例 `cs.loops.termination`）。
    #
    # **これが Q-matrix の入口である**（設計原則 P6）。空でよい ── 書かなければ
    # 採点は変わらず動き、S7 に届く `KcOutcome` が空になるだけである
    # （習熟度が付かない、という劣化で済む）。
    #
    # 重みは書かない。「この課題はこの KC をどれだけ問うているか」を教員に
    # 見積もらせる前に、**まず対応があるかどうかだけを集める。** 重みが要ると
    # 分かってから `QMatrixEntry.weight` を開ける。
    knowledge_components: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Self:
        bad = set(self.key) - _KEY_CHARS
        if bad:
            raise ValueError(f"key に使えない文字があります: {sorted(bad)}")
        # ID の素材にしかならないので経路にはならないが、画面にも出る値なので
        # 経路に見える形は受け付けない。
        if ".." in self.key or self.key.startswith("/"):
            raise ValueError(f"key の形が不正です: {self.key!r}")
        if self.due_at is not None and self.opens_at is not None and self.due_at <= self.opens_at:
            raise ValueError("締切が公開日時より前になっています")
        names = [case.name for case in self.test_cases]
        if len(set(names)) != len(names):
            raise ValueError("テストケース名が重複しています")
        keys = self.knowledge_components
        if len(set(keys)) != len(keys):
            raise ValueError("KC の指定が重複しています")
        for key in keys:
            # 形だけ検査する。**実在の検査はここではしない** ── 体系は
            # 科目ごとに育つもので、課題を書く時点で全部揃っている前提を
            # 置くと、KC を足すまで課題が登録できなくなる。
            if not _KC_KEY_RE.match(key):
                raise ValueError(f"KC の正準キーの形が不正です: {key!r}")
        if self.criteria:
            codes = [c.code for c in self.criteria]
            if len(set(codes)) != len(codes):
                raise ValueError("観点コードが重複しています")
            total = sum(c.weight for c in self.criteria)
            if abs(total - 1.0) > 1e-6:
                # 合計が 1.0 でないと集約が成立しない。ここで落とさないと、
                # 保存の直前にコアの検証が落ちて原因が分かりにくくなる。
                raise ValueError(f"観点の重みの合計は 1.0 でなければなりません（{total}）")
            if self.readability_weight > 0.0:
                # 両方を書けると、どちらが効くのか読めない。
                raise ValueError(
                    "criteria を宣言する課題では readability_weight を使えません"
                    "（読みやすさも観点として書いてください）"
                )
        return self

    @property
    def auto_graded(self) -> bool:
        """決定的評価器が担当する観点を持てるか。

        テストケースが無ければ持てない ── 持たせると、その観点は永久に
        採点されないまま全提出が教員に積まれる（設計原則 P5 が
        「全部を人間が見る」に化ける）。
        """
        return bool(self.test_cases)


def _declared_version(
    spec: TaskSpec,
    cases: tuple[TestCase, ...],
    *,
    subject_profile: str,
    authored_by: UserId,
    version: int,
) -> TaskVersion:
    """課題が観点を宣言している場合の版。

    観点 ID は課題キーと観点コードから決定的に導く（取り込み直しても同じ ID
    になり、保存済みの採点結果がどの観点の点なのか辿れる、P8）。
    """
    from .importers.sharif_judge import _criterion_id

    criteria = tuple(
        RubricCriterion(
            id=_criterion_id(spec.key, declared.code),
            code=declared.code,
            title=declared.title,
            description=declared.description,
            weight=declared.weight,
            levels=tuple(
                RubricLevel(
                    level=level.level,
                    label=level.label,
                    descriptor=level.descriptor,
                    score_ratio=level.score_ratio,
                )
                for level in sorted(declared.levels, key=lambda item: item.level)
            ),
            evaluator_id=declared.evaluator,
        )
        for declared in spec.criteria
    )
    version_id = TaskVersionId(derived_id("tsv", spec.key, str(version)))
    return TaskVersion(
        id=version_id,
        task_id=TaskId(derived_id("tsk", spec.key)),
        version=version,
        subject_profile=subject_profile,
        statement=spec.statement,
        reference_solution=spec.reference_solution,
        criteria=criteria,
        aggregation=spec.aggregation,
        test_cases=cases,
        q_matrix=q_matrix_for(spec.knowledge_components, version_id),
        max_score=spec.max_score,
        source_key=spec.key,
        allow_handwriting=False,
        provenance=Provenance(
            authored_by=authored_by,
            review_state=ReviewState.APPROVED,
            reviewed_by=authored_by,
        ),
        created_at=datetime.now(UTC),
    )


def q_matrix_for(keys: tuple[str, ...], task_version_id: TaskVersionId) -> tuple[QMatrixEntry, ...]:
    """宣言した KC を Q-matrix の行にする（設計原則 P6）。

    **KC の ID は正準キーから導く。** 体系を先に登録してから課題を書く、
    という順序を強制しないためである。同じキーは同じ ID になるので、
    KC の実体を後から足しても対応は繋がる（導出は `kc_id_for` に 1 本化）。
    **登録済みかどうかはここでは見ない** ── 模型の層は保存先を知らない。
    確かめるのは app 層（`aijudge_admin.kc.assert_registered`）である。

    重みは既定の 1.0 のまま置く。「どれだけ問うているか」を見積もらせる前に、
    まず対応があるかどうかだけを集める。

    **課題を足す経路が 3 つあるので、ここに 1 本化する**（宣言・画面・取り込み）。
    経路ごとに書くと、取り込んだ課題にだけ KC が付かない、が起きる ──
    `readability_weight` で実際に起きた形である。
    """
    return tuple(
        QMatrixEntry(task_version_id=task_version_id, kc_id=kc_id_for(key)) for key in keys
    )


def _provenance(
    authored_by: UserId, generated_by: str | None, prompt_version: str | None
) -> Provenance:
    """出所。**生成物は承認済みにしない**（P5）。"""
    if generated_by is None:
        # 教員が明示的に足した課題なので、その時点で承認済みとする。
        return Provenance(
            authored_by=authored_by,
            review_state=ReviewState.APPROVED,
            reviewed_by=authored_by,
        )
    return Provenance(
        authored_by=authored_by,
        generated_by=generated_by,
        generation_prompt_version=prompt_version,
        # 教員が読むまでは提案。**却下も承認もされていない。**
        review_state=ReviewState.IN_REVIEW,
    )


def build_task_version(
    spec: TaskSpec,
    *,
    subject_profile: str,
    authored_by: UserId,
    version: int = 1,
    generated_by: str | None = None,
    generation_prompt_version: str | None = None,
) -> TaskVersion:
    """宣言から課題版を作る。

    ID は `key` から決定的に導く。取り込みを何度流しても同じ課題を指し、
    保存済みの採点結果がどの観点の点なのかも辿れる（P8）。

    **`generated_by` を渡すと承認済みにしない。** 教員が明示的に足した課題は
    その時点で承認済みとしてよいが、生成物は違う ── AI の出力は提案であって
    確定ではない（設計原則 P5）。ここを共通にしていると、生成した課題が
    レビューを経ずにそのまま出題されうる。出所と版も残す（P8、承認率の測定）。
    """
    from .importers.sharif_judge import correctness_criterion, readability_criterion

    cases = tuple(
        TestCase(
            name=case.name,
            evaluator_id=spec.evaluator,
            # **キー名は評価器が読むものと一致していなければならない。**
            # `code_test_runner` は `payload.get("expected", "")` で読む。
            # 違う名前で書くと既定値の空文字と比較され、**全ケースが黙って
            # 不合格になる**（例外は出ない）。テストで固定してある。
            payload={"input": case.input, "expected": case.expected},
            hidden=case.hidden,
            weight=case.weight,
        )
        for case in spec.test_cases
    )

    # テストケースが無い課題は AI 観点だけで構成する。「自動採点できない
    # 課題」ではなく「**まだ**自動採点できない課題」で、実在する
    # （HTTP サーバ課題・自己採点課題・レポート課題）。
    if spec.criteria:
        return _declared_version(
            spec, cases, subject_profile=subject_profile, authored_by=authored_by, version=version
        )

    graded_by = spec.evaluator if spec.auto_graded else AI_EVALUATOR
    correctness = correctness_criterion(graded_by, spec.key)
    if not spec.auto_graded:
        correctness = correctness.model_copy(
            update={
                "title": "仕様の充足",
                "description": (
                    "課題の指示どおりに動作するか。自動テストがまだ無いため "
                    "AI が判定し、教員が確定させる。"
                ),
            }
        )

    criteria: tuple[RubricCriterion, ...]
    if spec.readability_weight > 0.0:
        criteria = (
            correctness.model_copy(update={"weight": 1.0 - spec.readability_weight}),
            readability_criterion(spec.readability_weight, AI_EVALUATOR, spec.key),
        )
    else:
        criteria = (correctness,)

    version_id = TaskVersionId(derived_id("tsv", spec.key, str(version)))
    return TaskVersion(
        id=version_id,
        task_id=TaskId(derived_id("tsk", spec.key)),
        version=version,
        subject_profile=subject_profile,
        statement=spec.statement,
        reference_solution=spec.reference_solution,
        criteria=criteria,
        aggregation=spec.aggregation,
        test_cases=cases,
        q_matrix=q_matrix_for(spec.knowledge_components, version_id),
        max_score=spec.max_score,
        source_key=spec.key,
        allow_handwriting=False,
        provenance=_provenance(authored_by, generated_by, generation_prompt_version),
        created_at=datetime.now(UTC),
    )


__all__ = [
    "AI_EVALUATOR",
    "DEFAULT_EVALUATOR",
    "CriterionSpec",
    "LevelSpec",
    "TaskSpec",
    "TestCaseSpec",
    "build_task_version",
    "q_matrix_for",
]
