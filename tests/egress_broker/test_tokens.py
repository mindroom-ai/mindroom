"""Tests for worker proxy token signing and verification."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import replace
from pathlib import Path  # noqa: TC003 - Required at runtime for test fixtures
from typing import Literal

import pytest

from mindroom.egress_broker.tokens import TokenSigner, WorkerClaims
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity


@pytest.fixture
def signer() -> TokenSigner:
    """Return a token signer with a fixed key."""
    return TokenSigner(b"k" * 32)


@pytest.fixture
def claims() -> WorkerClaims:
    """Return sample worker claims for testing."""
    return WorkerClaims(
        worker_key="worker_abc123",
        worker_scope="user",
        routing_agent_name="router",
        tenant_id="tenant_1",
        account_id="account_1",
        channel="matrix",
        agent_name="agent_1",
        requester_id="@user:example.com",
    )


def test_roundtrip(signer: TokenSigner, claims: WorkerClaims) -> None:
    """Token roundtrip preserves claims."""
    assert signer.verify(signer.mint(claims)) == claims


@pytest.mark.parametrize(
    ("age_seconds", "valid"),
    [
        pytest.param(604799, True, id="valid-until-ttl"),
        pytest.param(604801, False, id="expired-beyond-ttl"),
    ],
)
def test_token_expires_with_the_default_ttl(
    signer: TokenSigner,
    claims: WorkerClaims,
    age_seconds: int,
    valid: bool,
) -> None:
    """A token verifies until the default one-week TTL and is rejected after it."""
    assert signer.verify(signer.mint(claims, now=1000), now=1000 + age_seconds) == (claims if valid else None)


def test_custom_ttl(claims: WorkerClaims) -> None:
    """Custom TTL is respected."""
    s = TokenSigner(b"k" * 32, ttl_seconds=60)
    assert s.verify(s.mint(claims, now=0), now=61) is None


def test_tampered_claims_rejected(signer: TokenSigner, claims: WorkerClaims) -> None:
    """Tampered claims segment is rejected."""
    token = signer.mint(claims)
    parts = token.split(".")
    assert len(parts) == 3

    # Flip one character in the claims segment (base64url-encoded payload)
    payload_bytes = parts[1].encode("ascii")
    if payload_bytes:
        tampered_byte = bytes([payload_bytes[0] ^ 1])  # flip first bit
        tampered_payload = tampered_byte + payload_bytes[1:]
        tampered_token = f"{parts[0]}.{tampered_payload.decode('ascii')}.{parts[2]}"
        assert signer.verify(tampered_token) is None


def test_other_key_rejected(claims: WorkerClaims) -> None:
    """Token signed with different key is rejected."""
    signer_x = TokenSigner(b"x" * 32)
    signer_y = TokenSigner(b"y" * 32)
    token = signer_x.mint(claims)
    assert signer_y.verify(token) is None


@pytest.mark.parametrize(
    "token",
    [
        "",
        "mrb1.",
        "mrb2.a.b",
        "mrb1.a.b.c",
        "mrb1." + "a" * 5000 + ".b",
        "mrb1.!!!.b",
        "mrb1.\u00e9.b",
    ],
)
def test_malformed_rejected(signer: TokenSigner, token: str) -> None:
    """Malformed tokens are rejected."""
    assert signer.verify(token) is None


def _signed_token(payload: bytes) -> str:
    """Return a token for raw payload bytes with a valid MAC under the `signer` fixture's key."""
    mac = hmac.new(b"k" * 32, b"mindroom-egress-broker-token-v1" + payload, hashlib.sha256).digest()

    def encode(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"mrb1.{encode(payload)}.{encode(mac)}"


_VALID_CLAIMS: dict[str, object] = {
    "worker_key": "worker_abc123",
    "worker_scope": "user",
    "routing_agent_name": "router",
    "tenant_id": "tenant_1",
    "account_id": "account_1",
    "channel": "matrix",
    "agent_name": "agent_1",
    "requester_id": "@user:example.com",
}


def _payload(claims: object = None, *, exp: object = 2000) -> bytes:
    return json.dumps({"claims": _VALID_CLAIMS if claims is None else claims, "exp": exp}).encode()


def test_signed_payload_with_valid_claims_is_accepted(signer: TokenSigner, claims: WorkerClaims) -> None:
    """The hand-signed payload helper produces a token `verify` accepts, so the rejections below test the claims."""
    assert signer.verify(_signed_token(_payload()), now=1000) == claims
    unscoped = {**_VALID_CLAIMS, "worker_scope": None, "routing_agent_name": None, "requester_id": None}
    assert signer.verify(_signed_token(_payload(unscoped)), now=1000) is not None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(b"\xff\xfe\xfa", id="not-utf8"),
        pytest.param(b"[]", id="not-an-object"),
        pytest.param(_payload(exp="2000"), id="exp-not-int"),
        pytest.param(_payload(exp=1000), id="expired"),
        pytest.param(_payload([]), id="claims-not-an-object"),
        pytest.param(_payload({**_VALID_CLAIMS, "extra": "x"}), id="unknown-claim"),
        pytest.param(_payload({k: v for k, v in _VALID_CLAIMS.items() if k != "tenant_id"}), id="missing-claim"),
        pytest.param(_payload({**_VALID_CLAIMS, "worker_key": 1}), id="worker-key-not-str"),
        pytest.param(_payload({**_VALID_CLAIMS, "agent_name": None}), id="agent-name-none"),
        pytest.param(_payload({**_VALID_CLAIMS, "worker_scope": "global"}), id="unknown-scope"),
        pytest.param(_payload({**_VALID_CLAIMS, "channel": None}), id="channel-none"),
        pytest.param(_payload({**_VALID_CLAIMS, "requester_id": 7}), id="requester-not-str"),
        pytest.param(_payload({**_VALID_CLAIMS, "tenant_id": ["t"]}), id="tenant-not-str"),
    ],
)
def test_signed_payload_with_invalid_content_is_rejected(signer: TokenSigner, payload: bytes) -> None:
    """A correctly signed payload is still rejected when its JSON, expiry, or claims are malformed."""
    assert signer.verify(_signed_token(payload), now=1000) is None


