# ---------------------------------------------------------------------------
# config.py
#
# Application configuration.
#
# This module centralizes all environment-driven settings used by the API.
# In a shared DevOps environment, keeping configuration in one place prevents
# "magic numbers" scattered throughout the codebase and makes deployments
# predictable.
#
# Values are read from environment variables with safe defaults appropriate for
# local development. Types are normalized (e.g., boolean and integer parsing).
# ---------------------------------------------------------------------------

from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable with a safe fallback."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable.

    Truthy values: 1, true, yes, y, on (case-insensitive).
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


# Database connection string (SQLAlchemy URL).
DATABASE_URL: str = os.getenv(
    "DATABASE_URL", "postgresql+psycopg2://app:app@localhost:5432/app"
)

# Environment name (informational).
APP_ENV: str = os.getenv("APP_ENV", "local")

# Mark cookies as Secure (HTTPS-only).
COOKIE_SECURE: bool = _env_bool("COOKIE_SECURE", default=False)

# 1 MiB request body limit (often also enforced at the reverse proxy).
MAX_BODY_BYTES: int = _env_int("MAX_BODY_BYTES", 1 * 1024 * 1024)

# Session cookie configuration.
SESSION_COOKIE_NAME: str = os.getenv("SESSION_COOKIE_NAME", "session_token")
SESSION_TTL_HOURS: int = _env_int("SESSION_TTL_HOURS", 48)

# In-memory request rate limiting (requests per minute) keyed by auth token.
RATE_LIMIT_RPM: int = _env_int("RATE_LIMIT_RPM", 600)
