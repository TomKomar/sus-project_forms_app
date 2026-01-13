# ---------------------------------------------------------------------------
# main.py
#
# FastAPI application entrypoint.
#
# This module wires together:
# - FastAPI app instance, middleware, and startup tasks
# - Lightweight schema migration (`ensure_schema`) for MVP deployments
# - Public and admin API routes
#
# The API is designed for a shared codebase:
# - Route handlers are kept thin; DB operations live in crud.py
# - Cross-cutting concerns (logging, rate limiting, request limits) are middleware
# - Configuration is centralized in config.py
# ---------------------------------------------------------------------------

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from . import crud
from .auth import (
    get_current_user,
    get_db,
    get_request_fingerprint,
    hash_password,
    issue_session,
    require_admin,
    revoke_session,
    verify_password,
)
from .config import COOKIE_SECURE, SESSION_COOKIE_NAME, SESSION_TTL_HOURS
from .db import Base, SessionLocal, engine
from .middleware import ApiLoggingMiddleware, BodySizeLimitMiddleware, RateLimitMiddleware
from .models import Project, ProjectQuestionSet, QuestionSet, User
from .schemas import (
    InviteCreateIn,
    InviteOut,
    LoginIn,
    MeOut,
    ProjectAddCustomQuestionIn,
    ProjectAssignQuestionSetsIn,
    ProjectCreateIn,
    ProjectImportIn,
    ProjectOut,
    ProjectQuestionSetsBatchIn,
    ProjectUpdateIn,
    QuestionSetCreateIn,
    QuestionSetOut,
    RecordCreateIn,
    RecordOut,
    RecordSummaryOut,
    RecordUpdateIn,
    RegisterIn,
    ReviewIn,
    UserOut,
    UserProjectsUpdateIn,
    UserUpdateIn,
)
from .utils import fmt_dt, sha256_hex

app = FastAPI(title="Project Forms App", openapi_url="/api/openapi.json", docs_url="/api/docs")

# Request-level safety and observability.
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(ApiLoggingMiddleware)
app.add_middleware(RateLimitMiddleware)


# ---------------------------------------------------------------------------
# Startup / schema management
# ---------------------------------------------------------------------------


def ensure_schema() -> None:
    """Auto-add missing columns based on SQLAlchemy models (lightweight migration).

    This keeps existing Postgres volumes working as the MVP evolves, without Alembic.
    It does **not** modify existing column types/constraints; it only adds missing columns.
    """
    from sqlalchemy import inspect

    with engine.begin() as conn:
        insp = inspect(conn)

        existing_tables = set(insp.get_table_names(schema="public"))

        for table_name, table in Base.metadata.tables.items():
            if table_name not in existing_tables:
                continue

            existing_cols = {c["name"] for c in insp.get_columns(table_name, schema="public")}

            for col in table.columns:
                if col.name in existing_cols:
                    continue

                coltype = col.type.compile(dialect=conn.dialect)

                # Safe defaults for existing rows.
                extra = ""
                if table_name == "projects" and col.name == "custom_questions":
                    extra = " NOT NULL DEFAULT '{}'::jsonb"
                elif table_name == "projects" and col.name == "closed":
                    extra = " NOT NULL DEFAULT false"

                sql = f'ALTER TABLE "{table_name}" ADD COLUMN "{col.name}" {coltype}{extra}'
                conn.execute(text(sql))
                existing_cols.add(col.name)
                print(f"[schema] added {table_name}.{col.name} {coltype}{extra}")


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_schema()

    # Print a bootstrap invite if no admin exists.
    db = SessionLocal()
    try:
        crud.ensure_default_question_set(db)
        if crud.get_admin_count(db) == 0:
            key, secret = crud.create_invite(db, created_by_user_id=None, email=None)
            print("\n" + "=" * 72)
            print("BOOTSTRAP ADMIN REGISTRATION")
            print(f"Open: http://localhost:8080/register.html?key={key}")
            print(f"Secret: {secret}")
            print("This is printed ONLY when there is no admin user in the database.")
            print("=" * 72 + "\n")
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Shared helpers (used by multiple routes)
# ---------------------------------------------------------------------------


