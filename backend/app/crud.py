# ---------------------------------------------------------------------------
# crud.py
#
# Database CRUD operations and domain-level helpers.
#
# This module encapsulates database access patterns used by the API routes in
# main.py. Centralizing DB operations keeps handlers small and consistent and
# makes it easier to change persistence behavior (e.g., adding audits, soft
# deletes, or permissions) without touching every endpoint.
#
# Design principles:
# - Functions take an explicit SQLAlchemy Session.
# - Raise FastAPI HTTPException for domain errors (404/400) as the routes do.
# - Keep JSON question-set canonicalization in utils.py and call it here.
# ---------------------------------------------------------------------------

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .models import (
    Invite,
    Project,
    ProjectQuestionSet,
    QuestionSet,
    Record,
    User,
    UserProjectAccess,
)
from .utils import (
    canonical_project_custom_questions,
    new_key,
    sha256_hex,
    to_canonical_question_set,
)

DEFAULT_QUESTION_SET_NAME = "default"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_or_404(db: Session, model: type, obj_id: Any, *, detail: str):
    obj = db.get(model, obj_id)
    if not obj:
        raise HTTPException(status_code=404, detail=detail)
    return obj


def _utcnow() -> datetime:
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# Question sets
# ---------------------------------------------------------------------------


def latest_question_set_by_name(
    db: Session, name: str, include_deleted: bool = False
) -> QuestionSet | None:
    q = select(QuestionSet).where(QuestionSet.name == name)
    if not include_deleted:
        q = q.where(QuestionSet.deleted_at.is_(None))
    return db.execute(q.order_by(QuestionSet.created_at.desc())).scalars().first()


def ensure_default_question_set(db: Session) -> QuestionSet:
    """Seed the built-in default question set if it does not yet exist."""
    existing = latest_question_set_by_name(db, DEFAULT_QUESTION_SET_NAME)
    if existing:
        return existing

    data = {
        "title": DEFAULT_QUESTION_SET_NAME,
        "sections": [
            {
                "title": "Monthly Update",
                "questions": [
                    {
                        "text": "Focalpoint code?",
                        "type": "integer",
                        "required": False,
                        "auto": {"source": "project_focalpoint_code"},
                    },
                    {
                        "text": "Project name?",
                        "type": "short_text",
                        "required": False,
                        "auto": {"source": "project_name"},
                    },
                    {"text": "Project manager?", "type": "short_text", "required": True, "remember": True},
                    {"text": "Project sponsor?", "type": "short_text", "required": True, "remember": True},
                    {
                        "text": "Funder or Programme?",
                        "type": "dropdown",
                        "required": True,
                        "remember": True,
                        "options": ["ATE", "DfT", "T9"],
                    },
                    {
                        "text": "Project Type?",
                        "type": "dropdown",
                        "required": True,
                        "remember": True,
                        "options": ["Engagement", "Construction", "Pipeline", "Other"],
                    },
                    {
                        "text": "Region?",
                        "type": "dropdown",
                        "required": True,
                        "remember": True,
                        "options": ["South", "M&E", "North", "London", "National"],
                    },
                    {"text": "Progress during last period?", "type": "long_text", "required": True},
                    {"text": "Focus for next month?", "type": "long_text", "required": True},
                    {
                        "text": "Has the NCN risk register been updated this calendar month?",
                        "type": "yes_no",
                        "required": True,
                    },
                    {
                        "text": "Key risks escalated on behalf of Project Sponsor?",
                        "type": "long_text",
                        "required": False,
                    },
                    {"text": "Agreed Project Deadline?", "type": "date", "required": False, "remember": True},
                    {"text": "Are the dates in the Infrastructure Plan correct?", "type": "yes_no", "required": True},
                    {
                        "text": "Have you identified and added any new significant issues to the ATE Issues Log this calendar month?",
                        "type": "yes_no",
                        "required": True,
                    },
                    {"text": "Significant issues to highlight?", "type": "long_text", "required": False},
                    {"text": "Any other comments?", "type": "long_text", "required": False},
                    {
                        "text": "Overall RAG status: time?",
                        "type": "dropdown_mapped",
                        "required": True,
                        "options": ["red", "amber", "green"],
                        "value_map": {"red": 2, "amber": 1, "green": 0},
                    },
                    {
                        "text": "Overall RAG status: budget?",
                        "type": "dropdown_mapped",
                        "required": True,
                        "options": ["red", "amber", "green"],
                        "value_map": {"red": 2, "amber": 1, "green": 0},
                    },
                    {
                        "text": "Overall RAG status: scope?",
                        "type": "dropdown_mapped",
                        "required": True,
                        "options": ["red", "amber", "green"],
                        "value_map": {"red": 2, "amber": 1, "green": 0},
                    },
                ],
            }
        ],
    }

    canonical = to_canonical_question_set(data, fallback_name=DEFAULT_QUESTION_SET_NAME)
    qs = QuestionSet(name=DEFAULT_QUESTION_SET_NAME, created_by_user_id=None, data=canonical)
    db.add(qs)
    db.commit()
    return qs


