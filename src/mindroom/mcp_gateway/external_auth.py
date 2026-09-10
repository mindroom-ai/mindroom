"""Verify external signed MCP credentials without browser authentication fallback."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import jwt
from fastapi import HTTPException

from mindroom.matrix.identity import try_parse_historical_matrix_user_id

if TYPE_CHECKING:
    from starlette.requests import Request

    from mindroom.constants import RuntimePaths

_ALGORITHMS = ("RS256", "ES256")
_MAX_TOKEN_BYTES = 16384
_KEY_CACHE_SECONDS = 60


def _text(value: object, *, limit: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > limit
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        msg = "Invalid external authentication text"
        raise ValueError(msg)
    return value


def _https_url(value: str) -> None:
    parsed = urlsplit(_text(value))
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or parsed.port == 0
        or any(char.isspace() for char in value)
    ):
        msg = "External authentication URLs must use HTTPS without credentials, queries or fragments"
        raise ValueError(msg)


@dataclass(frozen=True)
class ExternalAuthSettings:
    """One explicit external authority and its signed identity mapping."""

    authorization_server: str
    issuer: str
    audience: str
    jwks_url: str
    token_header: str = "authorization"  # noqa: S105 -- header name, not a secret
    email_claim: str = "email"
    matrix_user_id_claim: str | None = None
    email_to_matrix_user_id_template: str | None = None
    email_domain: str | None = None
    required_scopes: tuple[str, ...] = ()
    client_id: str | None = None

    def __post_init__(self) -> None:
        """Reject incomplete or ambiguous external trust settings."""
        for value in (self.authorization_server, self.issuer, self.jwks_url):
            _https_url(value)
        _text(self.audience)
        _text(self.email_claim)
        if self.client_id is not None:
            _text(self.client_id)
        if re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", self.token_header) is None:
            msg = "Invalid external token header"
            raise ValueError(msg)
        template = self.email_to_matrix_user_id_template
        if (self.matrix_user_id_claim is None) == (template is None):
            msg = "Configure exactly one external Matrix identity claim or email template"
            raise ValueError(msg)
        if self.matrix_user_id_claim is not None:
            _text(self.matrix_user_id_claim)
        if template is not None:
            _text(template)
            if (
                template.count("{localpart}") != 1
                or "{" in template.replace("{localpart}", "")
                or "}" in template.replace("{localpart}", "")
                or try_parse_historical_matrix_user_id(template.replace("{localpart}", "example")) is None
                or self.email_domain is None
                or re.fullmatch(r"[A-Za-z0-9.-]+", self.email_domain) is None
            ):
                msg = "External email mapping requires a valid template and explicit email domain"
                raise ValueError(msg)
        elif self.email_domain is not None:
            msg = "External email domain requires an email template"
            raise ValueError(msg)
        if any(re.fullmatch(r"[\x21\x23-\x5b\x5d-\x7e]+", scope) is None for scope in self.required_scopes):
            msg = "Invalid external required scope"
            raise ValueError(msg)

    @classmethod
    def from_paths(cls, paths: RuntimePaths) -> ExternalAuthSettings | None:
        """Read mode and validate all external settings before accepting requests."""
        mode = paths.env_value("MINDROOM_MCP_AUTH_MODE", default="builtin")
        if mode == "builtin":
            return None
        if mode != "external":
            msg = "MINDROOM_MCP_AUTH_MODE must be builtin or external"
            raise ValueError(msg)

        def value(name: str, default: str | None = None) -> str | None:
            return paths.env_value(f"MINDROOM_MCP_EXTERNAL_{name}", default=default) or None

        return cls(
            authorization_server=_text(value("AUTHORIZATION_SERVER")),
            issuer=_text(value("ISSUER")),
            audience=_text(value("AUDIENCE")),
            jwks_url=_text(value("JWKS_URL")),
            token_header=_text(value("TOKEN_HEADER", "authorization")).lower(),
            email_claim=_text(value("EMAIL_CLAIM", "email")),
            matrix_user_id_claim=value("MATRIX_USER_ID_CLAIM"),
            email_to_matrix_user_id_template=value("EMAIL_TO_MATRIX_USER_ID_TEMPLATE"),
            email_domain=value("EMAIL_DOMAIN"),
            required_scopes=tuple((value("REQUIRED_SCOPES") or "").split()),
            client_id=value("CLIENT_ID"),
        )


@dataclass(frozen=True)
class ExternalIdentity:
    """Verified claims plus a credential-specific admission and cancellation binding."""

    subject: str
    email: str
    matrix_user_id: str
    issued_at: float
    token_digest: str


class ExternalAuth:
    """Check signed credentials with bounded, shared JWKS refresh work."""

    def __init__(self, settings: ExternalAuthSettings) -> None:
        self.settings = settings
        self._client = jwt.PyJWKClient(settings.jwks_url, cache_jwk_set=False, timeout=5)
        self._keys: list[jwt.PyJWK] = []
        self._refresh_after = 0.0
        self._lock = asyncio.Lock()

    async def _signing_key(self, key_id: str, algorithm: str) -> jwt.PyJWK:
        async with self._lock:
            if time.monotonic() >= self._refresh_after:
                self._refresh_after = time.monotonic() + _KEY_CACHE_SECONDS
                self._keys = []
                self._keys = await asyncio.to_thread(self._client.get_signing_keys, refresh=True)
            matches = [key for key in self._keys if key.key_id == key_id and key.algorithm_name == algorithm]
            if len(matches) != 1:
                raise jwt.InvalidTokenError
            return matches[0]

    def _token(self, request: Request) -> str:
        values = request.headers.getlist(self.settings.token_header)
        if len(values) != 1 or len(values[0]) > _MAX_TOKEN_BYTES + 7:
            msg = "Invalid external credential header"
            raise ValueError(msg)
        token = values[0]
        if self.settings.token_header == "authorization":  # noqa: S105 -- header name
            scheme, separator, token = token.partition(" ")
            if not separator or scheme.lower() != "bearer":
                msg = "Invalid external bearer credential"
                raise ValueError(msg)
        if (
            len(token) > _MAX_TOKEN_BYTES
            or re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token) is None
        ):
            msg = "Invalid external JWT encoding"
            raise ValueError(msg)
        return token

    async def verify(self, request: Request) -> ExternalIdentity:
        """Return only verified identity; reject invalid credentials and missing scopes."""
        try:
            token = self._token(request)
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            if algorithm not in _ALGORITHMS or header.get("crit"):
                raise jwt.InvalidTokenError
            key = await self._signing_key(_text(header.get("kid")), algorithm)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=list(_ALGORITHMS),
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                options={"require": ["iss", "aud", "exp", "iat", "sub"]},
            )
            now = time.time()
            for name in claims.keys() & {"exp", "iat", "nbf"}:
                value = claims[name]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise jwt.InvalidTokenError
                if (name == "exp" and value <= now) or (name != "exp" and value > now):
                    raise jwt.InvalidTokenError
            subject = _text(claims["sub"])
            if self.settings.client_id is not None and _text(claims.get("client_id")) != self.settings.client_id:
                raise jwt.InvalidTokenError
            email = _text(claims.get(self.settings.email_claim), limit=320)
            if email.count("@") != 1 or any(char.isspace() for char in email):
                raise jwt.InvalidTokenError
            localpart, domain = email.split("@")
            if not localpart or not domain:
                raise jwt.InvalidTokenError
            matrix_id = self._matrix_identity(claims, localpart, domain)
        except (jwt.PyJWTError, ValueError, TypeError, OverflowError, RecursionError) as error:
            raise HTTPException(status_code=401, detail="Invalid external MCP credential") from error
        scope = claims.get("scope", "")
        if not isinstance(scope, str) or not set(self.settings.required_scopes).issubset(scope.split()):
            raise HTTPException(status_code=403, detail="Insufficient external MCP scope")
        return ExternalIdentity(
            subject,
            email,
            matrix_id,
            float(claims["iat"]),
            hashlib.sha256(token.encode()).hexdigest(),
        )

    def _matrix_identity(self, claims: dict[str, Any], localpart: str, domain: str) -> str:
        if self.settings.matrix_user_id_claim is not None:
            value = _text(claims.get(self.settings.matrix_user_id_claim))
        else:
            if (
                domain.lower() != (self.settings.email_domain or "").lower()
                or not self.settings.email_to_matrix_user_id_template
            ):
                raise jwt.InvalidTokenError
            value = self.settings.email_to_matrix_user_id_template.replace("{localpart}", localpart)
        matrix_id = try_parse_historical_matrix_user_id(value)
        if matrix_id is None:
            raise jwt.InvalidTokenError
        return matrix_id