def _set_auth_state_for_rate_limit(request: Request, user_id: int, auth_type: str) -> None:
    """Populate request.state so the rate-limit and logging middleware can key properly."""
    request.state.user_id = user_id
    request.state.auth_type = auth_type


def _session_cookie_max_age_seconds() -> int:
    return int(SESSION_TTL_HOURS) * 60 * 60


def _require_project_access(
    db,
    user: User,
    project_id: int,
    *,
    include_closed: bool,
    on_forbidden_status: int = 403,
    on_forbidden_detail: str = "No access to project",
):
    """Return the project if accessible, otherwise raise an HTTPException."""
    q = crud.accessible_projects_query(db, user.id, include_closed=include_closed).where(Project.id == project_id)
    allowed = db.execute(q).scalars().first()
    if not allowed:
        raise HTTPException(status_code=on_forbidden_status, detail=on_forbidden_detail)
    return allowed


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except Exception:
        return False


def _norm_label(value: str) -> str:
    return " ".join(str(value).strip().split()).casefold()


def _normalize_yes_no(v: Any) -> Any:
    if v is True:
        return "yes"
    if v is False:
        return "no"
    if isinstance(v, str):
        vv = v.strip().lower()
        if vv in ("true", "t", "1", "yes", "y"):
            return "yes"
        if vv in ("false", "f", "0", "no", "n"):
            return "no"
    if v in (0, 1):
        return "yes" if v == 1 else "no"
    return v


def _infer_question_from_value(label: str, raw: Any) -> tuple[dict, Any]:
    """Infer a canonical question definition from a posted value.

    Returns:
        (question_definition, normalized_value)
    """
    q: Dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "text": str(label).strip(),
        "required": False,
    }

    # Structured payload: {type, value, options, value_map}
    if isinstance(raw, dict):
        meta = raw
        qtype = meta.get("type") or meta.get("qtype")
        val = meta.get("value") if "value" in meta else meta.get("answer", meta)
        if qtype:
            q["type"] = str(qtype)
        if isinstance(meta.get("options"), list):
            q["options"] = meta.get("options")
        if isinstance(meta.get("value_map"), dict):
            q["value_map"] = meta.get("value_map")
        raw = val

    if "type" not in q:
        if isinstance(raw, bool):
            q["type"] = "yes_no"
            raw = _normalize_yes_no(raw)
        elif isinstance(raw, (int, float)):
            q["type"] = "numeric"
        elif isinstance(raw, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw.strip()):
            q["type"] = "date"
        else:
            q["type"] = "long_text" if isinstance(raw, str) and len(raw) > 80 else "short_text"

    if q.get("type") == "yes_no":
        raw = _normalize_yes_no(raw)

    return q, raw


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@app.post("/api/login")
def login(payload: LoginIn, request: Request, response: Response, db=Depends(get_db)):
    user = db.execute(select(User).where(User.email == payload.email.lower())).scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    ip, ua = get_request_fingerprint(request)
    token = issue_session(user, ip, ua, db)

    _set_auth_state_for_rate_limit(request, user.id, "session")
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        max_age=_session_cookie_max_age_seconds(),
        path="/",
    )
    return {"ok": True, "email": user.email, "is_admin": user.is_admin, "token": token}