def create_question_set(db: Session, name: str, raw_data: Any, created_by_user_id: int | None) -> QuestionSet:
    """Create a new question set version and auto-upgrade project assignments."""
    canonical = to_canonical_question_set(raw_data, fallback_name=name)
    qs = QuestionSet(name=name, created_by_user_id=created_by_user_id, data=canonical)
    db.add(qs)
    db.commit()

    # Auto-upgrade all project references from older versions (same name) to this newest version.
    old_ids = db.execute(
        select(QuestionSet.id)
        .where(QuestionSet.name == name)
        .where(QuestionSet.id != qs.id)
        .where(QuestionSet.deleted_at.is_(None))
    ).scalars().all()

    if old_ids:
        rows = db.execute(
            select(ProjectQuestionSet).where(ProjectQuestionSet.question_set_id.in_(old_ids))
        ).scalars().all()

        for row in rows:
            exists_new = db.execute(
                select(ProjectQuestionSet)
                .where(ProjectQuestionSet.project_id == row.project_id)
                .where(ProjectQuestionSet.question_set_id == qs.id)
            ).scalar_one_or_none()

            if exists_new:
                db.delete(row)
            else:
                row.question_set_id = qs.id

        db.commit()

    return qs


def list_question_sets(db: Session) -> list[QuestionSet]:
    return db.execute(select(QuestionSet).order_by(QuestionSet.name, QuestionSet.created_at.desc())).scalars().all()


def delete_question_set(db: Session, question_set_id: int) -> None:
    qs = _get_or_404(db, QuestionSet, question_set_id, detail="Question set not found")
    db.execute(delete(ProjectQuestionSet).where(ProjectQuestionSet.question_set_id == question_set_id))
    db.delete(qs)
    db.commit()


# ---------------------------------------------------------------------------
# Users & invites
# ---------------------------------------------------------------------------


def get_admin_count(db: Session) -> int:
    return db.execute(select(func.count()).select_from(User).where(User.is_admin == True)).scalar_one()  # noqa: E712


def create_invite(db: Session, created_by_user_id: int | None, email: str | None) -> tuple[str, str]:
    key = new_key(18)
    secret = new_key(18)

    inv = Invite(
        key=key,
        secret_hash=sha256_hex(secret),
        email=email,
        created_by_user_id=created_by_user_id,
        created_at=_utcnow(),
    )
    db.add(inv)
    db.commit()
    return key, secret


def use_invite_and_create_user(
    db: Session,
    key: str,
    secret: str,
    email: str,
    password_hash: str,
    make_admin: bool,
) -> User:
    inv = db.execute(select(Invite).where(Invite.key == key)).scalar_one_or_none()
    if not inv or inv.used_at is not None:
        raise HTTPException(status_code=400, detail="Invalid or used invite key")

    if sha256_hex(secret) != inv.secret_hash:
        raise HTTPException(status_code=400, detail="Invalid secret")

    if inv.email and inv.email.lower() != email.lower():
        raise HTTPException(status_code=400, detail="Email does not match invite")

    exists = db.execute(select(User).where(User.email == email.lower())).scalar_one_or_none()
    if exists:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(email=email.lower(), password_hash=password_hash, is_admin=make_admin)
    db.add(user)
    db.flush()  # populate user.id for invite usage tracking

    inv.used_at = _utcnow()
    inv.used_by_user_id = user.id
    db.commit()
    return user


def list_users_with_access(db: Session) -> list[tuple[User, list[int], list[int]]]:
    users = db.execute(select(User).order_by(User.email)).scalars().all()
    res: list[tuple[User, list[int], list[int]]] = []

    for user in users:
        acc = db.execute(select(UserProjectAccess).where(UserProjectAccess.user_id == user.id)).scalars().all()
        assigned = [a.project_id for a in acc if a.access_type == "assigned"]
        banned = [a.project_id for a in acc if a.access_type == "banned"]
        res.append((user, assigned, banned))

    return res


def set_user_projects(db: Session, user_id: int, assigned: list[int], banned: list[int]) -> None:
    db.execute(delete(UserProjectAccess).where(UserProjectAccess.user_id == user_id))

    for pid in sorted(set(assigned)):
        db.add(UserProjectAccess(user_id=user_id, project_id=pid, access_type="assigned"))

    for pid in sorted(set(banned)):
        db.add(UserProjectAccess(user_id=user_id, project_id=pid, access_type="banned"))

    db.commit()


