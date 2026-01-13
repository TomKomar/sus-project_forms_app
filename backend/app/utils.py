# ---------------------------------------------------------------------------
# utils.py
#
# Shared utility helpers.
#
# This module contains small, dependency-light helpers used across the API.
# Keeping this logic centralized reduces duplication and avoids subtle
# inconsistencies between endpoints.
#
# Responsibilities:
# - Cryptographic helpers (SHA-256 hashing, secure random tokens)
# - Canonicalization of question-set JSON payloads
# - Small parsing/formatting helpers (e.g., datetime formatting)
# - Auto-question inference helpers for automation/seeding clients
# ---------------------------------------------------------------------------

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime
from typing import Any, Tuple


# ---------------------------------------------------------------------------
# Crypto helpers
# ---------------------------------------------------------------------------


def sha256_hex(value: str) -> str:
    """Return a SHA-256 hex digest of a UTF-8 string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_key(nbytes: int = 24) -> str:
    """Generate a URL-safe random string (suitable for secrets/tokens)."""
    return secrets.token_urlsafe(nbytes)


def is_uuid(value: str) -> bool:
    """Return True if the given value parses as a UUID."""
    try:
        uuid.UUID(str(value))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Question-set canonicalization
# ---------------------------------------------------------------------------


def _split_nonempty_lines(text: str) -> list[str]:
    return [line.strip() for line in str(text).splitlines() if line.strip()]


def normalize_dropdown_options(question: dict) -> dict:
    """Normalize dropdown options/value_map formats in a question dict.

    - Ensures dropdown `options` is a list of non-empty strings.
    - Ensures dropdown_mapped `value_map` is a dict (empty dict if missing).
    """
    q = dict(question)
    qtype = q.get("type")

    if qtype in {"dropdown", "dropdown_mapped"}:
        opts = q.get("options")
        if isinstance(opts, str):
            q["options"] = _split_nonempty_lines(opts)
        elif isinstance(opts, list):
            # If it's a list of single-character strings, treat it as an expanded string.
            if opts and all(isinstance(x, str) and len(x) <= 1 for x in opts):
                q["options"] = _split_nonempty_lines("".join(opts))
            else:
                q["options"] = [str(x).strip() for x in opts if str(x).strip()]

    if qtype == "dropdown_mapped":
        value_map = q.get("value_map")
        if value_map is None:
            q["value_map"] = {}
        elif isinstance(value_map, dict):
            q["value_map"] = value_map
        elif isinstance(value_map, str):
            try:
                parsed = json.loads(value_map)
                q["value_map"] = parsed if isinstance(parsed, dict) else {}
            except Exception:
                q["value_map"] = {}
        else:
            q["value_map"] = {}

    return q


def ensure_question_ids(canonical: dict) -> dict:
    """Ensure every question object in a canonical payload has an `id`."""
    canonical = dict(canonical)
    sections = canonical.get("sections", [])
    new_sections: list[dict] = []

    for sec in sections:
        sec = dict(sec)
        questions: list[dict] = []
        for q in sec.get("questions", []):
            q = normalize_dropdown_options(dict(q))
            q.setdefault("id", str(uuid.uuid4()))
            questions.append(q)

        sec["questions"] = questions
        new_sections.append(sec)

    canonical["sections"] = new_sections
    canonical.setdefault("title", canonical.get("name") or "Untitled")
    return canonical


def to_canonical_question_set(raw: Any, fallback_name: str = "Imported") -> dict:
    """Convert an incoming question-set payload into canonical form.

    Supported shapes:
    1) Canonical: {"title": ..., "sections": [...]}
    2) Legacy nested dict: {Title: {section_title: [ ...questions... ]}}
    """
    if isinstance(raw, dict) and "sections" in raw and "title" in raw:
        return ensure_question_ids(raw)

    # Legacy-ish: {Title: {section_title: [{question_text: {type, required}}, ...], ...}}
    if isinstance(raw, dict) and len(raw) == 1:
        title = next(iter(raw.keys()))
        inner = raw[title]
        sections: list[dict] = []

        if isinstance(inner, dict):
            for section_title, items in inner.items():
                questions: list[dict] = []
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict) and len(item) == 1:
                            q_text = next(iter(item.keys()))
                            meta = item[q_text]
                            meta = meta if isinstance(meta, dict) else {}
                            qobj: dict = {
                                "text": str(q_text),
                                "type": str(meta.get("type", "short_text")),
                                "required": bool(meta.get("required", True)),
                            }
                            options = meta.get("options")
                            if isinstance(options, list):
                                qobj["options"] = [str(x) for x in options]
                            value_map = meta.get("value_map") or meta.get("map")
                            if isinstance(value_map, dict):
                                qobj["value_map"] = {str(k): value_map[k] for k in value_map}
                            questions.append(qobj)
                            continue

                        # Alternative legacy shape: {"question": "...", "type": "...", ...}
                        if isinstance(item, dict) and "question" in item:
                            qobj = {
                                "text": str(item.get("question")),
                                "type": str(item.get("type", "short_text")),
                                "required": bool(item.get("required", True)),
                            }
                            if isinstance(item.get("options"), list):
                                qobj["options"] = [str(x) for x in item.get("options")]
                            value_map = item.get("value_map") or item.get("map")
                            if isinstance(value_map, dict):
                                qobj["value_map"] = {str(k): value_map[k] for k in value_map}
                            questions.append(qobj)

                sections.append({"title": str(section_title), "questions": questions})

        canonical = {"title": str(title), "sections": sections}
        return ensure_question_ids(canonical)

    # Unknown shape: wrap in an empty canonical container.
    return ensure_question_ids(
        {"title": fallback_name, "sections": [{"title": "Section", "questions": []}]}
    )


def canonical_project_custom_questions(raw: Any) -> dict:
    """Ensure project custom questions are stored in canonical format."""
    if isinstance(raw, dict) and "sections" in raw and "title" in raw:
        return ensure_question_ids(raw)
    return ensure_question_ids({"title": "Custom", "sections": []})


# ---------------------------------------------------------------------------
# Small parsing/formatting helpers
# ---------------------------------------------------------------------------


def parse_numeric(value: Any) -> float | None:
    """Best-effort numeric coercion; return None if not numeric."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def fmt_dt(dt: datetime) -> str:
    """Format datetimes consistently for UI consumption."""
    return dt.strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Auto-question helpers (used by /projects/{id}/records)
