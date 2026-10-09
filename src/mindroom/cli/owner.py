"""Owner Matrix user ID helpers for CLI onboarding."""

from __future__ import annotations

from mindroom.constants import OWNER_MATRIX_USER_ID_PLACEHOLDER
from mindroom.matrix.identity import parse_current_matrix_user_id

_LEGACY_OWNER_MATRIX_USER_ID_PLACEHOLDER = "__PLACEHOLDER__"

# LEGACY_COMPAT: Generic owner placeholder in authored configuration.
# Legacy format: Unversioned authored config may contain the generic __PLACEHOLDER__ owner token.
# Last legacy release: no tagged native writer; v2026.2.163 introduced both accepted owner placeholders.
# Handling: Replace either token with the same validated, YAML-quoted Matrix user ID.
# Coverage: tests/test_cli_connect.py::test_replace_owner_placeholders_in_config_accepts_server_port.


def parse_owner_matrix_user_id(raw_value: object) -> str | None:
    """Parse an optional owner Matrix user ID in the current user ID grammar."""
    if not isinstance(raw_value, str):
        return None
    try:
        return parse_current_matrix_user_id(raw_value.strip())
    except ValueError:
        return None


def replace_owner_placeholders_in_text(content: str, owner_user_id: str) -> str:
    """Return config text with owner placeholders replaced by a quoted Matrix user ID."""
    if parse_owner_matrix_user_id(owner_user_id) is None:
        return content
    # Quote the Matrix user ID so the leading '@' doesn't break YAML parsing
    # (@ starts a YAML tag/anchor when unquoted). The current grammar admits no
    # quote, backslash, or whitespace, so the ID cannot end the scalar early.
    quoted = f'"{owner_user_id}"'
    return content.replace(OWNER_MATRIX_USER_ID_PLACEHOLDER, quoted).replace(
        _LEGACY_OWNER_MATRIX_USER_ID_PLACEHOLDER,
        quoted,
    )