@app.post("/api/logout")
def logout(request: Request, response: Response, db=Depends(get_db), user=Depends(get_current_user)):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        revoke_session(token, db)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@app.post("/api/register")
def register(key: str, payload: RegisterIn, request: Request, response: Response, db=Depends(get_db)):
    if payload.password != payload.password_confirm:
        raise HTTPException(status_code=400, detail="Passwords do not match")
    if len(payload.password.encode("utf-8")) > 256:
        raise HTTPException(status_code=400, detail="Password too long")

    make_admin = crud.get_admin_count(db) == 0  # first user becomes admin
    user = crud.use_invite_and_create_user(
        db=db,
        key=key,
        secret=payload.secret,
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        make_admin=make_admin,
    )

    # Auto-login.
    ip, ua = get_request_fingerprint(request)
    token = issue_session(user, ip, ua, db)

    _set_auth_state_for_rate_limit(request, user.id, "session")
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="strict",
        max_age=_session_cookie_max_age_seconds(),
        path="/",
    )
    return {"ok": True, "email": user.email, "is_admin": user.is_admin}


@app.get("/api/me", response_model=MeOut)
def me(request: Request, db=Depends(get_db), user=Depends(get_current_user)):
    assigned, banned = crud.get_user_access(db, user.id)
    _set_auth_state_for_rate_limit(request, user.id, request.state.auth_type)
    return MeOut(email=user.email, is_admin=user.is_admin, assigned_project_ids=assigned, banned_project_ids=banned)


@app.post("/api/me/api_token/regenerate")
def regenerate_api_token(request: Request, db=Depends(get_db), user=Depends(get_current_user)):
    import secrets

    token = secrets.token_urlsafe(32)
    user.api_token_hash = sha256_hex(token)
    db.commit()

    _set_auth_state_for_rate_limit(request, user.id, request.state.auth_type)
    return {"api_token": token}


# ---------------------------------------------------------------------------
# Project routes
# ---------------------------------------------------------------------------


@app.get("/api/projects", response_model=List[ProjectOut])
def list_projects(request: Request, include_closed: bool = False, db=Depends(get_db), user=Depends(get_current_user)):
    _set_auth_state_for_rate_limit(request, user.id, request.state.auth_type)

    # Closed projects hidden from non-admin dropdown; admins may include if they ask.
    q = crud.accessible_projects_query(db, user.id, include_closed=(include_closed and user.is_admin))
    projects = db.execute(q).scalars().all()

    resp = [
        ProjectOut(id=p.id, name=p.name, closed=p.closed, focalpoint_code=getattr(p, "focalpoint_code", None))
        for p in projects
    ]
    return JSONResponse(content=[r.model_dump() for r in resp], headers={"x-returned-items": str(len(resp))})


@app.post("/api/projects", response_model=ProjectOut)
def create_project_self_service(request: Request, payload: ProjectCreateIn, db=Depends(get_db), user=Depends(get_current_user)):
    """Allow any authenticated user to create a project from the main dropdown.

    Access rules:
    - If user has explicit assigned projects (non-empty), we auto-assign them to the new project
      so it appears in their dropdown.
    - If user has no assignments (and no bans), they can already see everything.
    """
    _set_auth_state_for_rate_limit(request, user.id, request.state.auth_type)

    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name required")

    fp = payload.focalpoint_code
    if fp is None:
        raise HTTPException(status_code=400, detail="Focalpoint code required")

    # If exists, return it (idempotent behavior).
    existing = db.execute(select(Project).where(Project.name == name)).scalar_one_or_none()
    if existing:
        # If it was soft-deleted, restore it.
        if getattr(existing, "deleted_at", None) is not None:
            existing.deleted_at = None
            existing.closed = False
            existing.focalpoint_code = fp
            db.add(existing)
            db.commit()

        return ProjectOut(
            id=existing.id,
            name=existing.name,
            closed=existing.closed,
            focalpoint_code=existing.focalpoint_code,
            deleted_at=(existing.deleted_at.isoformat(timespec="seconds") + "Z") if existing.deleted_at else None,
        )

    p = crud.create_project(db, name, fp)

    # Ensure creator can see it if they are in "assigned-only" mode.
    assigned, banned = crud.get_user_access(db, user.id)
    if assigned:
        assigned2 = list(set(assigned + [p.id]))
        crud.set_user_projects(db, user.id, assigned2, banned)

    return ProjectOut(id=p.id, name=p.name, closed=p.closed, focalpoint_code=p.focalpoint_code)


