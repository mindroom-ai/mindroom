"""First-party OpenID Connect issuer for hosted Synapse login."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import jwt
from backend.config import (
    INSTANCE_BASE_DOMAIN,
    MATRIX_OIDC_CLIENT_ID,
    MATRIX_OIDC_CLIENT_SECRET,
    MATRIX_OIDC_ENABLED,
    MATRIX_OIDC_ISSUER,
    MATRIX_OIDC_KEY_ID,
    MATRIX_OIDC_PRIVATE_KEY,
    PLATFORM_DOMAIN,
)
from backend.deps import _extract_bearer_token, ensure_supabase, limiter, verify_user
from backend.entitlements import assert_instance_entitlement
from backend.services import instances_data
from cachetools import TTLCache
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import RedirectResponse
from jwt.algorithms import RSAAlgorithm

router = APIRouter()

AUTH_CODE_TTL_SECONDS = 120
ACCESS_TOKEN_TTL_SECONDS = 3600
_USED_AUTH_CODE_IDS = TTLCache(maxsize=10_000, ttl=AUTH_CODE_TTL_SECONDS)


def _issuer() -> str:
    if MATRIX_OIDC_ISSUER:
        return MATRIX_OIDC_ISSUER.rstrip("/")
    return f"https://api.{PLATFORM_DOMAIN}/matrix-oidc"


def _require_enabled() -> None:
    if not MATRIX_OIDC_ENABLED:
        raise HTTPException(status_code=404, detail="Matrix OIDC is not enabled")


def _private_key() -> RSAPrivateKey:
    if not MATRIX_OIDC_PRIVATE_KEY:
        raise HTTPException(status_code=500, detail="Matrix OIDC signing key is not configured")
    key = serialization.load_pem_private_key(MATRIX_OIDC_PRIVATE_KEY.encode("utf-8"), password=None)
    if not isinstance(key, RSAPrivateKey):
        raise HTTPException(status_code=500, detail="Matrix OIDC signing key must be an RSA private key")
    return key


def _public_key() -> RSAPublicKey:
    return _private_key().public_key()


def _public_jwk() -> dict[str, Any]:
    jwk = json.loads(RSAAlgorithm.to_jwk(_public_key()))
    jwk["kid"] = MATRIX_OIDC_KEY_ID
    jwk["use"] = "sig"
    jwk["alg"] = "RS256"
    return jwk


def _now() -> datetime:
    return datetime.now(UTC)


def _epoch(value: datetime) -> int:
    return int(value.timestamp())


def _sign_claims(claims: dict[str, Any]) -> str:
    return jwt.encode(claims, _private_key(), algorithm="RS256", headers={"kid": MATRIX_OIDC_KEY_ID})


def _decode_claims(token: str, *, audience: str) -> dict[str, Any]:
    return jwt.decode(token, _public_key(), algorithms=["RS256"], audience=audience, issuer=_issuer())


def _authorize_url(request: Request) -> str:
    query = request.url.query
    suffix = f"?{query}" if query else ""
    return f"{_issuer()}/authorize{suffix}"


def _platform_login_redirect(request: Request) -> RedirectResponse:
    target = quote(_authorize_url(request), safe="")
    return RedirectResponse(f"https://app.{PLATFORM_DOMAIN}/auth/login?redirect_to={target}")


def _validate_redirect_uri(redirect_uri: str) -> str:
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "https" or parsed.path != "/_synapse/client/oidc/callback":
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")

    hostname = (parsed.hostname or "").lower()
    suffix = f".matrix.{(INSTANCE_BASE_DOMAIN or PLATFORM_DOMAIN).lower()}"
    if not hostname.endswith(suffix):
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")

    instance_id = hostname[: -len(suffix)]
    if not instance_id or "." in instance_id:
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")
    return instance_id


def _load_owned_instance(instance_id: str, account_id: str) -> dict[str, Any]:
    instance = instances_data.get_owned_instance(ensure_supabase(), instance_id, account_id)
    if instance is None:
        raise HTTPException(status_code=403, detail="Instance not found or access denied")
    return instance


def _assert_instance_subscription_allows_login(instance: dict[str, Any]) -> None:
    sb = ensure_supabase()
    result = sb.table("subscriptions").select("*").eq("id", instance["subscription_id"]).limit(1).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Subscription not found")
    assert_instance_entitlement(result.data[0], "sign in to")


def _subject_from_user(user: dict[str, Any]) -> str:
    return str(user["user_id"])


def _display_name_from_user(user: dict[str, Any]) -> str:
    account = user.get("account") or {}
    full_name = str(account.get("full_name") or "").strip()
    if full_name:
        return full_name
    email = str(user.get("email") or "").strip()
    return email.split("@", maxsplit=1)[0] if email else _subject_from_user(user)


def _email_from_user(user: dict[str, Any]) -> str:
    email = str(user.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=403, detail="Matrix SSO requires a verified platform email")
    return email


def _build_code_claims(
    *, user: dict[str, Any], instance_id: str, redirect_uri: str, scope: str, nonce: str | None
) -> dict[str, Any]:
    now = _now()
    return {
        "typ": "matrix_oidc_code",
        "iss": _issuer(),
        "sub": _subject_from_user(user),
        "aud": MATRIX_OIDC_CLIENT_ID,
        "iat": _epoch(now),
        "exp": _epoch(now + timedelta(seconds=AUTH_CODE_TTL_SECONDS)),
        "jti": secrets.token_urlsafe(24),
        "account_id": str(user["account_id"]),
        "instance_id": instance_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "nonce": nonce,
        "email": _email_from_user(user),
        "email_verified": True,
        "name": _display_name_from_user(user),
    }


def _parse_request_body(raw_body: bytes) -> dict[str, str]:
    decoded = raw_body.decode("utf-8")
    parsed = parse_qs(decoded, keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def _client_auth_from_basic(authorization: str | None) -> tuple[str | None, str | None]:
    if not authorization or not authorization.lower().startswith("basic "):
        return None, None
    encoded = authorization.split(" ", maxsplit=1)[1]
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=401, detail="Invalid client authentication") from None
    client_id, separator, client_secret = decoded.partition(":")
    if not separator:
        raise HTTPException(status_code=401, detail="Invalid client authentication")
    return client_id, client_secret


def _require_client_auth(params: dict[str, str], authorization: str | None) -> None:
    basic_client_id, basic_client_secret = _client_auth_from_basic(authorization)
    client_id = basic_client_id or params.get("client_id") or ""
    client_secret = basic_client_secret or params.get("client_secret") or ""
    if client_id != MATRIX_OIDC_CLIENT_ID:
        raise HTTPException(status_code=401, detail="Invalid client authentication")
    if not MATRIX_OIDC_CLIENT_SECRET:
        raise HTTPException(status_code=500, detail="Matrix OIDC client secret is not configured")
    if not hmac.compare_digest(client_secret, MATRIX_OIDC_CLIENT_SECRET):
        raise HTTPException(status_code=401, detail="Invalid client authentication")


def _consume_code(code: str, redirect_uri: str) -> dict[str, Any]:
    claims = _decode_claims(code, audience=MATRIX_OIDC_CLIENT_ID)
    if claims.get("typ") != "matrix_oidc_code":
        raise HTTPException(status_code=400, detail="Invalid authorization code")
    if claims.get("redirect_uri") != redirect_uri:
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")

    jti = str(claims.get("jti") or "")
    if not jti or jti in _USED_AUTH_CODE_IDS:
        raise HTTPException(status_code=400, detail="Authorization code has already been used")
    _USED_AUTH_CODE_IDS[jti] = True
    return claims


def _build_id_token_claims(code_claims: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    claims = {
        "iss": _issuer(),
        "sub": code_claims["sub"],
        "aud": MATRIX_OIDC_CLIENT_ID,
        "iat": _epoch(now),
        "exp": _epoch(now + timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS)),
        "auth_time": code_claims["iat"],
        "email": code_claims["email"],
        "email_verified": code_claims["email_verified"],
        "name": code_claims["name"],
    }
    if code_claims.get("nonce"):
        claims["nonce"] = code_claims["nonce"]
    return claims


def _build_access_token_claims(code_claims: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    return {
        "typ": "matrix_oidc_access",
        "iss": _issuer(),
        "sub": code_claims["sub"],
        "aud": "matrix_oidc_userinfo",
        "iat": _epoch(now),
        "exp": _epoch(now + timedelta(seconds=ACCESS_TOKEN_TTL_SECONDS)),
        "email": code_claims["email"],
        "email_verified": code_claims["email_verified"],
        "name": code_claims["name"],
    }


@router.get("/matrix-oidc/.well-known/openid-configuration")
@router.get("/.well-known/openid-configuration/matrix-oidc")
@limiter.limit("60/minute")
async def openid_configuration(request: Request) -> dict[str, Any]:
    """Return OIDC discovery metadata for Synapse."""
    _require_enabled()
    issuer = _issuer()
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/authorize",
        "token_endpoint": f"{issuer}/token",
        "userinfo_endpoint": f"{issuer}/userinfo",
        "jwks_uri": f"{issuer}/jwks.json",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
        "scopes_supported": ["openid", "profile", "email"],
        "claims_supported": ["sub", "email", "email_verified", "name"],
    }


@router.get("/matrix-oidc/jwks.json")
@limiter.limit("60/minute")
async def jwks(request: Request) -> dict[str, Any]:
    """Return public signing keys for OIDC token verification."""
    _require_enabled()
    return {"keys": [_public_jwk()]}


@router.get("/matrix-oidc/authorize", response_model=None)
@limiter.limit("60/minute")
async def authorize(
    request: Request,
    response_type: str,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    nonce: str | None = None,
) -> RedirectResponse:
    """Issue an authorization code to Synapse after platform-cookie authentication."""
    _require_enabled()
    if response_type != "code" or client_id != MATRIX_OIDC_CLIENT_ID:
        raise HTTPException(status_code=400, detail="Invalid OIDC authorization request")

    instance_id = _validate_redirect_uri(redirect_uri)
    token = request.cookies.get("mindroom_jwt")
    if not token:
        return _platform_login_redirect(request)

    try:
        user = await verify_user(authorization=f"Bearer {token}", request=request)
    except HTTPException:
        return _platform_login_redirect(request)

    instance = _load_owned_instance(instance_id, str(user["account_id"]))
    _assert_instance_subscription_allows_login(instance)
    code = _sign_claims(
        _build_code_claims(user=user, instance_id=instance_id, redirect_uri=redirect_uri, scope=scope, nonce=nonce)
    )
    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(f"{redirect_uri}{separator}code={quote(code, safe='')}&state={quote(state, safe='')}")


@router.post("/matrix-oidc/token")
@limiter.limit("60/minute")
async def token(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Exchange a Synapse authorization code for OIDC tokens."""
    _require_enabled()
    params = _parse_request_body(await request.body())
    _require_client_auth(params, authorization)
    if params.get("grant_type") != "authorization_code":
        raise HTTPException(status_code=400, detail="Unsupported grant_type")
    code = params.get("code") or ""
    redirect_uri = params.get("redirect_uri") or ""
    code_claims = _consume_code(code, redirect_uri)
    access_token = _sign_claims(_build_access_token_claims(code_claims))
    id_token = _sign_claims(_build_id_token_claims(code_claims))
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        "id_token": id_token,
        "scope": code_claims.get("scope") or "openid profile email",
    }


@router.get("/matrix-oidc/userinfo")
@limiter.limit("60/minute")
async def userinfo(request: Request, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Return user claims for Synapse's OIDC userinfo fetch."""
    _require_enabled()
    access_token = _extract_bearer_token(authorization)
    claims = _decode_claims(access_token, audience="matrix_oidc_userinfo")
    if claims.get("typ") != "matrix_oidc_access":
        raise HTTPException(status_code=401, detail="Invalid access token")
    return {
        "sub": claims["sub"],
        "email": claims["email"],
        "email_verified": claims.get("email_verified") is True,
        "name": claims.get("name") or claims["email"],
    }
