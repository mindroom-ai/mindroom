"""Tests for the worker egress policy resolver."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mindroom.egress.policy import (
    EgressGrantSubject,
    WorkerEgressPolicy,
    canonical_hostname,
    is_hostname_allowed,
    load_static_allowlist,
    resolve_grant_subject,
    resolve_worker_egress_policy,
)
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    resolve_worker_key,
    resolve_worker_target,
)
from mindroom.tools import approved_egress as approved_egress_module

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.worker_routing import WorkerScope


@pytest.fixture(autouse=True)
def _egress_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        "MINDROOM_APPROVED_EGRESS_ALLOWLIST",
        "MINDROOM_EGRESS_ALLOWLIST_PATH",
        "MINDROOM_APPROVED_EGRESS_API_URL",
        "MINDROOM_APPROVED_EGRESS_TOKEN",
        "MINDROOM_APPROVED_EGRESS_MAX_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    # Keep tests hermetic on hosts that mount a real allowlist at the default path.
    monkeypatch.setenv("MINDROOM_APPROVED_EGRESS_ALLOWLIST_PATH", str(tmp_path / "missing-allowlist.txt"))


def _identity(requester_id: str | None = "@user:server") -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="assistant",
        requester_id=requester_id,
        room_id="!room:server",
        thread_id="$thread",
        resolved_thread_id=None,
        session_id=None,
        tenant_id="tenant-a",
        account_id=None,
    )


def test_load_static_allowlist_from_inline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inline env entries split on commas, drop comments, and deduplicate in order."""
    monkeypatch.setenv(
        "MINDROOM_APPROVED_EGRESS_ALLOWLIST",
        "docs.example.com, .api.example.com,docs.example.com,# comment",
    )

    assert load_static_allowlist() == ("docs.example.com", ".api.example.com")


def test_load_static_allowlist_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """File entries drop comments and blank lines."""
    allowlist_path = tmp_path / "allowed-domains.txt"
    allowlist_path.write_text("docs.example.com\n# comment\n\n.api.example.com  # trailing\n", encoding="utf-8")
    monkeypatch.setenv("MINDROOM_APPROVED_EGRESS_ALLOWLIST_PATH", str(allowlist_path))

    assert load_static_allowlist() == ("docs.example.com", ".api.example.com")


def test_load_static_allowlist_falls_back_to_legacy_path_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """MINDROOM_EGRESS_ALLOWLIST_PATH backs the proxy-shared mount when the tool env is unset."""
    allowlist_path = tmp_path / "allowed-domains.txt"
    allowlist_path.write_text("docs.example.com\n", encoding="utf-8")
    monkeypatch.delenv("MINDROOM_APPROVED_EGRESS_ALLOWLIST_PATH", raising=False)
    monkeypatch.setenv("MINDROOM_EGRESS_ALLOWLIST_PATH", str(allowlist_path))

    assert load_static_allowlist() == ("docs.example.com",)


def test_load_static_allowlist_empty_when_file_missing() -> None:
    """A missing allowlist file resolves to the empty allowlist."""
    assert load_static_allowlist() == ()


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("Docs.Example.COM.", "docs.example.com"),
        ("münchen.example", "xn--mnchen-3ya.example"),
    ],
)
def test_canonical_hostname_normalizes_case_trailing_dot_and_idna(hostname: str, expected: str) -> None:
    """Hostnames should canonicalize to lowercase ASCII DNS names."""
    assert canonical_hostname(hostname) == expected


@pytest.mark.parametrize(
    ("hostname", "match"),
    [
        ("", "must not be empty"),
        ("https://docs.example.com", "scheme, path, query, or credentials"),
        ("docs.example.com/path", "scheme, path, query, or credentials"),
        ("user@docs.example.com", "scheme, path, query, or credentials"),
        ("docs.example.com:443", "must not include a port"),
        ("*.example.com", "wildcards are not supported"),
        ("203.0.113.7", "IP literals are not valid"),
        ("example", "fully-qualified external DNS name"),
        ("localhost", "fully-qualified external DNS name"),
        ("metadata.google.internal", "points at an internal name"),
        ("foo.svc.cluster.local", "points at an internal name"),
        ("api.svc", "points at an internal name"),
        ("foo.localhost", "points at an internal name"),
        ("bad..example.com", "not valid IDNA"),
        (f"{'a' * 64}.example.com", "not valid IDNA"),
        ("-bad.example.com", "not a valid DNS name"),
        ("foo_bar.example.com", "contains unsupported characters"),
    ],
)
def test_canonical_hostname_rejects_invalid_and_internal_names(hostname: str, match: str) -> None:
    """The hostname gate should reject malformed, internal, and non-DNS inputs."""
    with pytest.raises(ValueError, match=match):
        canonical_hostname(hostname)


@pytest.mark.parametrize(
    ("entries", "hostname", "expected"),
    [
        (("docs.example.com",), "docs.example.com", True),
        (("docs.example.com",), "sub.docs.example.com", False),
        ((".example.com",), "example.com", True),
        ((".example.com",), "deep.sub.example.com", True),
        ((".example.com",), "badexample.com", False),
        (("*.example.com", "docs.example.com"), "docs.example.com", True),
        (("*.example.com",), "anything.example.com", False),
        (("Docs.Example.COM.",), "docs.example.com", True),
        ((".MüNCHEN.example",), "sub.xn--mnchen-3ya.example", True),
        ((), "docs.example.com", False),
    ],
)
def test_is_hostname_allowed_static_matching(entries: tuple[str, ...], hostname: str, expected: bool) -> None:
    """Static matching supports exact entries, leading-dot suffix entries, and skips invalid entries."""
    policy = WorkerEgressPolicy(static_allowlist=entries)

    assert is_hostname_allowed(hostname, policy) is expected