@app.get("/api/projects/{project_id}/form")
def project_form(request: Request, project_id: int, db=Depends(get_db), user=Depends(get_current_user)):
    _set_auth_state_for_rate_limit(request, user.id, request.state.auth_type)

    _require_project_access(
        db,
        user,
        project_id,
        include_closed=user.is_admin,
        on_forbidden_status=403,
        on_forbidden_detail="No access to project",
    )

    return crud.merged_project_form(db, project_id)


@app.post("/api/projects/{project_id}/records", response_model=RecordSummaryOut)
def create_record(request: Request, project_id: int, payload: RecordCreateIn, db=Depends(get_db), user=Depends(get_current_user)):
    _set_auth_state_for_rate_limit(request, user.id, request.state.auth_type)

    _require_project_access(
        db,
        user,
        project_id,
        include_closed=user.is_admin,
        on_forbidden_status=403,
        on_forbidden_detail="No access to project",
    )

    # Validate required questions.
    form = crud.merged_project_form(db, project_id)
    qlookup = crud.question_lookup_from_form(form)
    answers: Dict[str, Any] = dict(payload.answers or {})

    # Auto-fill from project fields (currently a no-op unless qlookup includes `auto`).
    proj = db.get(Project, project_id)
    if proj:
        for qid, qinfo in qlookup.items():
            auto = qinfo.get("auto") if isinstance(qinfo, dict) else None
            if isinstance(auto, dict):
                src = auto.get("source")
                if src == "project_name":
                    answers[qid] = proj.name
                elif src == "project_focalpoint_code":
                    if getattr(proj, "focalpoint_code", None) is not None:
                        answers[qid] = int(proj.focalpoint_code)

    # Allow posting answers for custom questions that do not exist yet.
    #
    # Two supported mechanisms:
    # 1) Use a *human readable label* as the key in `answers`.
    #    Example: {"Local stakeholder sentiment": "positive"}
    #    -> creates a bespoke custom question on that project and stores the value.
    #
    # 2) Use an unknown UUID as the key, with the value being an object that
    #    contains question metadata, e.g.
    #    {"<uuid>": {"text": "Local stakeholder sentiment", "type": "short_text", "value": "positive"}}
    #
    # This is primarily to support seeding / automation clients.
    label_to_qid: Dict[str, str] = {}
    for qid, qinfo in qlookup.items():
        if isinstance(qinfo, dict) and qinfo.get("text"):
            label_to_qid[_norm_label(qinfo.get("text"))] = qid

    unknown_keys = [k for k in list(answers.keys()) if k not in qlookup]
    if unknown_keys:
        for k in unknown_keys:
            raw_val = answers.pop(k)

            # Unknown UUID + metadata object -> create using provided UUID.
            if _is_uuid(k) and isinstance(raw_val, dict) and (raw_val.get("text") or raw_val.get("question")):
                label = raw_val.get("text") or raw_val.get("question")
                qtype = raw_val.get("type")
                qdef, norm_val = _infer_question_from_value(
                    label, {"type": qtype, "value": raw_val.get("value", raw_val.get("answer"))}
                )
                qdef["id"] = str(k)

                existing_qid = label_to_qid.get(_norm_label(label))
                if existing_qid:
                    answers[existing_qid] = norm_val
                    continue

                crud.add_project_custom_question(db, project_id, section_title="Auto-created", question=qdef)

                # Refresh lookup.
                form = crud.merged_project_form(db, project_id)
                qlookup = crud.question_lookup_from_form(form)
                for qid2, qinfo2 in qlookup.items():
                    if isinstance(qinfo2, dict) and qinfo2.get("text"):
                        label_to_qid[_norm_label(qinfo2.get("text"))] = qid2

                answers[str(k)] = norm_val
                continue

            # Human readable label key -> create / reuse a custom question.
            if not _is_uuid(k):
                label = str(k)
                existing_qid = label_to_qid.get(_norm_label(label))
                if existing_qid:
                    answers[existing_qid] = raw_val
                    continue

                qdef, norm_val = _infer_question_from_value(label, raw_val)
                crud.add_project_custom_question(db, project_id, section_title="Auto-created", question=qdef)

                # Refresh lookup.
                form = crud.merged_project_form(db, project_id)
                qlookup = crud.question_lookup_from_form(form)
                for qid2, qinfo2 in qlookup.items():
                    if isinstance(qinfo2, dict) and qinfo2.get("text"):
                        label_to_qid[_norm_label(qinfo2.get("text"))] = qid2

                answers[qdef["id"]] = norm_val
                continue

            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown question id: {k}. Provide question metadata or use a human-readable label as the key."
                ),
            )

    missing: list[str] = []
    for qid, qinfo in qlookup.items():
        if qinfo.get("required") and (qid not in answers or answers[qid] in ("", None)):
            missing.append(qinfo.get("text"))

    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required answers: {', '.join(missing[:10])}")

    rec = crud.create_record(db, project_id, user.id, answers)
    return RecordSummaryOut(id=rec.id, created_at=fmt_dt(rec.created_at))


