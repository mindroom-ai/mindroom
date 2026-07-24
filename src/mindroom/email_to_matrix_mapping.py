"""Validation for trusted email-to-Matrix identity mappings."""

from __future__ import annotations


def email_to_matrix_template_error(template: str) -> str | None:
    """Return why one trusted email-to-Matrix template is invalid."""
    if template.count("{localpart}") != 1:
        return "Trusted upstream email-to-Matrix template must contain exactly one {localpart} placeholder"
    return None
