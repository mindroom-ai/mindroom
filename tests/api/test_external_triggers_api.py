"""API tests for signed external trigger ingress."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import config_lifecycle
from mindroom.api import main as api_main
from mindroom.external_triggers.auth import sign_trigger_request
from mindroom.external_triggers.replay_store import ExternalTriggerEventClaim

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.external_triggers.models import ExternalTriggerPayload


@dataclass(frozen=True)
class TriggerApiContext:
    """Test runtime for one signed trigger API app."""

    client: TestClient
    private_key: Ed25519PrivateKey
    runtime_paths: constants.RuntimePaths


def _public_key_b64(private_key: Ed25519PrivateKey) -> str:
    public_key_bytes = private_key.public_key().public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )
    return base64.b64encode(public_key_bytes).decode("ascii")


def _body(**overrides: object) -> bytes:
    payload: dict[str, object] = {
        "kind": "campground.availability",
        "message": "Site 42 opened.",
        "event_id": "availability-42",
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _sign(
    private_key: Ed25519PrivateKey,
    *,
    trigger_id: str = "campground",
    body: bytes,
    nonce: str = "nonce-1",
) -> dict[str, str]:
    return sign_trigger_request(
        method="POST",
        path=f"/api/triggers/{trigger_id}",
        body=body,
        key_id="campground-main",
        timestamp=str(int(time.time())),
        nonce=nonce,
        private_key=private_key,
    )


def _write_config(config_path: Path, public_key: str) -> None:
    config = {
        "models": {
            "default": {
                "provider": "openai",
                "id": "gpt-5.5",
            },
        },
        "router": {
            "model": "default",
        },
        "agents": {
            "research": {
                "display_name": "Research",
                "role": "test",
                "rooms": [],
            },
        },
        "external_triggers": {
            "campground": {
                "key_id": "campground-main",
                "public_key": public_key,
                "allowed_kinds": ["campground.availability"],
                "max_body_bytes": 1024,
                "replay_window_seconds": 30,
                "target": {
                    "room_id": "!campground:example.org",
                    "thread_id": "$thread-root",
                    "agent": "research",
                },
            },
            "disabled": {
                "enabled": False,
                "key_id": "campground-main",
                "public_key": public_key,
                "target": {
                    "room_id": "!campground:example.org",
                    "agent": "research",
                },
            },
        },
    }
    config_path.write_text(yaml.dump(config), encoding="utf-8")


@pytest.fixture
def trigger_api(tmp_path: Path) -> TriggerApiContext:
    """Return one initialized API app with a signed trigger config."""
    private_key = Ed25519PrivateKey.generate()
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, _public_key_b64(private_key))
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    api_main.initialize_api_app(api_main.app, runtime_paths)
    assert config_lifecycle.load_config_into_app(runtime_paths, api_main.app) is True
    if hasattr(api_main, "unbind_external_trigger_runtime"):
        api_main.unbind_external_trigger_runtime(api_main.app)

    with TestClient(api_main.app) as client:
        yield TriggerApiContext(client=client, private_key=private_key, runtime_paths=runtime_paths)

    if hasattr(api_main, "unbind_external_trigger_runtime"):
        api_main.unbind_external_trigger_runtime(api_main.app)


def _bind_runtime() -> object:
    client = object()
    api_main.bind_external_trigger_runtime(api_main.app, client=client, conversation_cache=object())
    return client


def test_unknown_trigger_returns_404(trigger_api: TriggerApiContext) -> None:
    """Unknown trigger IDs are not authenticated as real endpoints."""
    body = _body()
    response = trigger_api.client.post(
        "/api/triggers/missing",
        content=body,
        headers=_sign(trigger_api.private_key, trigger_id="missing", body=body),
    )

    assert response.status_code == 404


def test_missing_signature_headers_return_401(trigger_api: TriggerApiContext) -> None:
    """Configured triggers require signature headers."""
    response = trigger_api.client.post("/api/triggers/campground", content=_body())

    assert response.status_code == 401


def test_body_limit_returns_413_before_auth_or_runtime(trigger_api: TriggerApiContext) -> None:
    """Oversized bodies are rejected before signature or runtime checks."""
    response = trigger_api.client.post("/api/triggers/campground", content=b"x" * 1025)

    assert response.status_code == 413


def test_disallowed_kind_returns_422(trigger_api: TriggerApiContext) -> None:
    """Kinds outside the trigger allowlist are validation errors."""
    body = _body(kind="other.kind")
    response = trigger_api.client.post(
        "/api/triggers/campground",
        content=body,
        headers=_sign(trigger_api.private_key, body=body),
    )

    assert response.status_code == 422


def test_missing_runtime_binding_returns_503_after_auth_succeeds(trigger_api: TriggerApiContext) -> None:
    """A valid signature reaches the runtime-binding gate."""
    body = _body()
    response = trigger_api.client.post(
        "/api/triggers/campground",
        content=body,
        headers=_sign(trigger_api.private_key, body=body),
    )

    assert response.status_code == 503


def test_executor_none_releases_event_claim_for_retry(
    trigger_api: TriggerApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Matrix delivery failure does not burn event idempotency state."""
    _bind_runtime()
    calls: list[str] = []

    async def execute_external_trigger(
        *,
        client: object,
        trigger_id: str,
        trigger: object,
        payload: ExternalTriggerPayload,
        config: object,
        runtime_paths: object,
        conversation_cache: object,
    ) -> str | None:
        del client, trigger_id, trigger, config, runtime_paths, conversation_cache
        calls.append(payload.event_id or "")
        if len(calls) == 1:
            return None
        return "$matrix-event"

    monkeypatch.setattr("mindroom.api.external_triggers.execute_external_trigger", execute_external_trigger)

    first_body = _body(event_id="availability-retry")
    first = trigger_api.client.post(
        "/api/triggers/campground",
        content=first_body,
        headers=_sign(trigger_api.private_key, body=first_body, nonce="nonce-fail"),
    )
    retry_body = _body(event_id="availability-retry")
    retry = trigger_api.client.post(
        "/api/triggers/campground",
        content=retry_body,
        headers=_sign(trigger_api.private_key, body=retry_body, nonce="nonce-retry"),
    )

    assert first.status_code == 502
    assert retry.status_code == 202
    assert retry.json()["duplicate"] is False
    assert retry.json()["matrix_event_id"] == "$matrix-event"
    assert calls == ["availability-retry", "availability-retry"]