@app.get("/api/projects/{project_id}/last_record", response_model=Optional[RecordOut])
def last_record(
    request: Request,
    project_id: int,
    mine: bool = True,
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    """Return the most recent record for a project.

    If mine=true (default), returns the most recent record submitted by the current user.
    If none exists, returns null.
    """
    _set_auth_state_for_rate_limit(request, user.id, request.state.auth_type)

    _require_project_access(
        db,
        user,
        project_id,
        include_closed=user.is_admin,
        on_forbidden_status=404,
        on_forbidden_detail="Project not found",
    )

    rec = crud.latest_record_for_user(db, project_id, user.id) if mine else crud.latest_record_for_project(db, project_id)
    if not rec:
        return None

    form = crud.merged_project_form(db, project_id)
    qlookup = crud.question_lookup_from_form(form)

    answers_out: Dict[str, Any] = {}
    for qid, v in (rec.answers or {}).items():
        qinfo = qlookup.get(qid)
        if qinfo and qinfo.get("type") == "dropdown_mapped" and qinfo.get("value_map"):
            # For last_record used for remember-prefill, keep raw stored value.
            pass
        answers_out[qid] = v

    return RecordOut(
        id=rec.id,
        created_at=fmt_dt(rec.created_at),
        created_by=db.execute(select(User.email).where(User.id == rec.created_by_user_id)).scalar_one_or_none(),
        answers=answers_out,
    )


@app.get("/api/projects/{project_id}/records", response_model=List[RecordSummaryOut])
def list_records(
    request: Request,
    project_id: int,
    include_answers: bool = False,
    keys: Optional[str] = None,
    limit: int = 200,
    db=Depends(get_db),
    user=Depends(get_current_user),
):
    _set_auth_state_for_rate_limit(request, user.id, request.state.auth_type)

    _require_project_access(
        db,
        user,
        project_id,
        include_closed=user.is_admin,
        on_forbidden_status=403,
        on_forbidden_detail="No access to project",
    )

    recs = crud.list_record_summaries(db, project_id, limit=min(max(limit, 1), 500))

    keyset = None
    if keys:
        keyset = {k.strip() for k in keys.split(",") if k.strip()}

    if not include_answers:
        out = [RecordSummaryOut(id=r.id, created_at=fmt_dt(r.created_at)) for r in recs]
        return JSONResponse(content=[o.model_dump() for o in out], headers={"x-returned-items": str(len(out))})

    form = crud.merged_project_form(db, project_id)
    qlookup = crud.question_lookup_from_form(form)

    out: list[dict] = []
    for r in recs:
        ans = r.answers
        if keyset is not None:
            ans = {k: v for k, v in ans.items() if k in keyset}

        out.append(
            {
                "id": r.id,
                "created_at": fmt_dt(r.created_at),
                "updated_at": (fmt_dt(r.updated_at) if getattr(r, "updated_at", None) else None),
                "review_status": getattr(r, "review_status", "pending"),
                "review_comment": getattr(r, "review_comment", None),
                "answers": ans,
                "questions": qlookup,
            }
        )

    return JSONResponse(content=out, headers={"x-returned-items": str(len(out))})


@app.get("/api/records/{record_id}", response_model=RecordOut)
def get_record(request: Request, record_id: int, keys: Optional[str] = None, db=Depends(get_db), user=Depends(get_current_user)):
    _set_auth_state_for_rate_limit(request, user.id, request.state.auth_type)

    rec = crud.get_record(db, record_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Record not found")

    # Access check via project.
    _require_project_access(
        db,
        user,
        rec.project_id,
        include_closed=user.is_admin,
        on_forbidden_status=403,
        on_forbidden_detail="No access to project",
    )

    form = crud.merged_project_form(db, rec.project_id)
    qlookup = crud.question_lookup_from_form(form)

    ans = rec.answers
    if keys:
        keyset = {k.strip() for k in keys.split(",") if k.strip()}
        ans = {k: v for k, v in ans.items() if k in keyset}

    return RecordOut(
        id=rec.id,
        created_at=fmt_dt(rec.created_at),
        updated_at=(fmt_dt(rec.updated_at) if rec.updated_at else None),
        review_status=rec.review_status,
        review_comment=rec.review_comment,
        answers=ans,
        questions=qlookup,
    )


@app.put("/api/records/{record_id}")
def update_record(request: Request, record_id: int, payload: RecordUpdateIn, db=Depends(get_db), user=Depends(get_current_user)):
    _set_auth_state_for_rate_limit(request, user.id, request.state.auth_type)

    rec = crud.get_record(db, record_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Record not found")

    # Only creator or admin can edit.
    if (not user.is_admin) and (rec.created_by_user_id != user.id):
        raise HTTPException(status_code=403, detail="Not allowed to edit this record")

    _require_project_access(
        db,
        user,
        rec.project_id,
        include_closed=user.is_admin,
        on_forbidden_status=403,
        on_forbidden_detail="No access to project",
    )

    crud.update_record(db, record_id, user.id, dict(payload.answers or {}))
    return {"ok": True}


@app.post("/api/records/{record_id}/review")
def review_record(request: Request, record_id: int, payload: ReviewIn, db=Depends(get_db), admin=Depends(require_admin)):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)
    crud.review_record(db, record_id, admin.id, payload.status, payload.comment)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------


@app.post("/api/admin/invites", response_model=InviteOut)
def admin_create_invite(request: Request, payload: InviteCreateIn, db=Depends(get_db), admin=Depends(require_admin)):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)
    key, secret = crud.create_invite(
        db,
        created_by_user_id=admin.id,
        email=(payload.email.lower() if payload.email else None),
    )
    link = f"/register.html?key={key}"
    return InviteOut(link=link, secret=secret)


@app.get("/api/admin/users", response_model=List[UserOut])
def admin_list_users(request: Request, db=Depends(get_db), admin=Depends(require_admin)):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)

    users = crud.list_users_with_access(db)
    out: list[UserOut] = []
    for u, assigned, banned in users:
        out.append(UserOut(id=u.id, email=u.email, is_admin=u.is_admin, assigned_project_ids=assigned, banned_project_ids=banned))

    return JSONResponse(content=[o.model_dump() for o in out], headers={"x-returned-items": str(len(out))})


