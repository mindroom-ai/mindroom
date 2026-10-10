"""Tests for worker proxy token signing and verification."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - Required at runtime for test fixtures

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


def test_expired_token_rejected(signer: TokenSigner, claims: WorkerClaims) -> None:
    """Token expired beyond TTL is rejected."""
    assert signer.verify(signer.mint(claims, now=1000), now=1000 + 604801) is None


def test_token_valid_until_ttl(signer: TokenSigner, claims: WorkerClaims) -> None:
    """Token is valid until TTL."""
    assert signer.verify(signer.mint(claims, now=1000), now=1000 + 604799) == claims


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
    ],
)
def test_malformed_rejected(signer: TokenSigner, token: str) -> None:
    """Malformed tokens are rejected."""
    assert signer.verify(token) is None


def test_load_or_create_persists_key_with_0600(tmp_path: Path) -> None:
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
    claims = WorkerClaims(
        worker_key="test",
        worker_scope=None,
        routing_agent_name=None,
        tenant_id=None,
        account_id=None,
        channel="matrix",
        agent_name="agent",
        requester_id=None,
    )
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
    target1 = ResolvedWorkerTarget(
        worker_scope="user",
        routing_agent_name="router",
        execution_identity=identity,
        tenant_id="tenant_1",
        account_id="account_1",
        worker_key="worker_abc",
    )
    target2 = ResolvedWorkerTarget(
        worker_scope="user",
        routing_agent_name="router",
        execution_identity=ToolExecutionIdentity(
            channel="matrix",
            agent_name="agent_1",
            requester_id="@user:example.com",
            room_id="!different:example.com",  # different room_id
            thread_id="$different",  # different thread_id
            resolved_thread_id="$resolved",
            session_id="different_session",  # different session_id
            tenant_id="tenant_1",
            account_id="account_1",
        ),
        tenant_id="tenant_1",
        account_id="account_1",
        worker_key="worker_abc",
    )

    claims1 = WorkerClaims.from_worker_target(target1)
    claims2 = WorkerClaims.from_worker_target(target2)
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

    rebuilt_target = claims.to_worker_target()

    # These should be preserved
    assert rebuilt_target.worker_scope == original_target.worker_scope
    assert rebuilt_target.routing_agent_name == original_target.routing_agent_name
    assert rebuilt_target.tenant_id == original_target.tenant_id
    assert rebuilt_target.account_id == original_target.account_id
    assert rebuilt_target.worker_key == original_target.worker_key

    # Execution identity fields should be preserved except room/thread/session
    assert rebuilt_target.execution_identity is not None
    orig_identity = original_target.execution_identity
    assert orig_identity is not None
    rebuilt_identity = rebuilt_target.execution_identity
    assert rebuilt_identity.channel == orig_identity.channel
    assert rebuilt_identity.agent_name == orig_identity.agent_name
    assert rebuilt_identity.requester_id == orig_identity.requester_id
    assert rebuilt_identity.tenant_id == orig_identity.tenant_id
    assert rebuilt_identity.account_id == orig_identity.account_id

    # These should be None
    assert rebuilt_identity.room_id is None
    assert rebuilt_identity.thread_id is None
    assert rebuilt_identity.resolved_thread_id is None
    assert rebuilt_identity.session_id is None

    # private_agent_names should be None (per spec)
    assert rebuilt_target.private_agent_names is None


def test_scope_label() -> None:
    """scope_label returns worker_scope or unscoped."""
    claims_with_scope = WorkerClaims(
        worker_key="key",
        worker_scope="user",
        routing_agent_name=None,
        tenant_id=None,
        account_id=None,
        channel="matrix",
        agent_name="agent",
        requester_id=None,
    )
    assert claims_with_scope.scope_label == "user"

    claims_unscoped = WorkerClaims(
        worker_key="key",
        worker_scope=None,
        routing_agent_name=None,
        tenant_id=None,
        account_id=None,
        channel="matrix",
        agent_name="agent",
        requester_id=None,
    )
    assert claims_unscoped.scope_label == "unscoped"


def test_from_worker_target_returns_none_when_worker_key_is_none() -> None:
    """from_worker_target returns None when worker_key is None."""
    target = ResolvedWorkerTarget(
        worker_scope="user",
        routing_agent_name="router",
        execution_identity=ToolExecutionIdentity(
            channel="matrix",
            agent_name="agent",
            requester_id="@user:example.com",
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
        ),
        tenant_id=None,
        account_id=None,
        worker_key=None,  # None worker_key
    )
    assert WorkerClaims.from_worker_target(target) is None


def test_from_worker_target_returns_none_when_execution_identity_is_none() -> None:
    """from_worker_target returns None when execution_identity is None."""
    target = ResolvedWorkerTarget(
        worker_scope="user",
        routing_agent_name="router",
        execution_identity=None,  # None execution_identity
        tenant_id=None,
        account_id=None,
        worker_key="worker_abc",
    )
    assert WorkerClaims.from_worker_target(target) is None
