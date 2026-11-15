"""Tests for the pending secret request registry and card content."""

from __future__ import annotations

import pytest

from mindroom import secret_requests
from mindroom.secret_requests import (
    SECRET_REQUEST_TTL_SECONDS,
    _SecretRequestRegistry,
    secret_request_body,
    secret_request_content,
    secret_request_registry,
)
from tests.egress_broker.conftest import worker_target

ROOM = "!room:example.org"


def _register(registry: _SecretRequestRegistry, *, now: float = 1000.0) -> secret_requests.PendingSecretRequest:
    return registry.register(
        room_id=ROOM,
        requester_id="@alice:example.org",
        agent_name="code",
        service="github",
        worker_target=worker_target(),
        now=now,
    )


@pytest.mark.asyncio
async def test_claim_is_single_use() -> None:
    """A second claim of the same request is refused."""
    registry = _SecretRequestRegistry()
    request = _register(registry)
    assert registry.claim(request.request_id, room_id=ROOM, now=1001.0) is request
    assert registry.claim(request.request_id, room_id=ROOM, now=1001.0) is None


@pytest.mark.asyncio
async def test_release_lets_a_failed_save_retry() -> None:
    """Releasing a claim makes the request claimable again."""
    registry = _SecretRequestRegistry()
    request = _register(registry)
    assert registry.claim(request.request_id, room_id=ROOM, now=1001.0) is request
    registry.release(request.request_id)
    assert registry.claim(request.request_id, room_id=ROOM, now=1002.0) is request


@pytest.mark.asyncio
async def test_claim_refuses_other_room_and_expired() -> None:
    """Unknown, other-room, expired and finished requests cannot be claimed."""
    registry = _SecretRequestRegistry()
    request = _register(registry)
    assert request.expires_at == 1000.0 + SECRET_REQUEST_TTL_SECONDS
    assert registry.claim("unknown", room_id=ROOM, now=1001.0) is None
    assert registry.claim(request.request_id, room_id="!other:example.org", now=1001.0) is None
    assert registry.claim(request.request_id, room_id=ROOM, now=request.expires_at) is None
    done = _register(registry)
    registry.resolve(done.request_id, "declined")
    assert registry.claim(done.request_id, room_id=ROOM, now=1001.0) is None


@pytest.mark.asyncio
async def test_resolve_sets_outcome_once() -> None:
    """The first outcome wins and later ones are ignored."""
    registry = _SecretRequestRegistry()
    request = _register(registry)
    registry.resolve(request.request_id, "saved")
    registry.resolve(request.request_id, "expired")
    assert request.outcome.result() == "saved"
    registry.resolve("unknown", "saved")


@pytest.mark.asyncio
async def test_discard_forgets_the_request() -> None:
    """A discarded request is gone."""
    registry = _SecretRequestRegistry()
    request = _register(registry)
    registry.discard(request.request_id)
    registry.discard(request.request_id)
    assert registry.claim(request.request_id, room_id=ROOM, now=1001.0) is None


@pytest.mark.asyncio
async def test_ttl_is_read_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkeypatching the module constant changes the next expiry."""
    monkeypatch.setattr(secret_requests, "SECRET_REQUEST_TTL_SECONDS", 5)
    assert _register(_SecretRequestRegistry()).expires_at == 1005.0


def test_registry_is_a_singleton() -> None:
    """The accessor returns one shared registry."""
    assert secret_request_registry() is secret_request_registry()


@pytest.mark.asyncio
async def test_content_carries_metadata_only() -> None:
    """The card content has exactly the documented keys and millisecond expiry."""
    request = _register(_SecretRequestRegistry())
    content = secret_request_content(
        request,
        display_name="GitHub",
        hosts=["api.github.com"],
        reason="to open a PR",
        status="pending",
    )
    assert set(content) == {
        "request_id",
        "requester_id",
        "service",
        "display_name",
        "hosts",
        "reason",
        "status",
        "expires_at",
    }
    assert content["expires_at"] == int(request.expires_at * 1000)
    assert content["hosts"] == ["api.github.com"]


def test_bodies() -> None:
    """Each status has its body text."""
    kwargs = {"agent_display_name": "Code", "display_name": "GitHub"}
    assert (
        secret_request_body("pending", **kwargs) == "🔑 Code asks for your GitHub key. Open MindRoom Chat to enter it."
    )
    assert secret_request_body("saved", **kwargs) == "🔑 GitHub key saved."
    assert secret_request_body("declined", **kwargs) == "🔑 GitHub key request declined."
    assert secret_request_body("expired", **kwargs) == "🔑 GitHub key request expired."