@app.patch("/api/admin/users/{user_id}")
def admin_update_user(request: Request, user_id: int, payload: UserUpdateIn, db=Depends(get_db), admin=Depends(require_admin)):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)

    u = db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    if payload.is_admin is not None:
        u.is_admin = bool(payload.is_admin)

    db.commit()
    return {"ok": True}


@app.put("/api/admin/users/{user_id}/projects")
def admin_set_user_projects(
    request: Request,
    user_id: int,
    payload: UserProjectsUpdateIn,
    db=Depends(get_db),
    admin=Depends(require_admin),
):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)

    u = db.get(User, user_id)
    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    crud.set_user_projects(db, user_id, payload.assigned_project_ids, payload.banned_project_ids)
    return {"ok": True}


@app.get("/api/admin/question_sets", response_model=List[QuestionSetOut])
def admin_list_question_sets(request: Request, db=Depends(get_db), admin=Depends(require_admin)):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)

    qsets = crud.list_question_sets(db)
    out: list[QuestionSetOut] = []

    for qs in qsets:
        creator = None
        if qs.created_by_user_id:
            u = db.get(User, qs.created_by_user_id)
            creator = u.email if u else None

        out.append(
            QuestionSetOut(
                id=qs.id,
                name=qs.name,
                created_at=qs.created_at.isoformat(timespec="seconds") + "Z",
                created_by=creator,
                data=qs.data,
            )
        )

    return JSONResponse(content=[o.model_dump() for o in out], headers={"x-returned-items": str(len(out))})


