"""Desktop Matrix login method names shared by CLI and session logic."""

from enum import StrEnum


class DesktopLoginMethod(StrEnum):
    """Supported desktop Matrix authentication choices."""

    AUTO = "auto"
    PASSWORD = "password"  # noqa: S105 - Authentication method name, not a credential.
    SSO = "sso"


__all__ = ["DesktopLoginMethod"]
