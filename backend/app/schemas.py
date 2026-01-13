# ---------------------------------------------------------------------------
# schemas.py
#
# Pydantic request/response models.
#
# These schemas define the public API contract for the FastAPI application.
# They are used for:
# - request validation (inputs)
# - response serialization (outputs)
# - OpenAPI documentation generation
#
# Notes:
# - This codebase uses Pydantic v2 style (`model_dump()` in handlers).
# - Defaults are set defensively for endpoints that return partial shapes.
# ---------------------------------------------------------------------------

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, EmailStr, Field


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RegisterIn(BaseModel):
    secret: str
    email: EmailStr
    password: str
    password_confirm: str


class MeOut(BaseModel):
    email: EmailStr
    is_admin: bool
    assigned_project_ids: List[int] = Field(default_factory=list)
    banned_project_ids: List[int] = Field(default_factory=list)


class InviteCreateIn(BaseModel):
    email: Optional[EmailStr] = None


class InviteOut(BaseModel):
    link: str
    secret: str


class UserOut(BaseModel):
    id: int
    email: EmailStr
    is_admin: bool
    assigned_project_ids: List[int]
    banned_project_ids: List[int]


class UserUpdateIn(BaseModel):
    is_admin: Optional[bool] = None


class UserProjectsUpdateIn(BaseModel):
    assigned_project_ids: List[int] = Field(default_factory=list)
    banned_project_ids: List[int] = Field(default_factory=list)


class QuestionSetCreateIn(BaseModel):
    name: str
    data: Dict[str, Any]


class QuestionSetOut(BaseModel):
    id: int
    name: str
    created_at: str
    created_by: Optional[str] = None
    data: Dict[str, Any]


class ProjectCreateIn(BaseModel):
    name: str
    focalpoint_code: int


class ProjectQuestionSetsBatchIn(BaseModel):
    project_ids: List[int]


class ProjectOut(BaseModel):
    id: int
    name: str
    closed: bool
    focalpoint_code: int | None = None
    deleted_at: str | None = None


class ProjectUpdateIn(BaseModel):
    closed: Optional[bool] = None


class ProjectImportIn(BaseModel):
    projects: List[str]


class ProjectAssignQuestionSetsIn(BaseModel):
    question_set_ids: List[int]


class ProjectAddCustomQuestionIn(BaseModel):
    # Canonical question object: {text,type,required,id?}
    question: Dict[str, Any]
    section_title: str = "Custom"


class RecordCreateIn(BaseModel):
    answers: Dict[str, Any]


class RecordSummaryOut(BaseModel):
    id: int
    created_at: str


class RecordUpdateIn(BaseModel):
    answers: Dict[str, Any]


class ReviewIn(BaseModel):
    status: str = Field(pattern=r"^(pending|approved|rejected)$")
    comment: Optional[str] = None


class RecordOut(BaseModel):
    id: int
    created_at: str
    updated_at: Optional[str] = None
    review_status: str = "pending"
    review_comment: Optional[str] = None
    answers: Dict[str, Any]

    # question_id -> {text,type,required,...}
    # Some lightweight endpoints (e.g. /projects/{id}/last_record) return only answers.
    # Default to an empty map so those endpoints don't 500 on response validation.
    questions: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