@app.post("/api/admin/question_sets")
def admin_create_question_set(request: Request, payload: QuestionSetCreateIn, db=Depends(get_db), admin=Depends(require_admin)):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)
    qs = crud.create_question_set(db, payload.name, payload.data, admin.id)
    return {"ok": True, "id": qs.id}


@app.put("/api/admin/question_sets/{question_set_id}")
def admin_update_question_set(
    request: Request,
    question_set_id: int,
    payload: QuestionSetCreateIn,
    db=Depends(get_db),
    admin=Depends(require_admin),
):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)

    existing = db.get(QuestionSet, question_set_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Question set not found")

    qs = crud.create_question_set(db, existing.name, payload.data, admin.id)
    return {"ok": True, "new_id": qs.id}


@app.post("/api/admin/projects", response_model=ProjectOut)
def admin_create_project(request: Request, payload: ProjectCreateIn, db=Depends(get_db), admin=Depends(require_admin)):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)
    p = crud.create_project(db, payload.name.strip(), payload.focalpoint_code)
    return ProjectOut(id=p.id, name=p.name, closed=p.closed, focalpoint_code=p.focalpoint_code)


@app.delete("/api/admin/projects/{project_id}")
def admin_delete_project(request: Request, project_id: int, db=Depends(get_db), admin=Depends(require_admin)):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)

    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")

    p.deleted_at = datetime.utcnow()
    p.closed = True
    db.add(p)
    db.commit()
    return {"ok": True}


@app.put("/api/admin/projects/import")
def admin_import_projects(request: Request, payload: ProjectImportIn, db=Depends(get_db), admin=Depends(require_admin)):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)
    added = crud.import_projects(db, payload.projects)
    return {"ok": True, "added": added}


@app.patch("/api/admin/projects/{project_id}")
def admin_update_project(request: Request, project_id: int, payload: ProjectUpdateIn, db=Depends(get_db), admin=Depends(require_admin)):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)

    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")

    if payload.closed is not None:
        p.closed = bool(payload.closed)

    db.commit()
    return {"ok": True}


@app.get("/api/admin/projects", response_model=List[ProjectOut])
def admin_list_projects(request: Request, include_deleted: bool = False, db=Depends(get_db), admin=Depends(require_admin)):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)

    q = select(Project).order_by(Project.name)
    if not include_deleted:
        q = q.where(Project.deleted_at.is_(None))

    ps = db.execute(q).scalars().all()
    out: list[ProjectOut] = []
    for p in ps:
        out.append(
            ProjectOut(
                id=p.id,
                name=p.name,
                closed=p.closed,
                focalpoint_code=getattr(p, "focalpoint_code", None),
                deleted_at=(p.deleted_at.isoformat(timespec="seconds") + "Z") if getattr(p, "deleted_at", None) else None,
            )
        )

    return JSONResponse(content=[o.model_dump() for o in out], headers={"x-returned-items": str(len(out))})


