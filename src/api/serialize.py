from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import inspect


def row(obj: Any, exclude: set[str] | None = None) -> dict:
    if obj is None:
        return {}
    out = {}
    for c in inspect(obj).mapper.column_attrs:
        if exclude and c.key in exclude:
            continue
        v = getattr(obj, c.key)
        out[c.key] = v.isoformat() if isinstance(v, datetime) else v
    return out


SECRET_COLS = {"token_hash"}


def rows(objs, exclude: set[str] | None = None) -> list[dict]:
    return [row(o, (exclude or set()) | SECRET_COLS) for o in objs]
