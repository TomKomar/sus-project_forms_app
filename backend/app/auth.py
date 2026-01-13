# ---------------------------------------------------------------------------
# auth.py
#
# Authentication and authorization helpers.
#
# This module implements:
# - Database session dependency (`get_db`)
# - Password hashing/verification (Argon2 via passlib)
# - Session issuance/revocation stored server-side (cookie token hashed in DB)
# - Optional API token authentication (Bearer <token> header, SHA-256 hashed in DB)
# - Admin-only guard (`require_admin`)
#
# Request fingerprinting (IP + User-Agent) is used to bind sessions to a client.
# For proxied deployments, the first hop of X-Forwarded-For is preferred.
# ---------------------------------------------------------------------------

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Generator, Optional, Tuple

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import SESSION_COOKIE_NAME, SESSION_TTL_HOURS
from .db import SessionLocal
from .models import Session as DbSession
from .models import User
from .utils import new_key, sha256_hex

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")
bearer = HTTPBearer(auto_error=False)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a DB session and guarantees close."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def hash_password(password: str) -> str:
    """Hash a plaintext password using Argon2."""
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against an Argon2 hash."""
    return pwd_context.verify(password, password_hash)


def get_request_fingerprint(request: Request) -> Tuple[str, str]:
    """Return a (ip, user_agent) fingerprint for the request."""
    xff = request.headers.get("x-forwarded-for", "")
    ip = xff.split(",", 1)[0].strip() if xff else (request.client.host if request.client else "unknown")
    user_agent = request.headers.get("user-agent", "")
    return ip, user_agent


def issue_session(user: User, ip: str, user_agent: str, db: Session) -> str:
    """Create a server-side session and return the *plaintext* token for the cookie."""
    token = new_key(32)
    now = datetime.utcnow()

    db_sess = DbSession(
        user_id=user.id,
        token_hash=sha256_hex(token),
        ip=ip,
        user_agent=user_agent[:2000],
        expires_at=now + timedelta(hours=SESSION_TTL_HOURS),
    )
    db.add(db_sess)
    db.commit()
    return token


def revoke_session(token: str, db: Session) -> None:
    """Revoke a session token (idempotent)."""
    token_hash = sha256_hex(token)
    sess = db.execute(select(DbSession).where(DbSession.token_hash == token_hash)).scalar_one_or_none()
    if sess and sess.revoked_at is None:
        sess.revoked_at = datetime.utcnow()
        db.commit()


def cleanup_expired_sessions(db: Session) -> None:
    """Delete expired sessions from the database."""
    now = datetime.utcnow()
    db.execute(delete(DbSession).where(DbSession.expires_at < now))
    db.commit()


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> User:
    """Authenticate the request using either a session cookie or a bearer token."""
    ip, user_agent = get_request_fingerprint(request)

    # 1) Session cookie authentication.
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_token:
        token_hash = sha256_hex(cookie_token)
        sess = db.execute(select(DbSession).where(DbSession.token_hash == token_hash)).scalar_one_or_none()

        if not sess or sess.revoked_at is not None or sess.expires_at < datetime.utcnow():
            raise HTTPException(status_code=401, detail="Session expired")

        # Bind session to (IP, UA) fingerprint; revoke on mismatch.
        if sess.ip != ip or sess.user_agent != user_agent:
            sess.revoked_at = datetime.utcnow()
            db.commit()
            raise HTTPException(status_code=401, detail="Session fingerprint mismatch")

        user = db.get(User, sess.user_id)
        if not user:
            raise HTTPException(status_code=401, detail="Unknown user")

        request.state.auth_type = "session"
        request.state.user_id = user.id
        return user

    # 2) API token authentication (Bearer token).
    if creds and creds.scheme.lower() == "bearer":
        token = creds.credentials
        token_hash = sha256_hex(token)
        user = db.execute(select(User).where(User.api_token_hash == token_hash)).scalar_one_or_none()
        if user:
            request.state.auth_type = "api_token"
            request.state.user_id = user.id
            return user

    raise HTTPException(status_code=401, detail="Not authenticated")


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Guard that only allows admin users."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return user
