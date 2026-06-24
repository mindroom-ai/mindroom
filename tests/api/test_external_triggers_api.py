"""API tests for signed external trigger ingress."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import config_lifecycle
from mindroom.api import external_triggers as external_triggers_api
from mindroom.api import main as api_main
from mindroom.config.main import Config
from mindroom.external_triggers.auth import sign_trigger_request
from mindroom.external_triggers.store import ExternalTriggerStore, ExternalTriggerTarget, TriggerDeliverySnapshot

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import Response

_OWNER = "@owner:example.org"


@dataclass(frozen=True)
class TriggerApiContext:
    """Test runtime for one signed trigger API app."""

    client: TestClient
    private_key: Ed25519PrivateKey
    runtime_paths: constants.RuntimePaths
    ready_snapshots: list[TriggerDeliverySnapshot]


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
    timestamp: str | None = None,
) -> dict[str, str]:
    return sign_trigger_request(
        method="POST",
        path=f"/api/triggers/{trigger_id}",
        body=body,
        key_id="campground-main",
        timestamp=timestamp or str(int(time.time())),
        nonce=nonce,
        private_key=private_key,
    )


def _config_payload(*, max_body_bytes: int = 262144, owner_authorized: bool = True) -> dict[str, object]:
    authorization: dict[str, object] = {"agent_reply_permissions": {"*": [_OWNER]}}
    if owner_authorized:
        authorization["global_users"] = [_OWNER]
    return {
        "models": {"default": {"provider": "openai", "id": "gpt-5.5"}},
        "router": {"model": "default"},
        "agents": {
            "research": {
                "display_name": "Research",
                "role": "test",
                "rooms": ["campground"],
            },
        },
        "rooms": {"campground": {"display_name": "Campground"}},
        "external_trigger_policy": {
            "default_max_body_bytes": min(max_body_bytes, 65536),
            "max_body_bytes": max_body_bytes,
        },
        "authorization": authorization,
    }


def _write_runtime_config(
    config_path: Path,
    *,
    max_body_bytes: int = 262144,
    owner_authorized: bool = True,
    research_rooms: list[str] | None = None,
) -> Config:
    """Write normal MindRoom config; trigger records live in the trigger store."""
    payload = _config_payload(max_body_bytes=max_body_bytes, owner_authorized=owner_authorized)
    if research_rooms is not None:
        agents = cast("dict[str, dict[str, object]]", payload["agents"])
        agents["research"]["rooms"] = research_rooms
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return Config.model_validate(payload)


def _create_record(runtime_paths: constants.RuntimePaths, config: Config, public_key: str) -> None:
    ExternalTriggerStore(runtime_paths).create_record(
        trigger_id="campground",
        owner_user_id=_OWNER,
        created_by_agent_name="research",
        created_in_room_id="campground",
        created_in_thread_id="$thread-root",
        target=ExternalTriggerTarget(room_id="campground", thread_id="$thread-root", agent="research"),
        public_key=public_key,
        key_id="campground-main",
        allowed_kinds=["campground.availability"],
        replay_window_seconds=30,
        max_body_bytes=65536,
        config=config,
    )


def _bind_runtime(ready_snapshots: list[TriggerDeliverySnapshot]) -> object:
    client = object()

    async def is_trigger_snapshot_ready(snapshot: TriggerDeliverySnapshot) -> bool:
        ready_snapshots.append(snapshot)
        return True

    api_main.bind_external_trigger_runtime(
        api_main.app,
        client=client,
        conversation_cache=object(),
        is_trigger_snapshot_ready=is_trigger_snapshot_ready,
    )
    return client


def test_uninitialized_api_state_maps_to_external_trigger_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the API-state initialization sentinel should be converted to trigger-unavailable."""

    def raise_uninitialized(_request: Request) -> object:
        msg = "MindRoom app state is not initialized"
        raise TypeError(msg)

    monkeypatch.setattr(config_lifecycle, "bind_current_request_snapshot", raise_uninitialized)

    with pytest.raises(HTTPException) as exc_info:
        external_triggers_api._request_config_and_trigger_snapshot("campground", cast("Request", object()))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "External trigger configuration is not available"


