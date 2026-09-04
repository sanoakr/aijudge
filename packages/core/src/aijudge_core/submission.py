"""提出（Submission）と成果物（Artifact）、および提出フローの状態機械。

手書き画像提出（§05）のために、提出は「1 回の POST」ではなく状態を持つ。
OCR の書き起こしを学習者が確認・修正して確定させるまで採点に進めない。
この状態を後から足すのは破壊的変更になるため、手書き機能の実装が
PoC-3.5 まで無くても、状態機械は最初からコアに入れておく。

締切は `confirmed_at`（＝ SUBMITTED に入った時刻）で判定する。
書き起こし待ちで締切を跨ぐ事故を防ぐのは上位層の責務だが、
判定に使う時刻がどれかはここで一意に決めておく。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ids import ArtifactId, SubmissionId, TaskVersionId, UserId
from .tenancy import Role


class SubmissionState(StrEnum):
    DRAFT = "draft"
    TRANSCRIBING = "transcribing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    SUBMITTED = "submitted"


# 許可する遷移。ここに無い遷移は不正。
_TRANSITIONS: dict[SubmissionState, frozenset[SubmissionState]] = {
    # テキスト直接入力は DRAFT から一足飛びに SUBMITTED へ行ける。
    SubmissionState.DRAFT: frozenset({SubmissionState.TRANSCRIBING, SubmissionState.SUBMITTED}),
    SubmissionState.TRANSCRIBING: frozenset({SubmissionState.AWAITING_CONFIRMATION}),
    # ページを追加して再度書き起こす経路を残す。
    SubmissionState.AWAITING_CONFIRMATION: frozenset(
        {SubmissionState.TRANSCRIBING, SubmissionState.SUBMITTED}
    ),
    # 提出後は不変。訂正は新しい Submission を作る。
    SubmissionState.SUBMITTED: frozenset(),
}


def can_transition(source: SubmissionState, target: SubmissionState) -> bool:
    return target in _TRANSITIONS[source]


def assert_transition(source: SubmissionState, target: SubmissionState) -> None:
    if not can_transition(source, target):
        raise ValueError(f"illegal submission transition: {source} -> {target}")


class ArtifactRole(StrEnum):
    """Artifact の役割。

    ORIGINAL      学習者が出したそのもの（コード、手書き画像、PDF）
    TRANSCRIPTION ORIGINAL から機械が起こし、学習者が確定させたテキスト
    ATTACHMENT    書き起こさずに残す領域（図・グラフ）や補助ファイル
    """

    ORIGINAL = "original"
    TRANSCRIPTION = "transcription"
    ATTACHMENT = "attachment"


class ArtifactKind(StrEnum):
    CODE = "code"
    LATEX = "latex"
    MARKDOWN = "markdown"
    PDF = "pdf"
    # Word 文書。実運用のレポート提出に PDF と混在して現れる（実データで
    # 19 件のうち 2 件が .docx だった）。片方だけ受け付けると、学習者は
    # 変換の失敗を採点の失敗として受け取る。
    DOCX = "docx"
    IMAGE = "image"
    # 動画。学習者あたり数 GB になりうるので、取り込みも配信も
    # **メモリに載せずストリームで**扱う（専用ルート・専用ストア）。
    # 機械に採点させる評価器はまだ無く、観点は `HUMAN_SCORED` で宣言して
    # 教員が視聴して段階を入れる（`RubricCriterion.scored_by_human`）。
    VIDEO = "video"

    @property
    def is_document(self) -> bool:
        """本文が直接読めない文書か。**採点の前に本文へ変換する必要がある。**

        PDF や DOCX の中身をそのまま評価器に渡すと、AI にはバイナリが渡り、
        字数や節の判定も成立しない（設計方針 §4 の Normalize 段）。
        """
        return self in (ArtifactKind.PDF, ArtifactKind.DOCX)

    @property
    def is_streamed(self) -> bool:
        """取り込み・配信でメモリに全体を載せてはいけない種別か。

        真なら通常の `POST /submit`（`await upload.read()`）では受けず、
        専用のストリーミング経路を通す。
        """
        return self is ArtifactKind.VIDEO


class TranscriptionMeta(BaseModel):
    """書き起こしの由来と、学習者による修正の記録。

    `learner_edited` / `edit_diff` を残すのは、修正箇所の集積がそのまま
    OCR 精度の実測データになるため（教員修正データと同じ扱い）。
    `confirmed_at` が入るまで、この Artifact は採点に使えない。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    engine: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    prompt_version: str | None = None
    # 領域キー -> 確信度。低確信度の箇所を UI がハイライトするのに使う。
    confidence_map: dict[str, float] = Field(default_factory=dict)
    learner_edited: bool = False
    edit_diff: str | None = None
    confirmed_at: datetime | None = None
    confirmed_by: UserId | None = None

    @model_validator(mode="after")
    def _check_confirmation(self) -> Self:
        if (self.confirmed_at is None) != (self.confirmed_by is None):
            raise ValueError("confirmed_at and confirmed_by must be set together")
        if self.learner_edited and not self.edit_diff:
            raise ValueError("learner_edited requires an edit_diff")
        for key, value in self.confidence_map.items():
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"confidence for {key!r} out of range: {value}")
        return self

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None