def test_load_or_create_persists_key_with_0600(tmp_path: Path, claims: WorkerClaims) -> None:
    """load_or_create persists key with mode 0600."""
    key_path = tmp_path / "broker.key"
    signer1 = TokenSigner.load_or_create(key_path)
    signer2 = TokenSigner.load_or_create(key_path)

    # Key file has mode 0600
    mode = key_path.stat().st_mode & 0o777
    assert mode == 0o600

    # Parent directory has mode 0700
    parent_mode = tmp_path.stat().st_mode & 0o777
    assert parent_mode == 0o700

    # Same key works for both signers
    token = signer1.mint(claims)
    assert signer2.verify(token) == claims


@pytest.mark.parametrize("size", [0, 31, 33])
def test_load_or_create_rejects_key_of_wrong_length(tmp_path: Path, size: int) -> None:
    """A key file that is not exactly 32 bytes stops the broker instead of signing with it."""
    key_path = tmp_path / "token.key"
    key_path.write_bytes(b"\xab" * size)

    with pytest.raises(ValueError, match=r"token\.key") as exc_info:
        TokenSigner.load_or_create(key_path)

    # The error names the file but never echoes its contents.
    assert "ab" not in str(exc_info.value).replace(str(tmp_path), "")
    assert key_path.read_bytes() == b"\xab" * size


def test_constructor_rejects_key_of_wrong_length() -> None:
    """An empty key would let anyone forge tokens, so the signer refuses it."""
    with pytest.raises(ValueError, match="32 bytes"):
        TokenSigner(b"")


