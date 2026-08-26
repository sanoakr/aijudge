"""テナント境界。

マルチテナント化の実装は PoC-5 だが、`tenant_id` を後から全テーブルに足すのは
現実的でないため、境界の語彙だけは最初から入れておく。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .ids import CourseId, TenantId, UserId


class Role(StrEnum):
    LEARNER = "learner"
    INSTRUCTOR = "instructor"
    ASSISTANT = "assistant"
    ADMIN = "admin"


class Tenant(BaseModel):
    """機関。単独運用時は 1 件だけ存在する。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: TenantId
    slug: str = Field(min_length=1)
    name: str = Field(min_length=1)


class Course(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: CourseId
    tenant_id: TenantId
    code: str = Field(min_length=1)
    title: str = Field(min_length=1)
    term: str = Field(min_length=1)
    subject_profile: str = Field(min_length=1)


class Enrollment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: TenantId
    course_id: CourseId
    user_id: UserId
    role: Role

    @property
    def can_grade(self) -> bool:
        return self.role in (Role.INSTRUCTOR, Role.ASSISTANT, Role.ADMIN)
