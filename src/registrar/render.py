"""Plain text and JSON rendering."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Protocol


class ToDict(Protocol):
    def to_dict(self) -> dict[str, Any]: ...


def render_json(items: Iterable[ToDict] | ToDict) -> str:
    if hasattr(items, "to_dict"):
        payload: Any = items.to_dict()
    else:
        payload = [item.to_dict() for item in items]
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


MAX_CELL_WIDTH = 88


def table(rows: list[dict[str, str]], columns: list[str]) -> str:
    if not rows:
        return "OK: no rows"
    clipped = [
        {column: _clip(row.get(column, "")) for column in columns} for row in rows
    ]
    widths = {
        column: max(len(column), *(len(row.get(column, "")) for row in clipped))
        for column in columns
    }
    header = "  ".join(column.ljust(widths[column]) for column in columns)
    sep = "  ".join("-" * widths[column] for column in columns)
    body = [
        "  ".join(row.get(column, "").ljust(widths[column]) for column in columns)
        for row in clipped
    ]
    return "\n".join([header, sep, *body])


def _clip(value: str) -> str:
    if len(value) <= MAX_CELL_WIDTH:
        return value
    return f"{value[: MAX_CELL_WIDTH - 3]}..."