# ---------------------------------------------------------------------------


def normalize_label(label: str) -> str:
    """Normalize a human label for stable comparisons."""
    return " ".join(str(label).strip().split()).casefold()


def normalize_yes_no(value: Any) -> Any:
    """Normalize common boolean-like values to 'yes'/'no'."""
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if isinstance(value, str):
        vv = value.strip().lower()
        if vv in {"true", "t", "1", "yes", "y"}:
            return "yes"
        if vv in {"false", "f", "0", "no", "n"}:
            return "no"
    if value in (0, 1):
        return "yes" if value == 1 else "no"
    return value


def infer_question_from_value(label: str, raw: Any) -> Tuple[dict, Any]:
    """Infer a canonical question definition from a posted value.

    Supports:
    1) scalar values (string/number/bool) -> type inferred heuristically
    2) structured dict with metadata: {type, value/answer, options, value_map}

    Returns:
        (question_definition, normalized_value)
    """
    question: dict = {
        "id": str(uuid.uuid4()),
        "text": str(label).strip(),
        "required": False,
    }

    # Structured input shape:
    if isinstance(raw, dict):
        meta = raw
        qtype = meta.get("type") or meta.get("qtype")
        value = meta.get("value") if "value" in meta else meta.get("answer", meta)
        if qtype:
            question["type"] = str(qtype)
        if isinstance(meta.get("options"), list):
            question["options"] = meta.get("options")
        if isinstance(meta.get("value_map"), dict):
            question["value_map"] = meta.get("value_map")
        raw = value

    # Type inference:
    if "type" not in question:
        if isinstance(raw, bool):
            question["type"] = "yes_no"
            raw = normalize_yes_no(raw)
        elif isinstance(raw, (int, float)):
            question["type"] = "numeric"
        elif isinstance(raw, str) and len(raw.strip()) == 10 and raw.strip()[4] == "-":
            question["type"] = "date"
        else:
            question["type"] = "long_text" if isinstance(raw, str) and len(raw) > 80 else "short_text"

    if question.get("type") == "yes_no":
        raw = normalize_yes_no(raw)

    return question, raw
