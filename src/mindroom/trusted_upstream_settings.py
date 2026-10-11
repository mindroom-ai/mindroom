"""Trusted-upstream browser identity settings shared by API auth and config validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


@dataclass(frozen=True)
class _TrustedUpstreamJwtSettings:
    """Signed assertion settings for trusted-upstream auth."""

    require_jwt: bool = False
    header: str | None = None
    jwks_url: str | None = None
    audience: str | None = None
    issuer: str | None = None
    email_claim: str = "email"
    user_id_claim: str | None = None
    matrix_user_id_claim: str | None = None


@dataclass(frozen=True)
class TrustedUpstreamAuthSettings:
    """Trusted reverse-proxy/browser identity settings for hosted deployments."""

    enabled: bool = False
    user_id_header: str | None = None
    email_header: str | None = None
    matrix_user_id_header: str | None = None
    email_to_matrix_user_id_template: str | None = None
    email_domain: str | None = None
    jwt: _TrustedUpstreamJwtSettings = field(default_factory=_TrustedUpstreamJwtSettings)


def env_text(runtime_paths: RuntimePaths, name: str) -> str | None:
    """Return one stripped runtime environment value, treating blank values as unset."""
    value = runtime_paths.env_value(name)
    if value is None:
        return None
    return value.strip() or None


def trusted_upstream_auth_settings(runtime_paths: RuntimePaths) -> TrustedUpstreamAuthSettings:
    """Read trusted-upstream auth settings from one runtime context."""
    return TrustedUpstreamAuthSettings(
        enabled=runtime_paths.env_flag("MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED"),
        user_id_header=env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER"),
        email_header=env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER"),
        matrix_user_id_header=env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_MATRIX_USER_ID_HEADER"),
        email_to_matrix_user_id_template=env_text(
            runtime_paths,
            "MINDROOM_TRUSTED_UPSTREAM_EMAIL_TO_MATRIX_USER_ID_TEMPLATE",
        ),
        email_domain=env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN"),
        jwt=_TrustedUpstreamJwtSettings(
            require_jwt=runtime_paths.env_flag("MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT"),
            header=env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER"),
            jwks_url=env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWKS_URL"),
            audience=env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE"),
            issuer=env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER"),
            email_claim=env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWT_EMAIL_CLAIM") or "email",
            user_id_claim=env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWT_USER_ID_CLAIM"),
            matrix_user_id_claim=env_text(runtime_paths, "MINDROOM_TRUSTED_UPSTREAM_JWT_MATRIX_USER_ID_CLAIM"),
        ),
    )


def matrix_identity_configuration_error(settings: TrustedUpstreamAuthSettings) -> str | None:  # noqa: PLR0911
    """Return why trusted-upstream auth cannot yield a verified Matrix user ID."""
    if not settings.enabled:
        return "MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED is not enabled"
    if settings.user_id_header is None:
        return "MINDROOM_TRUSTED_UPSTREAM_USER_ID_HEADER is not set"
    jwt = settings.jwt
    if jwt.require_jwt:
        required_jwt_settings = (
            ("MINDROOM_TRUSTED_UPSTREAM_JWT_HEADER", jwt.header),
            ("MINDROOM_TRUSTED_UPSTREAM_JWKS_URL", jwt.jwks_url),
            ("MINDROOM_TRUSTED_UPSTREAM_JWT_AUDIENCE", jwt.audience),
            ("MINDROOM_TRUSTED_UPSTREAM_JWT_ISSUER", jwt.issuer),
        )
        missing_setting = next((name for name, value in required_jwt_settings if value is None), None)
        if missing_setting is not None:
            return f"{missing_setting} is not set"
        if jwt.matrix_user_id_claim is not None:
            return None
        template = settings.email_to_matrix_user_id_template
        if template is None:
            return "strict trusted upstream auth has no verified Matrix identity claim or email mapping"
        return _email_mapping_error(template, settings.email_domain)
    if settings.matrix_user_id_header is not None:
        return None
    template = settings.email_to_matrix_user_id_template
    if template is None:
        return "trusted upstream auth has no Matrix identity header or email mapping"
    if settings.email_header is None:
        return "MINDROOM_TRUSTED_UPSTREAM_EMAIL_HEADER is required by the email-to-Matrix mapping"
    return _email_mapping_error(template, settings.email_domain)


def _email_mapping_error(template: str, email_domain: str | None) -> str | None:
    # Keep Matrix state out of the slim config import path until a template needs validation.
    from mindroom.matrix.identity import validate_email_to_matrix_mapping  # noqa: PLC0415

    try:
        validate_email_to_matrix_mapping(template, email_domain)
    except ValueError:
        return "trusted upstream email mapping requires a valid template and MINDROOM_TRUSTED_UPSTREAM_EMAIL_DOMAIN"
    return None