def test_executor_exception_releases_event_claim_for_retry(
    trigger_api: TriggerApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An executor exception does not burn event idempotency state."""
    _bind_runtime()
    calls: list[str] = []

    async def execute_external_trigger(
        *,
        client: object,
        trigger_id: str,
        trigger: object,
        payload: ExternalTriggerPayload,
        config: object,
        runtime_paths: object,
        conversation_cache: object,
    ) -> str:
        del client, trigger_id, trigger, config, runtime_paths, conversation_cache
        calls.append(payload.event_id or "")
        if len(calls) == 1:
            message = "executor failed"
            raise RuntimeError(message)
        return "$matrix-event"

    monkeypatch.setattr("mindroom.api.external_triggers.execute_external_trigger", execute_external_trigger)

    first_body = _body(event_id="availability-exception-retry")
    with pytest.raises(RuntimeError, match="executor failed"):
        trigger_api.client.post(
            "/api/triggers/campground",
            content=first_body,
            headers=_sign(trigger_api.private_key, body=first_body, nonce="nonce-exception"),
        )

    retry_body = _body(event_id="availability-exception-retry")
    retry = trigger_api.client.post(
        "/api/triggers/campground",
        content=retry_body,
        headers=_sign(trigger_api.private_key, body=retry_body, nonce="nonce-retry"),
    )

    assert retry.status_code == 202
    assert retry.json()["duplicate"] is False
    assert retry.json()["matrix_event_id"] == "$matrix-event"
    assert calls == ["availability-exception-retry", "availability-exception-retry"]


def test_first_success_returns_accepted_duplicate_false(
    trigger_api: TriggerApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh accepted event returns duplicate false and Matrix event id."""
    runtime_client = _bind_runtime()
    captured_payloads: list[ExternalTriggerPayload] = []

    async def execute_external_trigger(
        *,
        client: object,
        trigger_id: str,
        trigger: object,
        payload: ExternalTriggerPayload,
        config: object,
        runtime_paths: object,
        conversation_cache: object,
    ) -> str:
        del trigger_id, trigger, config, runtime_paths, conversation_cache
        assert client is runtime_client
        captured_payloads.append(payload)
        return "$matrix-event"

    monkeypatch.setattr("mindroom.api.external_triggers.execute_external_trigger", execute_external_trigger)
    body = _body(event_id=None)

    response = trigger_api.client.post(
        "/api/triggers/campground",
        content=body,
        headers=_sign(trigger_api.private_key, body=body, nonce="nonce-event-id"),
    )

    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "duplicate": False,
        "trigger_id": "campground",
        "event_id": "nonce-event-id",
        "matrix_event_id": "$matrix-event",
    }
    assert [payload.event_id for payload in captured_payloads] == ["nonce-event-id"]


