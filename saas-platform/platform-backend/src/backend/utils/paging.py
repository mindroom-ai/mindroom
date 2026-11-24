"""Paging past PostgREST's per-request row limit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

# PostgREST returns at most this many rows per request.
_PAGE_SIZE = 1000


def select_all_pages(build_query: Callable[[], Any]) -> list[dict[str, Any]]:
    """Return every row of a query, one page at a time.

    ``build_query`` returns a fresh filtered and ordered query on each call; the order keeps pages from overlapping.
    Paging stops only at an empty page, because a short page does not prove the server has no more rows.
    """
    rows: list[dict[str, Any]] = []
    while True:
        page = build_query().range(len(rows), len(rows) + _PAGE_SIZE - 1).execute().data or []
        if not page:
            return rows
        rows.extend(page)