def test_load_or_create_leaves_no_partial_key_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed first write publishes nothing, so the next start creates a fresh full key."""
    key_path = tmp_path / "token.key"

    def fail_fsync(_fd: int) -> None:
        msg = "disk full"
        raise OSError(msg)

    with monkeypatch.context() as patch:
        patch.setattr("os.fsync", fail_fsync)
        with pytest.raises(OSError, match="disk full"):
            TokenSigner.load_or_create(key_path)

    assert list(tmp_path.iterdir()) == []

    TokenSigner.load_or_create(key_path)
    assert len(key_path.read_bytes()) == 32
    assert key_path.stat().st_mode & 0o777 == 0o600


def _worker_target(
    identity: ToolExecutionIdentity | None,
    *,
    worker_key: str | None = "worker_abc",
    tenant_id: str | None = None,
    account_id: str | None = None,
) -> ResolvedWorkerTarget:
    return ResolvedWorkerTarget(
        worker_scope="user",
        routing_agent_name="router",
        execution_identity=identity,
        tenant_id=tenant_id,
        account_id=account_id,
        worker_key=worker_key,
    )


def test_from_worker_target_drops_room_thread_session() -> None:
    """from_worker_target drops room_id, thread_id, session_id."""
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="agent_1",
        requester_id="@user:example.com",
        room_id="!room:example.com",
        thread_id="$thread",
        resolved_thread_id="$resolved",
        session_id="session_123",
        tenant_id="tenant_1",
        account_id="account_1",
    )
    other_conversation = replace(
        identity,
        room_id="!different:example.com",
        thread_id="$different",
        session_id="different_session",
    )

    ids = {"tenant_id": "tenant_1", "account_id": "account_1"}
    claims1 = WorkerClaims.from_worker_target(_worker_target(identity, **ids))
    claims2 = WorkerClaims.from_worker_target(_worker_target(other_conversation, **ids))
    assert claims1 == claims2


def test_to_worker_target_roundtrip_preserves_scope_agent_requester() -> None:
    """to_worker_target roundtrip preserves scope, agent, and requester."""
    original_target = ResolvedWorkerTarget(
        worker_scope="user_agent",
        routing_agent_name="router",
        execution_identity=ToolExecutionIdentity(
            channel="mcp",
            agent_name="agent_2",
            requester_id="user_123",
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id="tenant_2",
            account_id="account_2",
        ),
        tenant_id="tenant_2",
        account_id="account_2",
        worker_key="worker_xyz",
    )

    claims = WorkerClaims.from_worker_target(original_target)
    assert claims is not None

    # The original has no room, thread, session, or private agent names, so the rebuilt target equals it.
    assert claims.to_worker_target() == original_target


@pytest.mark.parametrize(("worker_scope", "label"), [("user", "user"), (None, "unscoped")])
def test_scope_label(claims: WorkerClaims, worker_scope: Literal["user"] | None, label: str) -> None:
    """scope_label returns worker_scope or unscoped."""
    assert replace(claims, worker_scope=worker_scope).scope_label == label


_BARE_IDENTITY = ToolExecutionIdentity(
    channel="matrix",
    agent_name="agent",
    requester_id="@user:example.com",
    room_id=None,
    thread_id=None,
    resolved_thread_id=None,
    session_id=None,
)


@pytest.mark.parametrize(
    ("identity", "worker_key"),
    [
        pytest.param(_BARE_IDENTITY, None, id="worker-key-none"),
        pytest.param(None, "worker_abc", id="execution-identity-none"),
    ],
)
def test_from_worker_target_returns_none_without_a_worker_key_or_identity(
    identity: ToolExecutionIdentity | None,
    worker_key: str | None,
) -> None:
    """from_worker_target returns None when worker_key or execution_identity is None."""
    assert WorkerClaims.from_worker_target(_worker_target(identity, worker_key=worker_key)) is None