def test_event_id_idempotency_ttl_uses_one_day_not_signature_window(
    trigger_api: TriggerApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Event-id idempotency TTL is independent from short signature replay windows."""
    _bind_runtime()
    nonce_ttls: list[int] = []
    event_claim_ttls: list[int] = []
    delivered_ttls: list[int] = []

    class ReplayStore:
        """Capture replay TTLs used by the API route."""

        def __init__(self, tracking_root: Path) -> None:
            self.tracking_root = tracking_root

        def claim_nonce(self, trigger_id: str, nonce: str, *, now: int, ttl_seconds: int) -> bool:
            del trigger_id, nonce, now
            nonce_ttls.append(ttl_seconds)
            return True

        def claim_event_id(
            self,
            trigger_id: str,
            event_id: str,
            *,
            now: int,
            ttl_seconds: int,
        ) -> ExternalTriggerEventClaim:
            del trigger_id, event_id, now
            event_claim_ttls.append(ttl_seconds)
            return ExternalTriggerEventClaim.FRESH

        def mark_event_delivered(self, trigger_id: str, event_id: str, *, now: int, ttl_seconds: int) -> None:
            del trigger_id, event_id, now
            delivered_ttls.append(ttl_seconds)

    async def execute_external_trigger(
        *,
        client: object,
        trigger_id: str,
        trigger: object,
        payload: ExternalTriggerPayload,
        config: object,
        runtime_paths: object,
        conversation_cache: object,
    ) -> str:
        del client, trigger_id, trigger, payload, config, runtime_paths, conversation_cache
        return "$matrix-event"

    monkeypatch.setattr("mindroom.api.external_triggers.ExternalTriggerReplayStore", ReplayStore)
    monkeypatch.setattr("mindroom.api.external_triggers.execute_external_trigger", execute_external_trigger)
    body = _body(event_id="availability-ttl")

    response = trigger_api.client.post(
        "/api/triggers/campground",
        content=body,
        headers=_sign(trigger_api.private_key, body=body, nonce="nonce-ttl"),
    )

    assert response.status_code == 202
    assert nonce_ttls == [30]
    assert event_claim_ttls == [86400]
    assert delivered_ttls == [86400]


def test_delivered_event_id_duplicate_returns_accepted_without_second_execute(
    trigger_api: TriggerApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivered event id is accepted as a duplicate without redelivery."""
    _bind_runtime()
    call_count = 0

    async def execute_external_trigger(
        *,
        client: object,
        trigger_id: str,
        trigger: object,
        payload: ExternalTriggerPayload,
        config: object,
        runtime_paths: object,
        conversation_cache: object,
    ) -> str:
        del client, trigger_id, trigger, payload, config, runtime_paths, conversation_cache
        nonlocal call_count
        call_count += 1
        return "$matrix-event"

    monkeypatch.setattr("mindroom.api.external_triggers.execute_external_trigger", execute_external_trigger)
    body = _body(event_id="availability-duplicate")

    first = trigger_api.client.post(
        "/api/triggers/campground",
        content=body,
        headers=_sign(trigger_api.private_key, body=body, nonce="nonce-first"),
    )
    duplicate = trigger_api.client.post(
        "/api/triggers/campground",
        content=body,
        headers=_sign(trigger_api.private_key, body=body, nonce="nonce-second"),
    )

    assert first.status_code == 202
    assert duplicate.status_code == 202
    assert duplicate.json()["duplicate"] is True
    assert duplicate.json()["matrix_event_id"] is None
    assert call_count == 1


def test_initialize_api_app_clears_external_trigger_runtime_on_runtime_change(
    trigger_api: TriggerApiContext,
    tmp_path: Path,
) -> None:
    """Runtime rebinding clears stale external trigger Matrix clients."""
    _bind_runtime()
    assert config_lifecycle.app_state(api_main.app).external_trigger_runtime is not None

    other_config_path = tmp_path / "other" / "config.yaml"
    other_config_path.parent.mkdir()
    _write_config(other_config_path, _public_key_b64(trigger_api.private_key))
    other_runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=other_config_path,
        storage_path=tmp_path / "other" / "mindroom_data",
        process_env={},
    )

    api_main.initialize_api_app(api_main.app, other_runtime_paths)

    assert config_lifecycle.app_state(api_main.app).external_trigger_runtime is None


