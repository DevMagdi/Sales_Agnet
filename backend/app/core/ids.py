"""UUID helpers."""

from __future__ import annotations

import uuid


def new_id() -> str:
    return str(uuid.uuid4())


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False
