"""Compatibility normalization for persisted Matrix state."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.matrix.state import MatrixState


def normalize_legacy_matrix_state(
    state: MatrixState,
    raw_data: object,
    *,
    current_domain: str,
) -> dict[str, object] | None:
    """Backfill historical account domains and return a rewrite only when needed."""
    for account in state.accounts.values():
        if account.domain is None:
            account.domain = current_domain

    normalized_data = state.model_dump(mode="json")
    return normalized_data if raw_data != normalized_data else None