def get_user_access(db: Session, user_id: int) -> tuple[list[int], list[int]]:
    acc = db.execute(select(UserProjectAccess).where(UserProjectAccess.user_id == user_id)).scalars().all()
    assigned = [a.project_id for a in acc if a.access_type == "assigned"]
    banned = [a.project_id for a in acc if a.access_type == "banned"]
    return assigned, banned


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def accessible_projects_query(
    db: Session,
    user_id: int,
    include_closed: bool,
    include_deleted: bool = False,
):
    assigned, banned = get_user_access(db, user_id)
    q = select(Project)

    if not include_closed:
        q = q.where(Project.closed == False)  # noqa: E712

    if not include_deleted:
        q = q.where(Project.deleted_at.is_(None))

    if assigned:
        q = q.where(Project.id.in_(assigned))

    if banned:
        # SQLAlchemy 2.x supports `not_in`; older uses `notin_`.
        q = q.where(Project.id.not_in(banned))

    return q.order_by(Project.name)


def create_project(db: Session, name: str, focalpoint_code: int | None = None) -> Project:
    p = Project(
        name=name,
        focalpoint_code=focalpoint_code,
        closed=False,
        custom_questions={"title": "Custom", "sections": []},
    )
    db.add(p)
    db.flush()  # assign id

    # Auto-assign latest default question set (seed ensures it exists).
    qs = latest_question_set_by_name(db, DEFAULT_QUESTION_SET_NAME)
    if qs:
        db.add(ProjectQuestionSet(project_id=p.id, question_set_id=qs.id, position=0))

    db.commit()
    return p


def import_projects(db: Session, names: list[str]) -> int:
    count = 0
    for name in names:
        n = str(name).strip()
        if not n:
            continue
        exists = db.execute(select(Project).where(Project.name == n)).scalar_one_or_none()
        if not exists:
            create_project(db, n)
            count += 1

    # create_project commits; this commit is effectively a no-op but kept for compatibility
    db.commit()
    return count


def assign_project_question_sets(db: Session, project_id: int, question_set_ids: list[int]) -> None:
    db.execute(delete(ProjectQuestionSet).where(ProjectQuestionSet.project_id == project_id))
    for idx, qsid in enumerate(question_set_ids):
        db.add(ProjectQuestionSet(project_id=project_id, question_set_id=qsid, position=idx))
    db.commit()


def merged_project_form(db: Session, project_id: int) -> dict:
    p = _get_or_404(db, Project, project_id, detail="Project not found")

    links = db.execute(
        select(ProjectQuestionSet)
        .where(ProjectQuestionSet.project_id == project_id)
        .order_by(ProjectQuestionSet.position)
    ).scalars().all()

    sets: list[dict] = []
    for link in links:
        qs = db.get(QuestionSet, link.question_set_id)
        if qs:
            sets.append(qs.data)

    custom = canonical_project_custom_questions(p.custom_questions)

    merged_sections: list[dict] = []
    for idx, sdata in enumerate(sets):
        set_title = sdata.get("title", f"Set {idx + 1}")
        for sec in sdata.get("sections", []):
            merged_sections.append(
                {
                    "title": f"{set_title} — {sec.get('title', 'Section')}",
                    "questions": sec.get("questions", []),
                }
            )

    for sec in custom.get("sections", []):
        merged_sections.append(
            {
                "title": f"Custom — {sec.get('title', 'Section')}",
                "questions": sec.get("questions", []),
            }
        )

    return {"title": p.name, "sections": merged_sections}


def question_lookup_from_form(form: dict) -> dict[str, dict]:
    """Build a lookup of question_id -> metadata required by the frontend.

    The shape is intentionally minimal and mirrors the original behavior:
    only commonly used keys are included to reduce payload size.
    """
    out: dict[str, dict] = {}
    for sec in form.get("sections", []):
        for q in sec.get("questions", []):
            out[str(q["id"])] = {
                k: q.get(k)
                for k in (
                    "text",
                    "type",
                    "required",
                    "options",
                    "value_map",
                )
            }
    return out


# ---------------------------------------------------------------------------
# Project custom questions

# ---------------------------------------------------------------------------


def add_project_custom_question(db: Session, project_id: int, section_title: str, question: dict) -> Project:
    p = _get_or_404(db, Project, project_id, detail="Project not found")
    cq = canonical_project_custom_questions(p.custom_questions)

    sections = list(cq.get("sections", []))
    section = next((s for s in sections if s.get("title") == section_title), None)
    if section is None:
        section = {"title": section_title, "questions": []}
        sections.append(section)

    q = dict(question)
    q.setdefault("required", True)
    q.setdefault("type", "short_text")
    q.setdefault("id", str(__import__("uuid").uuid4()))

    section["questions"] = list(section.get("questions") or []) + [q]
    cq["sections"] = sections

    p.custom_questions = cq
    db.commit()
    return p


