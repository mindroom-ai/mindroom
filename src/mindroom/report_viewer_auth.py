"""Runtime configuration checks for protected report viewer identity."""

from __future__ import annotations

from typing import Protocol


class _RuntimeEnvironment(Protocol):
    def env_flag(self, name: str, *, default: bool = False) -> bool: ...

    def env_value(self, name: str, *, default: str | None = None) -> str | None: ...


def report_viewer_auth_configuration_error(runtime_paths: _RuntimeEnvironment) -> str | None:
    """Return why trusted browser auth cannot yield verified Matrix identity."""
    error: str | None = None
    if not runtime_paths.env_flag("MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED"):
        error = "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED is not enabled"
    elif not _env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER"):
        error = "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER is not set"
    elif runtime_paths.env_flag("MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT"):
        required_jwt_settings = (
            "MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER",
            "MINDROOM_TRUSTED_UPSTREAM_JWKS_URL",
            "MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE",
            "MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER",
        )
        missing_setting = next(
            (name for name in required_jwt_settings if not _env_text(runtime_paths, name)),
            None,
        )
        if missing_setting is not None:
            error = f"{missing_setting} is not set"
        elif not _env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWT_MATRIX_USER_ID_CLAIM"):
            email_template = _env_text(
                runtime_paths,
                "MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE",
            )
            if email_template is None:
                error = "strict trusted upstream auth has no verified Matrix identity claim or email mapping"
            else:
                error = _email_mapping_error(runtime_paths, email_template)
    elif not _env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER"):
        email_template = _env_text(
            runtime_paths,
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE",
        )
        if email_template is None:
            error = "trusted upstream auth has no Matrix identity header or email mapping"
        else:
            error = _email_mapping_error(runtime_paths, email_template)
            if error is None and not _env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER"):
                error = "MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER is required by the email-to-Matrix mapping"
    return error


def _email_mapping_error(runtime_paths: _RuntimeEnvironment, template: str) -> str | None:
    # Keep Matrix state out of the slim config import path until a template needs validation.
    from mindroom.matrix.identity import validate_email_to_matrix_mapping  # noqa: PLC0415

    try:
        validate_email_to_matrix_mapping(
            template,
            _env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN"),
        )
    except ValueError:
        return "trusted upstream email mapping requires a valid template and MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN"
    return None


def _env_text(runtime_paths: _RuntimeEnvironment, name: str) -> str | None:
    value = runtime_paths.env_value(name)
    if value is None:
        return None
    return value.strip() or None