@pytest.mark.asyncio
async def test_concurrent_same_event_id_cannot_both_deliver(
    trigger_api: TriggerApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent requests for one event id cannot both reach delivery."""
    _bind_runtime()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    delivery_count = 0

    async def execute_external_trigger(
        *,
        client: object,
        trigger_id: str,
        trigger: object,
        payload: ExternalTriggerPayload,
        config: object,
        runtime_paths: object,
        conversation_cache: object,
    ) -> str:
        del client, trigger_id, trigger, payload, config, runtime_paths, conversation_cache
        nonlocal delivery_count
        delivery_count += 1
        first_entered.set()
        await release_first.wait()
        return "$matrix-event"

    monkeypatch.setattr("mindroom.api.external_triggers.execute_external_trigger", execute_external_trigger)
    body = _body(event_id="availability-in-progress")

    async def post(nonce: str) -> httpx.Response:
        transport = httpx.ASGITransport(app=api_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/api/triggers/campground",
                content=body,
                headers=_sign(trigger_api.private_key, body=body, nonce=nonce),
            )

    first_task = asyncio.create_task(post("nonce-first"))
    await asyncio.wait_for(first_entered.wait(), timeout=2)
    second = await post("nonce-second")
    release_first.set()
    first = await first_task

    assert first.status_code == 202
    assert second.status_code == 409
    assert delivery_count == 1


def test_signed_route_is_not_protected_by_dashboard_auth(
    trigger_api: TriggerApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signed trigger route does not require dashboard auth headers."""
    _bind_runtime()
    executed = False

    async def execute_external_trigger(
        *,
        client: object,
        trigger_id: str,
        trigger: object,
        payload: ExternalTriggerPayload,
        config: object,
        runtime_paths: object,
        conversation_cache: object,
    ) -> str:
        del client, trigger_id, trigger, payload, config, runtime_paths, conversation_cache
        nonlocal executed
        executed = True
        return "$matrix-event"

    monkeypatch.setattr("mindroom.api.external_triggers.execute_external_trigger", execute_external_trigger)
    body = _body(event_id="availability-public")

    response = trigger_api.client.post(
        "/api/triggers/campground",
        content=body,
        headers=_sign(trigger_api.private_key, body=body, nonce="nonce-public"),
    )

    assert response.status_code == 202
    assert executed is True
