"""External credentials require signed, audience-bound identity claims."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import replace
from json.scanner import py_make_scanner  # ty: ignore[unresolved-import] -- stdlib Python scanner is absent from stubs
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import HTTPException
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from jwt.api_jws import PyJWS
from starlette.requests import Request

from mindroom.mcp_gateway.external_auth import ExternalAuth, ExternalAuthSettings
from tests.conftest import test_runtime_paths as make_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


@pytest.fixture
def runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Build isolated external authority settings inputs."""
    return make_runtime_paths(tmp_path)


@pytest.fixture
def settings() -> ExternalAuthSettings:
    """Use generic issuer and resource identities."""
    return ExternalAuthSettings(
        authorization_server="https://login.example.org/",
        issuer="https://issuer.example.org/",
        audience="https://tools.example.org/mcp",
        jwks_url="https://issuer.example.org/keys",
        matrix_user_id_claim="matrix_id",
        required_scopes=("mcp:tools",),
    )


@pytest.fixture
def signing_key() -> rsa.RSAPrivateKey:
    """Create issuer signing material for real cryptographic verification."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def claims() -> dict[str, Any]:
    """Create live audience-bound access claims."""
    return {
        "iss": "https://issuer.example.org/",
        "aud": "https://tools.example.org/mcp",
        "sub": "user-123",
        "email": "Alice@example.org",
        "matrix_id": "@Alice:example.org",
        "iat": time.time() - 1,
        "exp": time.time() + 300,
        "scope": "mcp:tools profile",
    }


def _token(signing_key: rsa.RSAPrivateKey, claims: dict[str, Any]) -> str:
    return jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "key-1"})


def _request(token: str, header: str = "authorization") -> Request:
    value = f"Bearer {token}" if header == "authorization" else token
    return Request({"type": "http", "headers": [(header.encode(), value.encode())]})


def _verifier(
    settings: ExternalAuthSettings,
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
) -> ExternalAuth:
    # Keep JWKS parsing and signature verification real; only replace remote key retrieval.
    public_jwk = RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _: {"keys": [{**public_jwk, "kid": "key-1"}]})
    return ExternalAuth(settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("header", ["authorization", "x-signed-assertion"])
async def test_signed_identity_and_token_binding(
    settings: ExternalAuthSettings,
    signing_key: rsa.RSAPrivateKey,
    claims: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    header: str,
) -> None:
    """Both configured credential forms yield exact signed identity and token digest."""
    verifier = _verifier(replace(settings, token_header=header), signing_key, monkeypatch)
    token = _token(signing_key, claims)
    identity = await verifier.verify(_request(token, header))
    assert (identity.subject, identity.email, identity.matrix_user_id) == (
        "user-123",
        "Alice@example.org",
        "@Alice:example.org",
    )
    assert identity.issued_at == claims["iat"]
    assert identity.token_digest == hashlib.sha256(token.encode()).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("iss", "https://other.example.org/"),
        ("iss", "https://issuer.example.org"),
        ("aud", "browser-client"),
        ("exp", 1),
        ("exp", "2000000000"),
        ("exp", float("inf")),
        ("iat", float("nan")),
        ("iat", True),
        ("iat", "1"),
        ("iat", 9000000000),
        ("nbf", float("inf")),
        ("nbf", "1"),
        ("nbf", 9000000000),
        ("sub", ""),
        ("sub", ["user-123"]),
        ("email", "Alice"),
        ("email", " Alice@example.org"),
        ("email", "alice@@example.org"),
        ("matrix_id", "@alice:invalid server"),
    ],
)
async def test_invalid_claims_fail_closed(
    settings: ExternalAuthSettings,
    signing_key: rsa.RSAPrivateKey,
    claims: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    claim: str,
    value: object,
) -> None:
    """Malformed identities and JWT NumericDates cannot bypass cryptographic admission."""
    verifier = _verifier(settings, signing_key, monkeypatch)
    with pytest.raises(HTTPException) as error:
        await verifier.verify(_request(_token(signing_key, {**claims, claim: value})))
    assert error.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["iss", "aud", "exp", "iat", "sub", "email", "matrix_id"])
async def test_missing_required_claim(
    settings: ExternalAuthSettings,
    signing_key: rsa.RSAPrivateKey,
    claims: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    claim: str,
) -> None:
    """Every configured identity component must be present in the signed payload."""
    verifier = _verifier(settings, signing_key, monkeypatch)
    claims.pop(claim)
    with pytest.raises(HTTPException) as error:
        await verifier.verify(_request(_token(signing_key, claims)))
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_missing_scope_is_forbidden(
    settings: ExternalAuthSettings,
    signing_key: rsa.RSAPrivateKey,
    claims: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid credential without required resource scope is forbidden."""
    verifier = _verifier(settings, signing_key, monkeypatch)
    claims["scope"] = "profile"
    with pytest.raises(HTTPException) as error:
        await verifier.verify(_request(_token(signing_key, claims)))
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_wrong_key_and_symmetric_signatures_rejected(
    settings: ExternalAuthSettings,
    signing_key: rsa.RSAPrivateKey,
    claims: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the configured asymmetric issuer keys authenticate requests."""
    verifier = _verifier(settings, signing_key, monkeypatch)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for token in [
        _token(other, claims),
        jwt.encode(claims, "untrusted-key-material-for-tests-only", algorithm="HS256"),
    ]:
        with pytest.raises(HTTPException) as error:
            await verifier.verify(_request(token))
        assert error.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"authorization", b"Bearer a.b.c"), (b"authorization", b"Bearer a.b.c")],
        [(b"authorization", b"Basic a.b.c")],
        [(b"authorization", b"Bearer a.b.c, Bearer a.b.c")],
        [(b"authorization", b"Bearer " + b"a" * 17000)],
        [(b"x-user", b"Alice@example.org")],
        [(b"authorization", b"Bearer a.b.c ")],
        [(b"authorization", b"Bearer \xff.b.c")],
    ],
)
async def test_malformed_headers_denied(
    settings: ExternalAuthSettings,
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
    headers: list[tuple[bytes, bytes]],
) -> None:
    """Duplicate, malformed, missing and unbounded credentials never authenticate."""
    verifier = _verifier(settings, signing_key, monkeypatch)
    with pytest.raises(HTTPException) as error:
        await verifier.verify(Request({"type": "http", "headers": headers}))
    assert error.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("email_domain", ["example.org", "EXAMPLE.ORG"])
async def test_explicit_email_mapping_and_subject_email(
    settings: ExternalAuthSettings,
    signing_key: rsa.RSAPrivateKey,
    claims: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    email_domain: str,
) -> None:
    """An explicitly trusted email subject can map to a Matrix identity."""
    verifier = _verifier(
        replace(
            settings,
            email_claim="sub",
            matrix_user_id_claim=None,
            email_to_matrix_user_id_template="@{localpart}:example.org",
            email_domain=email_domain,
        ),
        signing_key,
        monkeypatch,
    )
    claims["sub"] = "Alice@example.org"
    claims.pop("email")
    claims.pop("matrix_id")
    assert (await verifier.verify(_request(_token(signing_key, claims)))).matrix_user_id == "@Alice:example.org"
    claims["sub"] = "Alice@other.example.org"
    with pytest.raises(HTTPException) as error:
        await verifier.verify(_request(_token(signing_key, claims)))
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_unknown_keys_share_bounded_fetch(
    settings: ExternalAuthSettings,
    signing_key: rsa.RSAPrivateKey,
    claims: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent attacker-selected key IDs do not trigger per-token network requests."""
    calls = []
    public_jwk = RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)

    def fetch(_: jwt.PyJWKClient) -> dict[str, Any]:
        calls.append(1)
        return {"keys": [{**public_jwk, "kid": "key-1"}]}

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fetch)
    verifier = ExternalAuth(settings)
    tokens = [jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": str(index)}) for index in range(12)]
    results = await asyncio.gather(*(verifier.verify(_request(token)) for token in tokens), return_exceptions=True)
    assert all(isinstance(result, HTTPException) and result.status_code == 401 for result in results)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_key_service_failure_is_cached_denial(
    settings: ExternalAuthSettings,
    signing_key: rsa.RSAPrivateKey,
    claims: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unavailable key service denies requests without repeated network work during backoff."""
    calls = []

    def fetch(_: jwt.PyJWKClient) -> dict[str, Any]:
        calls.append(1)
        msg = "unavailable"
        raise jwt.PyJWKClientConnectionError(msg)

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fetch)
    verifier = ExternalAuth(settings)
    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            await verifier.verify(_request(_token(signing_key, claims)))
        assert error.value.status_code == 401
    assert len(calls) == 1


def _env(**updates: str) -> dict[str, str]:
    return {
        "MINDROOM_MCP_AUTH_MODE": "external",
        **{
            f"MINDROOM_MCP_EXTERNAL_{name}": value
            for name, value in {
                "AUTHORIZATION_SERVER": "https://login.example.org/",
                "ISSUER": "https://issuer.example.org/",
                "AUDIENCE": "https://tools.example.org/mcp",
                "JWKS_URL": "https://issuer.example.org/keys",
                "MATRIX_USER_ID_CLAIM": "matrix_id",
                **updates,
            }.items()
        },
    }


def test_config_preserves_exact_issuer(runtime_paths: RuntimePaths) -> None:
    """Issuer slash stays significant and browser credentials configure no MCP auth."""
    assert ExternalAuthSettings.from_paths(replace(runtime_paths, process_env={}, env_file_values={})) is None
    configured = ExternalAuthSettings.from_paths(replace(runtime_paths, process_env=_env()))
    assert configured is not None
    assert configured.issuer == "https://issuer.example.org/"
    assert configured.authorization_server == "https://login.example.org/"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "client_claim",
    [
        {},
        {"client_id": None},
        {"client_id": 123},
        {"client_id": True},
        {"client_id": ["registered-client"]},
        {"client_id": {"id": "registered-client"}},
        {"client_id": "other-client"},
        {"client_id": "Registered-client"},
    ],
)
async def test_client_pin_rejects_other_signed_clients(
    runtime_paths: RuntimePaths,
    signing_key: rsa.RSAPrivateKey,
    claims: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    client_claim: dict[str, object],
) -> None:
    """Matching issuer, key, resource and user cannot replace the configured client binding."""
    configured = ExternalAuthSettings.from_paths(
        replace(runtime_paths, process_env=_env(CLIENT_ID="registered-client")),
    )
    assert configured is not None
    verifier = _verifier(configured, signing_key, monkeypatch)
    with pytest.raises(HTTPException) as error:
        await verifier.verify(_request(_token(signing_key, {**claims, **client_claim})))
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_client_pin_accepts_exact_signed_client(
    runtime_paths: RuntimePaths,
    signing_key: rsa.RSAPrivateKey,
    claims: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An issuer-owned exact client ID can restrict a shared signing authority."""
    configured = ExternalAuthSettings.from_paths(
        replace(runtime_paths, process_env=_env(CLIENT_ID="registered-client")),
    )
    assert configured is not None
    verifier = _verifier(configured, signing_key, monkeypatch)
    identity = await verifier.verify(_request(_token(signing_key, {**claims, "client_id": "registered-client"})))
    assert identity.email == "Alice@example.org"


@pytest.mark.parametrize(
    "updates",
    [
        {"ISSUER": "http://issuer.example.org"},
        {"JWKS_URL": "https://user:pass@example.org/keys"},
        {"AUTHORIZATION_SERVER": "https://example.org/#fragment"},
        {"AUDIENCE": ""},
        {"CLIENT_ID": " registered-client"},
        {"CLIENT_ID": "client\nvalue"},
        {"CLIENT_ID": "x" * 1025},
        {"TOKEN_HEADER": "bad header"},
        {"MATRIX_USER_ID_CLAIM": ""},
        {"EMAIL_TO_MATRIX_USER_ID_TEMPLATE": "@{localpart}:example.org"},
        {"MATRIX_USER_ID_CLAIM": "", "EMAIL_TO_MATRIX_USER_ID_TEMPLATE": "@{other}:example.org"},
    ],
)
def test_invalid_config_fails_closed(runtime_paths: RuntimePaths, updates: dict[str, str]) -> None:
    """Partial or ambiguous external authority configuration cannot become built-in auth."""
    with pytest.raises(ValueError, match=r"."):
        ExternalAuthSettings.from_paths(replace(runtime_paths, process_env=_env(**updates)))


@pytest.mark.asyncio
async def test_es256_signing_key(
    settings: ExternalAuthSettings,
    claims: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An issuer using the allowed elliptic-curve algorithm also authenticates."""
    key = ec.generate_private_key(ec.SECP256R1())
    public_jwk = ECAlgorithm.to_jwk(key.public_key(), as_dict=True)
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _: {"keys": [{**public_jwk, "kid": "ec-key"}]})
    token = jwt.encode(claims, key, algorithm="ES256", headers={"kid": "ec-key"})
    assert (await ExternalAuth(settings).verify(_request(token))).subject == "user-123"


@pytest.mark.asyncio
async def test_assertion_mode_has_no_bearer_fallback(
    settings: ExternalAuthSettings,
    signing_key: rsa.RSAPrivateKey,
    claims: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only one unambiguous signed assertion header authenticates in assertion mode."""
    assertion_settings = replace(settings, token_header="x-signed-assertion")  # noqa: S106 -- header name
    verifier = _verifier(assertion_settings, signing_key, monkeypatch)
    token = _token(signing_key, claims)
    requests = [_request(token), Request({"type": "http", "headers": [(b"x-signed-assertion", token.encode())] * 2})]
    for request in requests:
        with pytest.raises(HTTPException) as error:
            await verifier.verify(request)
        assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_untrusted_algorithm_never_fetches_keys(
    settings: ExternalAuthSettings,
    claims: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric credentials cannot spend JWKS network work before rejection."""

    def fetch(_: jwt.PyJWKClient) -> dict[str, Any]:
        pytest.fail("Untrusted algorithm fetched issuer keys")

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fetch)
    token = jwt.encode(claims, "untrusted-key-material-for-tests-only", algorithm="HS256", headers={"kid": "key-1"})
    with pytest.raises(HTTPException) as error:
        await ExternalAuth(settings).verify(_request(token))
    assert error.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("parser", ["default", "python"])
async def test_deeply_nested_payload_is_invalid_credential(
    settings: ExternalAuthSettings,
    signing_key: rsa.RSAPrivateKey,
    monkeypatch: pytest.MonkeyPatch,
    parser: str,
) -> None:
    """Bounded but excessively nested signed JSON cannot escape credential rejection."""
    verifier = _verifier(settings, signing_key, monkeypatch)
    payload = b'{"nested":' + b"[" * 1200 + b"0" + b"]" * 1200 + b"}"
    token = PyJWS().encode(payload, signing_key, algorithm="RS256", headers={"kid": "key-1"})
    assert len(token) <= 16384
    if parser == "python":
        decoder = json.JSONDecoder()
        decoder.scan_once = py_make_scanner(decoder)
        with pytest.raises(RecursionError):
            decoder.decode(payload.decode())
        monkeypatch.setattr(jwt.api_jwt, "json", SimpleNamespace(loads=lambda value: decoder.decode(value.decode())))
    with pytest.raises(HTTPException) as error:
        await verifier.verify(_request(token))
    assert error.value.status_code == 401
    if parser == "python":
        assert isinstance(error.value.__cause__, RecursionError)