def test_resolve_policy_static_only_without_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolving without an agent yields the allowlist and no grant subject."""
    monkeypatch.setenv("MINDROOM_APPROVED_EGRESS_ALLOWLIST", ".example.com")

    policy = resolve_worker_egress_policy()

    assert policy == WorkerEgressPolicy(static_allowlist=(".example.com",), grant_subject=None)


def test_resolve_policy_empty_inputs() -> None:
    """No allowlist sources and no agent resolve to the empty policy."""
    assert resolve_worker_egress_policy() == WorkerEgressPolicy(static_allowlist=(), grant_subject=None)


@pytest.mark.parametrize("worker_scope", [None, "shared"])
def test_resolve_policy_agent_subject_for_non_isolating_scopes(worker_scope: WorkerScope | None) -> None:
    """Shared and unscoped agents receive agent-addressed grants."""
    policy = resolve_worker_egress_policy(
        agent_name="assistant",
        worker_scope=worker_scope,
        execution_identity=None,
    )

    assert policy.grant_subject == EgressGrantSubject(subject_type="agent", subject="assistant")


def test_resolve_policy_worker_key_subject_for_user_agent_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """user_agent scope addresses grants to the resolved worker key."""
    monkeypatch.setenv("MINDROOM_APPROVED_EGRESS_ALLOWLIST", "docs.example.com")
    identity = _identity()

    policy = resolve_worker_egress_policy(
        agent_name="assistant",
        worker_scope="user_agent",
        execution_identity=identity,
    )

    expected_key = resolve_worker_key("user_agent", identity, agent_name="assistant")
    assert expected_key is not None
    assert policy.static_allowlist == ("docs.example.com",)
    assert policy.grant_subject == EgressGrantSubject(subject_type="worker_key", subject=expected_key)


def test_resolve_grant_subject_rejects_user_scope() -> None:
    """worker_scope=user has no per-worker grant subject."""
    with pytest.raises(RuntimeError, match="not supported for worker_scope=user"):
        resolve_grant_subject(agent_name="assistant", worker_scope="user", execution_identity=_identity())


@pytest.mark.parametrize("execution_identity", [None, _identity(requester_id=None)])
def test_resolve_grant_subject_requires_resolvable_user_agent_key(
    execution_identity: ToolExecutionIdentity | None,
) -> None:
    """user_agent scope fails closed when the worker key cannot be resolved."""
    with pytest.raises(RuntimeError, match="could not resolve the user-agent worker key"):
        resolve_grant_subject(
            agent_name="assistant",
            worker_scope="user_agent",
            execution_identity=execution_identity,
        )


def test_tool_and_worker_provisioning_resolve_the_same_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The grant subject the tool POSTs equals the worker key the provisioning path uses.

    This is the cross-layer egress boundary: a worker_key grant created via the
    approved-egress tool only takes effect if it addresses the same worker key
    that worker provisioning (sandbox routing -> WorkerSpec -> the Kubernetes
    worker-key pod annotation) resolves for the same scope and identity.
    """
    identity = _identity()

    # Worker-provisioning path: sandbox routing resolves the worker target whose
    # worker_key reaches the backend and is stamped on the worker pod.
    provisioning_key = resolve_worker_target("user_agent", "assistant", identity).worker_key
    assert provisioning_key is not None

    # Policy-resolver path.
    policy = resolve_worker_egress_policy(
        agent_name="assistant",
        worker_scope="user_agent",
        execution_identity=identity,
    )
    assert policy.grant_subject == EgressGrantSubject(subject_type="worker_key", subject=provisioning_key)

    # Tool path: run the real request flow (real resolve_worker_key, no routing mocks)
    # and capture the grant payload it would POST to the policy service.
    captured: dict[str, object] = {}

    def post_grant(payload: dict[str, object]) -> dict[str, object]:
        captured.update(payload)
        return {"expires_at": 123}

    class Config:
        def get_agent_execution_scope(self, agent_name: str) -> str:
            del agent_name
            return "user_agent"

    class Context:
        agent_name = "assistant"
        room_id = "!room:server"
        resolved_thread_id = None
        thread_id = "$thread"
        requester_id = "@user:server"
        config = Config()

    monkeypatch.setattr(approved_egress_module, "_post_grant", post_grant)
    monkeypatch.setattr(approved_egress_module, "get_tool_runtime_context", lambda: Context())
    monkeypatch.setattr(
        approved_egress_module,
        "build_execution_identity_from_runtime_context",
        lambda _context: identity,
    )

    result = asyncio.run(
        approved_egress_module.approved_egress_tools()().request_network_access(
            "docs.example.com",
            5,
            "Need docs",
        ),
    )

    assert result.startswith("Approved temporary network access to docs.example.com")
    assert captured["subject_type"] == "worker_key"
    assert captured["subject"] == provisioning_key


def test_tool_static_allow_short_circuit_agrees_with_policy_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool's static-allow early return is exactly the policy allow-decision."""
    monkeypatch.setenv("MINDROOM_APPROVED_EGRESS_ALLOWLIST", ".example.com")

    assert is_hostname_allowed("docs.example.com", resolve_worker_egress_policy())

    def post_grant(_payload: dict[str, object]) -> dict[str, object]:
        msg = "static allowlist match should not create a grant"
        raise AssertionError(msg)

    monkeypatch.setattr(approved_egress_module, "_post_grant", post_grant)

    result = asyncio.run(
        approved_egress_module.approved_egress_tools()().request_network_access(
            "docs.example.com",
            5,
            "Need docs",
        ),
    )

    assert "docs.example.com is already allowed" in result