def test_runtime_config_type_error_is_not_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Programming errors after snapshot binding should not become generic trigger 503s."""

    def raise_programming_error(_request: Request) -> tuple[Config, constants.RuntimePaths]:
        msg = "programming bug"
        raise TypeError(msg)

    monkeypatch.setattr(config_lifecycle, "bind_current_request_snapshot", lambda _request: object())
    monkeypatch.setattr(config_lifecycle, "read_committed_runtime_config", raise_programming_error)

    with pytest.raises(TypeError, match="programming bug"):
        external_triggers_api._request_config_and_trigger_snapshot("campground", cast("Request", object()))


async def _owner_joined(*_args: object, **_kwargs: object) -> bool:
    return True


async def _owner_not_joined(*_args: object, **_kwargs: object) -> bool:
    return False


@pytest.fixture
def trigger_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TriggerApiContext:
    """Return one initialized API app with a tool-managed trigger record."""
    private_key = Ed25519PrivateKey.generate()
    config_path = tmp_path / "config.yaml"
    config = _write_runtime_config(config_path)
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    api_main.initialize_api_app(api_main.app, runtime_paths)
    assert config_lifecycle.load_config_into_app(runtime_paths, api_main.app) is True
    _create_record(runtime_paths, config, _public_key_b64(private_key))
    api_main.unbind_external_trigger_runtime(api_main.app)
    ready_snapshots: list[TriggerDeliverySnapshot] = []
    _bind_runtime(ready_snapshots)
    monkeypatch.setattr("mindroom.api.external_triggers.is_external_trigger_owner_joined_target_room", _owner_joined)

    with TestClient(api_main.app) as client:
        yield TriggerApiContext(
            client=client,
            private_key=private_key,
            runtime_paths=runtime_paths,
            ready_snapshots=ready_snapshots,
        )

    api_main.unbind_external_trigger_runtime(api_main.app)


def _post_signed(
    trigger_api: TriggerApiContext,
    *,
    body: bytes | None = None,
    nonce: str = "nonce-1",
    trigger_id: str = "campground",
) -> Response:
    signed_body = body or _body()
    return trigger_api.client.post(
        f"/api/triggers/{trigger_id}",
        content=signed_body,
        headers=_sign(trigger_api.private_key, trigger_id=trigger_id, body=signed_body, nonce=nonce),
    )


def test_unknown_trigger_returns_404(trigger_api: TriggerApiContext) -> None:
    """Unknown trigger IDs are not authenticated as real endpoints."""
    response = _post_signed(trigger_api, trigger_id="missing")

    assert response.status_code == 404


def test_trigger_invalidated_by_current_config_returns_404_before_replay_claim(
    trigger_api: TriggerApiContext,
) -> None:
    """Stored triggers that no longer target configured rooms are hidden as not found."""
    runtime_paths = trigger_api.runtime_paths
    config = _write_runtime_config(runtime_paths.config_path, research_rooms=[])
    assert config_lifecycle._publish_runtime_config_into_app(config, runtime_paths, api_main.app)

    response = _post_signed(trigger_api, nonce="stale-target")

    assert response.status_code == 404
    assert response.json()["detail"] == "External trigger not found"
    assert not (runtime_paths.control_state_root / "external_triggers" / "replay.json").exists()


def test_missing_signature_headers_return_401(trigger_api: TriggerApiContext) -> None:
    """Configured triggers require signature headers."""
    response = trigger_api.client.post("/api/triggers/campground", content=_body())

    assert response.status_code == 401


def test_tampered_signed_body_returns_401(trigger_api: TriggerApiContext) -> None:
    """HTTP boundary should reject bodies changed after signing."""
    signed_body = _body(message="Site 42 opened.")
    tampered_body = _body(message="Site 43 opened.")

    response = trigger_api.client.post(
        "/api/triggers/campground",
        content=tampered_body,
        headers=_sign(trigger_api.private_key, body=signed_body),
    )

    assert response.status_code == 401


def test_forged_signature_returns_401(trigger_api: TriggerApiContext) -> None:
    """HTTP boundary should reject signatures from an unconfigured key."""
    body = _body()

    response = trigger_api.client.post(
        "/api/triggers/campground",
        content=body,
        headers=_sign(Ed25519PrivateKey.generate(), body=body),
    )

    assert response.status_code == 401


def test_expired_signature_returns_401(trigger_api: TriggerApiContext) -> None:
    """HTTP boundary should reject signed requests outside the replay window."""
    body = _body()

    response = trigger_api.client.post(
        "/api/triggers/campground",
        content=body,
        headers=_sign(trigger_api.private_key, body=body, timestamp=str(int(time.time()) - 31)),
    )

    assert response.status_code == 401


def test_reused_nonce_returns_409(
    trigger_api: TriggerApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP boundary should reject exact nonce reuse before dispatching again."""

    async def execute_external_trigger(**_kwargs: object) -> str:
        return "$matrix-event"

    monkeypatch.setattr("mindroom.api.external_triggers.execute_external_trigger", execute_external_trigger)

    first = _post_signed(trigger_api, nonce="same-nonce")
    second = _post_signed(trigger_api, nonce="same-nonce")

    assert first.status_code == 202
    assert second.status_code == 409


