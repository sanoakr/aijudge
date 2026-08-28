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

from datetime import UTC, datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aijudge_core import (
    Provenance,
    ReviewState,
    RubricCriterion,
    RubricLevel,
    TaskVersion,
    TestCase,
    derived_id,
)
from aijudge_core.ids import TaskId, TaskVersionId, UserId

DEFAULT_EVALUATOR = "code_test_runner"
AI_EVALUATOR = "rubric_ai_judge"

# 課題キーに許す文字。パスにもファイル名にもならないが、ID の素材になり、
# 画面にも出るので、素性の知れない文字は入れない。
_KEY_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_/.")


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
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    weight: float = Field(gt=0.0, le=1.0)
    evaluator: str | None = None
    levels: tuple[LevelSpec, ...] = Field(min_length=2)


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
    evaluator: str = DEFAULT_EVALUATOR
    reference_solution: str | None = None
    test_cases: tuple[TestCaseSpec, ...] = ()
    # 課題が観点を宣言する場合。空なら「正しさ（＋読みやすさ）」を組み立てる。
    criteria: tuple[CriterionSpec, ...] = ()

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
    return TaskVersion(
        id=TaskVersionId(derived_id("tsv", spec.key, str(version))),
        task_id=TaskId(derived_id("tsk", spec.key)),
        version=version,
        subject_profile=subject_profile,
        statement=spec.statement,
        reference_solution=spec.reference_solution,
        criteria=criteria,
        test_cases=cases,
        q_matrix=(),
        max_score=spec.max_score,
        allow_handwriting=False,
        provenance=Provenance(
            authored_by=authored_by,
            review_state=ReviewState.APPROVED,
            reviewed_by=authored_by,
        ),
        created_at=datetime.now(UTC),
    )


def build_task_version(
    spec: TaskSpec,
    *,
    subject_profile: str,
    authored_by: UserId,
    version: int = 1,
) -> TaskVersion:
    """宣言から課題版を作る。

    ID は `key` から決定的に導く。取り込みを何度流しても同じ課題を指し、
    保存済みの採点結果がどの観点の点なのかも辿れる（P8）。
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
        return _declared_version(spec, cases, subject_profile=subject_profile,
                                 authored_by=authored_by, version=version)

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

    return TaskVersion(
        id=TaskVersionId(derived_id("tsv", spec.key, str(version))),
        task_id=TaskId(derived_id("tsk", spec.key)),
        version=version,
        subject_profile=subject_profile,
        statement=spec.statement,
        reference_solution=spec.reference_solution,
        criteria=criteria,
        test_cases=cases,
        q_matrix=(),
        max_score=spec.max_score,
        allow_handwriting=False,
        provenance=Provenance(
            authored_by=authored_by,
            # 教員が明示的に足した課題なので、その時点で承認済みとする。
            review_state=ReviewState.APPROVED,
            reviewed_by=authored_by,
        ),
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
]
