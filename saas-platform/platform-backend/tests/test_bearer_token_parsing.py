"""Unit tests for bearer token extraction."""

from __future__ import annotations

import pytest
from backend.deps import _extract_bearer_token
from fastapi import HTTPException


def test_extract_bearer_token_ok() -> None:
    """Parses valid Authorization header with Bearer scheme."""
    assert _extract_bearer_token("Bearer abc123") == "abc123"
    # Case-insensitive scheme
    assert _extract_bearer_token("bearer tokenXYZ") == "tokenXYZ"


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Basic abc",
        "Bearer",
        "Bearer a b",
        "Token abc",
    ],
)
def test_extract_bearer_token_errors(header: str | None) -> None:
    """Rejects missing/invalid/incorrect scheme headers with 401."""
    with pytest.raises(HTTPException) as ei:
        _extract_bearer_token(header)  # type: ignore[arg-type]
    assert ei.value.status_code == 401