def test_disallowed_kind_returns_422(trigger_api: TriggerApiContext) -> None:
    """HTTP boundary should reject payload kinds outside the trigger allowlist."""
    response = _post_signed(trigger_api, body=_body(kind="campground.closed"), nonce="wrong-kind")

    assert response.status_code == 422


def test_delivery_uses_single_snapshot_for_auth_readiness_and_execute(
    trigger_api: TriggerApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same delivery snapshot is used for readiness and execution."""
    execute_snapshots: list[TriggerDeliverySnapshot] = []

    async def execute_external_trigger(**kwargs: object) -> str:
        snapshot = kwargs["snapshot"]
        assert isinstance(snapshot, TriggerDeliverySnapshot)
        execute_snapshots.append(snapshot)
        return "$matrix-event"

    monkeypatch.setattr("mindroom.api.external_triggers.execute_external_trigger", execute_external_trigger)

    response = _post_signed(trigger_api)

    assert response.status_code == 202
    assert response.json()["matrix_event_id"] == "$matrix-event"
    assert trigger_api.ready_snapshots
    assert execute_snapshots[0] is trigger_api.ready_snapshots[0]
    assert execute_snapshots[0].owner_user_id == _OWNER


def test_policy_caps_apply_at_request_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lowering policy caps after creation limits later trigger requests."""
    private_key = Ed25519PrivateKey.generate()
    config_path = tmp_path / "config.yaml"
    initial_config = _write_runtime_config(config_path, max_body_bytes=262144)
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    api_main.initialize_api_app(api_main.app, runtime_paths)
    assert config_lifecycle.load_config_into_app(runtime_paths, api_main.app) is True
    _create_record(runtime_paths, initial_config, _public_key_b64(private_key))

    lowered_config = _write_runtime_config(config_path, max_body_bytes=1024)
    assert config_lifecycle._publish_runtime_config_into_app(lowered_config, runtime_paths, api_main.app)
    ready_snapshots: list[TriggerDeliverySnapshot] = []
    _bind_runtime(ready_snapshots)
    monkeypatch.setattr("mindroom.api.external_triggers.is_external_trigger_owner_joined_target_room", _owner_joined)

    with TestClient(api_main.app) as client:
        body = _body(message="x" * 2000)
        response = client.post(
            "/api/triggers/campground",
            content=body,
            headers=_sign(private_key, body=body),
        )

    assert response.status_code == 413
    api_main.unbind_external_trigger_runtime(api_main.app)


def test_owner_permission_removed_blocks_delivery_before_replay_claim(
    trigger_api: TriggerApiContext,
) -> None:
    """Current authorization is checked before replay state is touched."""
    runtime_paths = trigger_api.runtime_paths
    config = _write_runtime_config(runtime_paths.config_path, owner_authorized=False)
    assert config_lifecycle._publish_runtime_config_into_app(config, runtime_paths, api_main.app)
    _bind_runtime(trigger_api.ready_snapshots)

    response = _post_signed(trigger_api)

    assert response.status_code == 403
    assert not (runtime_paths.control_state_root / "external_triggers" / "replay.json").exists()


def test_owner_not_joined_blocks_delivery_before_replay_claim(
    trigger_api: TriggerApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live owner room membership is checked before replay state is touched."""
    monkeypatch.setattr(
        "mindroom.api.external_triggers.is_external_trigger_owner_joined_target_room",
        _owner_not_joined,
    )

    response = _post_signed(trigger_api)

    assert response.status_code == 403
    assert not (trigger_api.runtime_paths.control_state_root / "external_triggers" / "replay.json").exists()


def test_duplicate_event_id_returns_duplicate_response(
    trigger_api: TriggerApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delivered event ids are idempotent within one replay scope."""

    async def execute_external_trigger(**_kwargs: object) -> str:
        return "$matrix-event"

    monkeypatch.setattr("mindroom.api.external_triggers.execute_external_trigger", execute_external_trigger)

    first = _post_signed(trigger_api, nonce="nonce-1")
    second = _post_signed(trigger_api, nonce="nonce-2")

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["duplicate"] is True