def update_project_custom_question(
    db: Session,
    project_id: int,
    question_id: str,
    new_section_title: str,
    question: dict,
) -> Project:
    p = _get_or_404(db, Project, project_id, detail="Project not found")
    cq = canonical_project_custom_questions(p.custom_questions)
    sections = list(cq.get("sections", []))

    found: dict | None = None
    found_section: dict | None = None
    for s in sections:
        for q in (s.get("questions") or []):
            if str(q.get("id")) == str(question_id):
                found = q
                found_section = s
                break
        if found:
            break

    if not found:
        raise HTTPException(status_code=404, detail="Custom question not found")

    # Remove from old section if moving.
    if found_section and found_section.get("title") != new_section_title:
        found_section["questions"] = [
            q for q in (found_section.get("questions") or []) if str(q.get("id")) != str(question_id)
        ]

    # Ensure target section exists.
    target = next((s for s in sections if s.get("title") == new_section_title), None)
    if target is None:
        target = {"title": new_section_title, "questions": []}
        sections.append(target)

    qnew = dict(question or {})
    qnew["id"] = str(question_id)
    qnew.setdefault("required", True)
    qnew.setdefault("type", "short_text")

    # Replace if present; else append.
    tgtqs = list(target.get("questions") or [])
    for i, qq in enumerate(tgtqs):
        if str(qq.get("id")) == str(question_id):
            tgtqs[i] = qnew
            break
    else:
        tgtqs.append(qnew)
    target["questions"] = tgtqs

    # Drop empty sections.
    cq["sections"] = [s for s in sections if s.get("questions")]
    p.custom_questions = cq
    db.commit()
    return p


def delete_project_custom_question(db: Session, project_id: int, question_id: str) -> Project:
    p = _get_or_404(db, Project, project_id, detail="Project not found")
    cq = canonical_project_custom_questions(p.custom_questions)
    sections = list(cq.get("sections", []))

    removed = False
    for s in sections:
        qs = list(s.get("questions") or [])
        new_qs = [q for q in qs if str(q.get("id")) != str(question_id)]
        if len(new_qs) != len(qs):
            removed = True
        s["questions"] = new_qs

    if not removed:
        raise HTTPException(status_code=404, detail="Custom question not found")

    cq["sections"] = [s for s in sections if s.get("questions")]
    p.custom_questions = cq
    db.commit()
    return p


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def create_record(db: Session, project_id: int, created_by_user_id: int | None, answers: dict) -> Record:
    rec = Record(project_id=project_id, created_by_user_id=created_by_user_id, answers=answers)
    db.add(rec)
    db.commit()
    return rec


def latest_record_for_user(db: Session, project_id: int, user_id: int):
    """Return latest non-deleted record for a project created by a specific user."""
    q = (
        select(Record)
        .where(Record.project_id == project_id)
        .where(Record.created_by_user_id == user_id)
        .where(Record.deleted_at.is_(None))
        .order_by(Record.created_at.desc())
        .limit(1)
    )
    return db.execute(q).scalars().first()


def latest_record_for_project(db: Session, project_id: int):
    q = (
        select(Record)
        .where(Record.project_id == project_id)
        .where(Record.deleted_at.is_(None))
        .order_by(Record.created_at.desc())
        .limit(1)
    )
    return db.execute(q).scalars().first()


def list_record_summaries(db: Session, project_id: int, limit: int = 200) -> list[Record]:
    return db.execute(
        select(Record).where(Record.project_id == project_id).order_by(Record.created_at.desc()).limit(limit)
    ).scalars().all()


def get_record(db: Session, record_id: int) -> Record | None:
    return db.get(Record, record_id)


def update_record(db: Session, record_id: int, user_id: int, answers: dict) -> Record:
    rec = _get_or_404(db, Record, record_id, detail="Record not found")
    rec.answers = answers
    rec.updated_by_user_id = user_id
    rec.updated_at = _utcnow()

    # Any edit resets review to pending.
    rec.review_status = "pending"
    rec.review_comment = None
    rec.reviewed_at = None
    rec.reviewed_by_user_id = None

    db.commit()
    return rec


def review_record(db: Session, record_id: int, admin_user_id: int, status: str, comment: str | None) -> Record:
    rec = _get_or_404(db, Record, record_id, detail="Record not found")
    rec.review_status = status
    rec.review_comment = comment
    rec.reviewed_at = _utcnow()
    rec.reviewed_by_user_id = admin_user_id
    db.commit()
    return rec
