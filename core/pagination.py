from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db.models import Q, QuerySet
from django.utils.dateparse import parse_datetime
from rest_framework import serializers


@dataclass(frozen=True)
class CursorPage:
    items: list[Any]
    next_cursor: str | None
    has_more: bool


def encode_cursor(sort_value: Any, pk: int) -> str:
    if isinstance(sort_value, datetime):
        sort_value = sort_value.isoformat()
    payload = json.dumps({"v": sort_value, "id": pk}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_cursor(cursor: str | None) -> tuple[Any, int] | None:
    if not cursor:
        return None
    try:
        payload = base64.urlsafe_b64decode(cursor.encode("ascii"))
        data = json.loads(payload.decode("utf-8"))
        value = data["v"]
        pk = int(data["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise serializers.ValidationError({"cursor": "Invalid cursor."}) from exc

    parsed_dt = parse_datetime(value) if isinstance(value, str) else None
    return parsed_dt or value, pk


def paginate_keyset(
    queryset: QuerySet,
    *,
    limit: int,
    cursor: str | None = None,
    sort_field: str = "created_at",
    descending: bool = True,
) -> CursorPage:
    decoded = decode_cursor(cursor)
    if decoded is not None:
        sort_value, pk = decoded
        if descending:
            queryset = queryset.filter(
                Q(**{f"{sort_field}__lt": sort_value})
                | (Q(**{sort_field: sort_value}) & Q(id__lt=pk))
            )
        else:
            queryset = queryset.filter(
                Q(**{f"{sort_field}__gt": sort_value})
                | (Q(**{sort_field: sort_value}) & Q(id__gt=pk))
            )

    prefix = "-" if descending else ""
    rows = list(queryset.order_by(f"{prefix}{sort_field}", f"{prefix}id")[: limit + 1])
    has_more = len(rows) > limit
    items = rows[:limit]

    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(getattr(last, sort_field), last.id)

    return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)
