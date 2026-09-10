"""Owner Matrix user ID helpers for CLI onboarding."""

from __future__ import annotations

import re

from mindroom.constants import OWNER_MATRIX_USER_ID_PLACEHOLDER

_LEGACY_OWNER_MATRIX_USER_ID_PLACEHOLDER = "__PLACEHOLDER__"
_OWNER_MATRIX_USER_ID_RE = re.compile(r"^@[^:\s]+:[^\s]+$")

# Legacy format: Unversioned authored config may contain the generic __PLACEHOLDER__ owner token.
# Last legacy release: no tagged native writer; v2026.2.163 introduced both accepted owner placeholders.
# Handling: Replace either token with the same validated, YAML-quoted Matrix user ID.
# Coverage: tests/test_cli_connect.py::test_replace_owner_placeholders_in_config_accepts_server_port.


def parse_owner_matrix_user_id(raw_value: object) -> str | None:
    """Parse an optional owner Matrix user ID."""
    if not isinstance(raw_value, str):
        return None
    candidate_owner_user_id = raw_value.strip()
    if _OWNER_MATRIX_USER_ID_RE.fullmatch(candidate_owner_user_id):
        return candidate_owner_user_id
    return None


def replace_owner_placeholders_in_text(content: str, owner_user_id: str) -> str:
    """Return config text with owner placeholders replaced by a quoted Matrix user ID."""
    if parse_owner_matrix_user_id(owner_user_id) is None:
        return content
    # Quote the Matrix user ID so the leading '@' doesn't break YAML parsing
    # (@ starts a YAML tag/anchor when unquoted).
    quoted = f'"{owner_user_id}"'
    return content.replace(OWNER_MATRIX_USER_ID_PLACEHOLDER, quoted).replace(
        _LEGACY_OWNER_MATRIX_USER_ID_PLACEHOLDER,
        quoted,
    )