@app.delete("/api/admin/question_sets/{question_set_id}")
def admin_delete_question_set(request: Request, question_set_id: int, db=Depends(get_db), admin=Depends(require_admin)):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)
    crud.delete_question_set(db, question_set_id)
    return {"ok": True}


@app.post("/api/admin/projects/question_sets_batch")
def admin_project_question_sets_batch(payload: ProjectQuestionSetsBatchIn, user: User = Depends(require_admin), db=Depends(get_db)):
    ids = [int(x) for x in payload.project_ids if int(x) > 0]
    if not ids:
        return {}

    rows = db.execute(
        select(ProjectQuestionSet.project_id, ProjectQuestionSet.question_set_id)
        .where(ProjectQuestionSet.project_id.in_(ids))
        .order_by(ProjectQuestionSet.project_id, ProjectQuestionSet.position)
    ).all()

    out: dict[int, list[int]] = {}
    for pid, qsid in rows:
        out.setdefault(pid, []).append(qsid)

    return out


@app.get("/api/admin/projects/{project_id}/question_sets")
def admin_get_project_question_sets(request: Request, project_id: int, db=Depends(get_db), admin=Depends(require_admin)):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)

    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")

    links = db.execute(
        select(ProjectQuestionSet)
        .where(ProjectQuestionSet.project_id == project_id)
        .order_by(ProjectQuestionSet.position)
    ).scalars().all()

    data: list[dict] = []
    for link in links:
        qs = db.get(QuestionSet, link.question_set_id)
        if qs:
            data.append(
                {
                    "question_set_id": qs.id,
                    "name": qs.name,
                    "created_at": qs.created_at.isoformat(timespec="seconds") + "Z",
                }
            )

    return data


@app.put("/api/admin/projects/{project_id}/question_sets")
def admin_set_project_question_sets(
    request: Request,
    project_id: int,
    payload: ProjectAssignQuestionSetsIn,
    db=Depends(get_db),
    admin=Depends(require_admin),
):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)

    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")

    crud.assign_project_question_sets(db, project_id, payload.question_set_ids)
    return {"ok": True}


@app.get("/api/admin/projects/{project_id}/custom_questions")
def admin_get_custom_questions(request: Request, project_id: int, db=Depends(get_db), admin=Depends(require_admin)):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)

    p = db.get(Project, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")

    cq = crud.canonical_project_custom_questions(p.custom_questions)

    # Persist generated IDs if missing in stored JSON so Edit/Delete works reliably.
    if p.custom_questions != cq:
        p.custom_questions = cq
        db.add(p)
        db.commit()

    return cq


@app.post("/api/admin/projects/{project_id}/custom_questions")
def admin_add_custom_question(
    request: Request,
    project_id: int,
    payload: ProjectAddCustomQuestionIn,
    db=Depends(get_db),
    admin=Depends(require_admin),
):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)
    crud.add_project_custom_question(db, project_id, payload.section_title, payload.question)
    return {"ok": True}


@app.put("/api/admin/projects/{project_id}/custom_questions/{question_id}")
def admin_update_custom_question(
    request: Request,
    project_id: int,
    question_id: str,
    payload: ProjectAddCustomQuestionIn,
    db=Depends(get_db),
    admin=Depends(require_admin),
):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)
    crud.update_project_custom_question(db, project_id, question_id, payload.section_title, payload.question)
    return {"ok": True}


@app.delete("/api/admin/projects/{project_id}/custom_questions/{question_id}")
def admin_delete_custom_question(request: Request, project_id: int, question_id: str, db=Depends(get_db), admin=Depends(require_admin)):
    _set_auth_state_for_rate_limit(request, admin.id, request.state.auth_type)
    crud.delete_project_custom_question(db, project_id, question_id)
    return {"ok": True}
