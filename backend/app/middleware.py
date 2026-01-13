# ---------------------------------------------------------------------------
# middleware.py
#
# FastAPI / Starlette middleware used by the API service.
#
# Included middleware:
# - BodySizeLimitMiddleware: rejects requests exceeding MAX_BODY_BYTES based on
#   Content-Length (best-effort; streaming bodies cannot be measured reliably).
# - RateLimitMiddleware: in-memory sliding window limiter keyed by auth token
#   hash (session cookie or bearer token). Suitable for a single instance.
# - ApiLoggingMiddleware: persists a lightweight access log row per request.
#
# Middleware is designed to be safe: failures in logging/rate limiting should
# not crash the request path.
# ---------------------------------------------------------------------------

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Callable, Deque, DefaultDict, Optional

from fastapi import Request, Response
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .config import MAX_BODY_BYTES, RATE_LIMIT_RPM, SESSION_COOKIE_NAME
from .db import SessionLocal
from .models import ApiLog
from .utils import sha256_hex


def _client_ip(request: Request) -> str:
    """Best-effort client IP (prefers the first hop in X-Forwarded-For)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests above MAX_BODY_BYTES based on Content-Length header."""

    async def dispatch(self, request: Request, call_next: Callable):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_BODY_BYTES:
                    return JSONResponse({"detail": "Request too large"}, status_code=413)
            except ValueError:
                # If Content-Length isn't an int, ignore and let downstream handle.
                pass
        return await call_next(request)


class _SlidingWindowLimiter:
    """A simple in-memory sliding window limiter (requests per minute)."""

    def __init__(self) -> None:
        self._hits: DefaultDict[str, Deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit_per_minute: int) -> bool:
        now = time.time()
        window_seconds = 60.0
        q = self._hits[key]

        # Drop timestamps outside the window.
        while q and (now - q[0]) > window_seconds:
            q.popleft()

        if len(q) >= limit_per_minute:
            return False

        q.append(now)
        return True


class RateLimitMiddleware(BaseHTTPMiddleware):
    """In-memory rate limiter keyed by auth token hash.

    Notes:
    - This works well for a single backend instance.
    - For multi-instance deployments, move rate limiting to a shared store
      (e.g., Redis) or a gateway/WAF.
    """

    def __init__(self, app):
        super().__init__(app)
        self._limiter = _SlidingWindowLimiter()

    def _rate_key(self, request: Request) -> Optional[str]:
        # Session cookie
        cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
        if cookie_token:
            return f"sess:{sha256_hex(cookie_token)}"

        # Bearer token header
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
            if token:
                return f"api:{sha256_hex(token)}"

        return None

    async def dispatch(self, request: Request, call_next: Callable):
        key = self._rate_key(request)
        if key is not None:
            if not self._limiter.allow(key, RATE_LIMIT_RPM):
                return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)

        return await call_next(request)


class ApiLoggingMiddleware(BaseHTTPMiddleware):
    """Persist a lightweight API log row for each request."""

    async def dispatch(self, request: Request, call_next: Callable):
        start = time.time()
        response: Response = await call_next(request)
        duration = time.time() - start

        try:
            ip = _client_ip(request)
            user_agent = request.headers.get("user-agent", "")[:2000]

            auth_type = getattr(request.state, "auth_type", "none") or "none"
            user_id = getattr(request.state, "user_id", None)

            # Estimate response size (best-effort).
            content_len = response.headers.get("content-length")
            response_bytes = int(content_len) if content_len and content_len.isdigit() else 0

            returned_items = 0
            ri = response.headers.get("x-returned-items")
            if ri and ri.isdigit():
                returned_items = int(ri)

            db: Session = SessionLocal()
            try:
                db.add(
                    ApiLog(
                        user_id=user_id,
                        auth_type=auth_type,
                        method=request.method,
                        path=str(request.url.path),
                        status_code=response.status_code,
                        ip=ip,
                        user_agent=user_agent,
                        returned_items=returned_items,
                        response_bytes=response_bytes,
                    )
                )
                db.commit()
            finally:
                db.close()
        except Exception:
            # Logging must never break the response path.
            pass

        response.headers["x-server-timing-ms"] = str(int(duration * 1000))
        return response
