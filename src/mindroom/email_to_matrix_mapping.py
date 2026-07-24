"""Validation for trusted email-to-Matrix identity mappings."""

from __future__ import annotations


def email_to_matrix_template_error(template: str) -> str | None:
    """Return why one trusted email-to-Matrix template is invalid."""
    placeholder = "{localpart}"
    remainder = template.replace(placeholder, "")
    if template.count(placeholder) != 1 or "{" in remainder or "}" in remainder:
        return (
            "Trusted upstream email-to-Matrix template must contain exactly one "
            "{localpart} placeholder and no other braces"
        )
    return None