class Artifact(BaseModel):
    """提出物の 1 ファイル / 1 テキスト。

    `content_hash` は Evidence のスパン有効性判定に使う（spans.py 参照）。
    内容が変われば hash が変わり、古い根拠は黙って別の場所を指すのではなく
    無効と判定できる。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: ArtifactId
    submission_id: SubmissionId
    role: ArtifactRole
    kind: ArtifactKind
    filename: str | None = None
    storage_key: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    byte_size: int = Field(ge=0)
    derived_from: ArtifactId | None = None
    transcription: TranscriptionMeta | None = None
    created_at: datetime

    @model_validator(mode="after")
    def _check_role(self) -> Self:
        if self.role is ArtifactRole.TRANSCRIPTION:
            if self.derived_from is None:
                raise ValueError("a transcription artifact must set derived_from")
            if self.transcription is None:
                raise ValueError("a transcription artifact must carry transcription metadata")
        elif self.transcription is not None:
            raise ValueError("transcription metadata is only valid on role=transcription")
        return self

    @property
    def is_gradable(self) -> bool:
        """採点パイプラインに渡してよいか。

        書き起こしは学習者が確定させたものだけを採点対象にする。
        原本画像は採点対象ではないが、図の解釈のために評価器へ併せて渡す。
        """
        if self.role is ArtifactRole.TRANSCRIPTION:
            return self.transcription is not None and self.transcription.is_confirmed
        return self.role is ArtifactRole.ORIGINAL


class Submission(BaseModel):
    """1 回の提出。SUBMITTED に入ったあとは不変。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: SubmissionId
    task_version_id: TaskVersionId
    learner_id: UserId
    # 出した人の**そのときの役割**（#108）。教員も TA も提出できる ── 課題の
    # 動作確認や参照解答を実際の経路に通すのは正当な用途である。ただし学習者の
    # 提出と混ざると、難易度推定・得点分布・blind の抽出・一致度 κ の分母に
    # 入り、「この課題は正答率が低い」として現れる。
    #
    # **役割はここに焼き付ける。** 測定時に現在の受講から引くと、学生が TA に
    # なった瞬間に**その人の過去の提出が測定から消える**。遅延減点で踏んだのと
    # 同じ罠で（ADR 0013）、結論も同じ ── 実行時の事実として記録に残し、
    # 表示時に計算し直さない。
    submitted_as: Role = Role.LEARNER
    state: SubmissionState = SubmissionState.DRAFT
    attempt: int = Field(default=1, ge=1)
    artifacts: tuple[Artifact, ...] = ()
    created_at: datetime
    submitted_at: datetime | None = None

    @model_validator(mode="after")
    def _check_state(self) -> Self:
        for artifact in self.artifacts:
            if artifact.submission_id != self.id:
                raise ValueError("Artifact.submission_id must match its Submission")
        if self.state is SubmissionState.SUBMITTED:
            if self.submitted_at is None:
                raise ValueError("a submitted Submission must have submitted_at")
            if not self.artifacts:
                raise ValueError("a submitted Submission must carry at least one artifact")
            unconfirmed = [
                artifact
                for artifact in self.artifacts
                if artifact.role is ArtifactRole.TRANSCRIPTION
                and not (artifact.transcription and artifact.transcription.is_confirmed)
            ]
            if unconfirmed:
                # 確認ステップを迂回して提出できる抜け道を型で塞ぐ（PoC-3.5 の合格基準）。
                raise ValueError("every transcription must be confirmed before submitting")
        elif self.submitted_at is not None:
            raise ValueError("submitted_at is only valid in the submitted state")
        return self

    @property
    def is_trial(self) -> bool:
        """成績にも測定にも数えない提出か（#108）。

        **採点はする。** 動作確認の提出が採点されないなら確認にならない。
        数えないのは成績・分布・難易度・一致度のほうである。
        """
        return self.submitted_as is not Role.LEARNER

    @property
    def gradable_artifacts(self) -> tuple[Artifact, ...]:
        return tuple(artifact for artifact in self.artifacts if artifact.is_gradable)

    @property
    def deadline_timestamp(self) -> datetime | None:
        """締切判定に使う時刻。書き起こし開始時刻ではなく確定時刻を使う。"""
        return self.submitted_at
