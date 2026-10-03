"""
Two ways to walk a list, one per dialect.

**Anthropic** hands out opaque cursors — `next_page` and `prev_page` — and a cursor
encodes the `order` of the request that produced it. Reusing one with a different
order is a 400, because a position in an ascending walk means nothing in a
descending one.

**OpenAI** uses the id of the last item seen: `after=<id>`, with `order` and `limit`
as ordinary parameters, and `first_id` / `last_id` / `has_more` in the response.

The cursor is not signed, and that is a decision rather than an omission. It holds a
position and an order and nothing else — the tenant comes from the credential on the
request, never from the cursor — so a forged cursor can only move a caller around
inside data it may already read. Signing would need a server secret to protect
nothing.
"""

from __future__ import annotations

import base64
import json
from typing import Callable


class BadCursor(ValueError):
    """Malformed, or produced under a different order."""


def _encode(position: str, order: str) -> str:
    raw = json.dumps({"p": position, "o": order}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return str(data["p"]), str(data["o"])
    except Exception as exc:  # noqa: BLE001 — any malformation is the same answer
        raise BadCursor("the page cursor is not one this service issued") from exc


def _order(items: list[dict], key: Callable[[dict], str], order: str) -> list[dict]:
    if order not in ("asc", "desc"):
        raise BadCursor(f"order must be asc or desc, not {order!r}")
    return sorted(items, key=key, reverse=(order == "desc"))


def anthropic(items: list[dict], *, key: Callable[[dict], str], limit: int,
              order: str = "asc", page: str | None = None) -> dict:
    """
    A page, and the cursors either side of it.

    Every cursor points at the **first** item of the page it opens, so `next_page`
    and `prev_page` are the same kind of thing and decode the same way. A cursor
    whose item has since been deleted still lands correctly: it opens at the first
    key at or beyond the one it names.
    """
    ordered = _order(items, key, order)
    keys = [key(i) for i in ordered]
    start = 0
    if page:
        position, cursor_order = _decode(page)
        if cursor_order != order:
            raise BadCursor(
                f"this cursor was produced by a listing in {cursor_order} order and "
                f"cannot continue one in {order} order")
        start = next((n for n, k in enumerate(keys)
                      if (k >= position if order == "asc" else k <= position)),
                     len(keys))

    chunk = ordered[start:start + limit]
    end = start + len(chunk)
    return {
        "data": chunk,
        "next_page": _encode(keys[end], order) if end < len(keys) else None,
        "prev_page": _encode(keys[max(0, start - limit)], order) if start > 0 else None,
    }


def openai(items: list[dict], *, key: Callable[[dict], str], id_of: Callable[[dict], str],
           limit: int, order: str = "desc", after: str | None = None) -> dict:
    """
    A page by `after`, and `first_id` / `last_id` / `has_more` to continue it.
    """
    ordered = _order(items, key, order)
    start = 0
    if after:
        ids = [id_of(i) for i in ordered]
        if after not in ids:
            raise BadCursor(f"after={after!r} is not an item in this list")
        start = ids.index(after) + 1
    chunk = ordered[start:start + limit]
    return {
        "data": chunk,
        "first_id": id_of(chunk[0]) if chunk else None,
        "last_id": id_of(chunk[-1]) if chunk else None,
        "has_more": start + len(chunk) < len(ordered),
    }
