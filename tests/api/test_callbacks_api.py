"""API tests for bearer-token callback ingress."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest
import yaml
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import config_lifecycle
from mindroom.api import main as api_main
from mindroom.callbacks.store import CallbackStore
from mindroom.config.main import Config

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from httpx import Response

    from mindroom.external_triggers.models import TriggerDeliveryReadiness

_OWNER = "@owner:example.org"


@dataclass(frozen=True)
class CallbackApiContext:
    """Test runtime for one bearer-token callback API app."""

    client: TestClient
    callback_id: str
    token: str
    runtime_paths: constants.RuntimePaths
    ready_checks: list[TriggerDeliveryReadiness]


def _config_payload(
    *,
    owner_authorized: bool = True,
    private_coder: bool = False,
    callback_policy: dict[str, object] | None = None,
) -> dict[str, object]:
    authorization: dict[str, object] = {"agent_reply_permissions": {"*": [_OWNER]}}
    if owner_authorized:
        authorization["global_users"] = [_OWNER]
    payload: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "gpt-5.6"}},
        "router": {"model": "default"},
        "agents": {
            "coder": {
                "display_name": "Coder",
                "role": "test",
                "rooms": ["workroom"],
            },
        },
        "rooms": {"workroom": {"display_name": "Workroom"}},
        "authorization": authorization,
    }
    if callback_policy is not None:
        payload["callback_policy"] = callback_policy
    if private_coder:
        agents = cast("dict[str, dict[str, object]]", payload["agents"])
        agents["coder"]["private"] = {"per": "user", "root": "coder_data"}
    return payload


def _write_runtime_config(
    config_path: Path,
    *,
    owner_authorized: bool = True,
    private_coder: bool = False,
    callback_policy: dict[str, object] | None = None,
) -> Config:
    payload = _config_payload(
        owner_authorized=owner_authorized,
        private_coder=private_coder,
        callback_policy=callback_policy,
    )
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return Config.model_validate(payload)


def _mint_record(
    runtime_paths: constants.RuntimePaths,
    config: Config,
    *,
    ttl_seconds: int = 3600,
    max_uses: int = 1,
) -> tuple[str, str]:
    record, token = CallbackStore(runtime_paths).mint_record(
        owner_user_id=_OWNER,
        created_by_agent_name="coder",
        created_in_room_id="workroom",
        created_in_thread_id="$thread-root",
        target_room_id="workroom",
        target_thread_id="$thread-root",
        target_agent="coder",
        label="issue-042 implementer",
        ttl_seconds=ttl_seconds,
        max_uses=max_uses,
        on_expiry="notify",
        config=config,
    )
    return record.callback_id, token


def _bind_runtime(ready_checks: list[TriggerDeliveryReadiness]) -> None:
    async def is_delivery_target_ready(readiness: TriggerDeliveryReadiness) -> bool:
        ready_checks.append(readiness)
        return True

    api_main.bind_external_trigger_runtime(
        api_main.app,
        client=object(),
        conversation_cache=object(),
        is_delivery_target_ready=is_delivery_target_ready,
    )


async def _owner_joined(*_args: object, **_kwargs: object) -> bool:
    return True


async def _owner_not_joined(*_args: object, **_kwargs: object) -> bool:
    return False


def _callback_api_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    private_coder: bool = False,
    max_uses: int = 1,
) -> Iterator[CallbackApiContext]:
    config_path = tmp_path / "config.yaml"
    config = _write_runtime_config(config_path, private_coder=private_coder)
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    api_main.initialize_api_app(api_main.app, runtime_paths)
    assert config_lifecycle.load_config_into_app(runtime_paths, api_main.app) is True
    callback_id, token = _mint_record(runtime_paths, config, max_uses=max_uses)
    api_main.unbind_external_trigger_runtime(api_main.app)
    ready_checks: list[TriggerDeliveryReadiness] = []
    _bind_runtime(ready_checks)
    monkeypatch.setattr("mindroom.api.callbacks.is_user_joined_room", _owner_joined)

    with TestClient(api_main.app) as client:
        yield CallbackApiContext(
            client=client,
            callback_id=callback_id,
            token=token,
            runtime_paths=runtime_paths,
            ready_checks=ready_checks,
        )

    api_main.unbind_external_trigger_runtime(api_main.app)


@pytest.fixture
def callback_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CallbackApiContext]:
    """Return one initialized API app with a minted callback record."""
    yield from _callback_api_context(tmp_path, monkeypatch)


@pytest.fixture
def private_callback_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CallbackApiContext]:
    """Return one initialized API app with a private target callback record."""
    yield from _callback_api_context(tmp_path, monkeypatch, private_coder=True)


@pytest.fixture
def multi_use_callback_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CallbackApiContext]:
    """Return one initialized API app with a two-use callback record."""
    yield from _callback_api_context(tmp_path, monkeypatch, max_uses=2)


def _fire(
    callback_api: CallbackApiContext,
    *,
    token: str | None = None,
    callback_id: str | None = None,
    body: dict[str, object] | None = None,
    raw_body: bytes | None = None,
) -> Response:
    payload = (
        raw_body if raw_body is not None else json.dumps(body or {"status": "done", "message": "all green"}).encode()
    )
    headers = {"Content-Type": "application/json"}
    resolved_token = callback_api.token if token is None else token
    if resolved_token:
        headers["Authorization"] = f"Bearer {resolved_token}"
    return callback_api.client.post(
        f"/api/callbacks/{callback_id or callback_api.callback_id}",
        content=payload,
        headers=headers,
    )


def _mock_execute(monkeypatch: pytest.MonkeyPatch, *, event_id: str | None = "$matrix-event") -> list[object]:
    executed_snapshots: list[object] = []

    async def execute_callback_fire(**kwargs: object) -> str | None:
        executed_snapshots.append(kwargs["snapshot"])
        return event_id

    monkeypatch.setattr("mindroom.api.callbacks.execute_callback_fire", execute_callback_fire)
    return executed_snapshots


def test_fire_success_consumes_use_and_returns_matrix_event(
    callback_api: CallbackApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid bearer fire dispatches once and reports the remaining budget."""
    executed = _mock_execute(monkeypatch)

    response = _fire(callback_api)

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "accepted": True,
        "callback_id": callback_api.callback_id,
        "uses_left": 0,
        "matrix_event_id": "$matrix-event",
    }
    assert callback_api.ready_checks
    assert callback_api.ready_checks[0].target_agent == "coder"
    assert executed


def test_second_fire_after_consumption_returns_410(
    callback_api: CallbackApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumed callback answers 410 Gone with a clear error."""
    _mock_execute(monkeypatch)

    first = _fire(callback_api)
    second = _fire(callback_api)

    assert first.status_code == 200
    assert second.status_code == 410
    assert "already been used" in second.json()["detail"]


def test_multi_use_callback_allows_progress_pings(
    multi_use_callback_api: CallbackApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callbacks minted with max_uses > 1 accept intermediate progress fires."""
    _mock_execute(monkeypatch)

    first = _fire(multi_use_callback_api, body={"status": "progress", "message": "halfway"})
    second = _fire(multi_use_callback_api, body={"status": "done", "message": "finished"})
    third = _fire(multi_use_callback_api)

    assert first.status_code == 200
    assert first.json()["uses_left"] == 1
    assert second.status_code == 200
    assert second.json()["uses_left"] == 0
    assert third.status_code == 410


def test_wrong_token_returns_404_without_leaking_state(
    callback_api: CallbackApiContext,
) -> None:
    """Bad tokens are indistinguishable from unknown callbacks."""
    response = _fire(callback_api, token="mrcb_wrong-token")  # noqa: S106 - intentionally invalid test token

    assert response.status_code == 404
    assert response.json()["detail"] == "Callback not found"


def test_missing_token_returns_404(callback_api: CallbackApiContext) -> None:
    """Requests without a bearer token never authenticate."""
    response = _fire(callback_api, token="")

    assert response.status_code == 404


def test_unknown_callback_returns_404(callback_api: CallbackApiContext) -> None:
    """Unknown callback IDs are not real endpoints."""
    response = _fire(callback_api, callback_id="cb_0000000000000000")

    assert response.status_code == 404


def test_expired_callback_returns_410(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired callback with a valid token answers 410 Gone."""
    context_iter = _callback_api_context(tmp_path, monkeypatch)
    callback_api = next(context_iter)
    monkeypatch.setattr("mindroom.api.callbacks.time.time", lambda: 4102444800.0)

    response = _fire(callback_api)

    assert response.status_code == 410
    assert "expired" in response.json()["detail"]
    with pytest.raises(StopIteration):
        next(context_iter)


def test_oversized_body_returns_413(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The callback_policy body cap applies at request time."""
    config_path = tmp_path / "config.yaml"
    config = _write_runtime_config(config_path, callback_policy={"max_body_bytes": 1024})
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    api_main.initialize_api_app(api_main.app, runtime_paths)
    assert config_lifecycle.load_config_into_app(runtime_paths, api_main.app) is True
    callback_id, token = _mint_record(runtime_paths, config)
    ready_checks: list[TriggerDeliveryReadiness] = []
    _bind_runtime(ready_checks)
    monkeypatch.setattr("mindroom.api.callbacks.is_user_joined_room", _owner_joined)

    with TestClient(api_main.app) as client:
        response = client.post(
            f"/api/callbacks/{callback_id}",
            content=json.dumps({"status": "done", "message": "x" * 2000}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )

    assert response.status_code == 413
    api_main.unbind_external_trigger_runtime(api_main.app)


def test_disabled_policy_hides_endpoint(
    callback_api: CallbackApiContext,
) -> None:
    """callback_policy.enabled=false makes the endpoint answer 404."""
    runtime_paths = callback_api.runtime_paths
    config = _write_runtime_config(runtime_paths.config_path, callback_policy={"enabled": False})
    assert config_lifecycle._publish_runtime_config_into_app(config, runtime_paths, api_main.app)

    response = _fire(callback_api)

    assert response.status_code == 404


def test_invalid_payload_returns_422(callback_api: CallbackApiContext) -> None:
    """Malformed fire payloads are rejected before any state changes."""
    response = _fire(callback_api, body={"status": "exploded", "message": "boom"})

    assert response.status_code == 422
    store = CallbackStore(callback_api.runtime_paths)
    record = store.get_record(callback_api.callback_id)
    assert record is not None
    assert record.uses_left == 1


def test_owner_permission_removed_blocks_delivery(callback_api: CallbackApiContext) -> None:
    """Current owner authorization is enforced at fire time."""
    runtime_paths = callback_api.runtime_paths
    config = _write_runtime_config(runtime_paths.config_path, owner_authorized=False)
    assert config_lifecycle._publish_runtime_config_into_app(config, runtime_paths, api_main.app)
    _bind_runtime(callback_api.ready_checks)

    response = _fire(callback_api)

    assert response.status_code == 403
    store = CallbackStore(runtime_paths)
    record = store.get_record(callback_api.callback_id)
    assert record is not None
    assert record.uses_left == 1


def test_owner_not_joined_blocks_delivery(
    callback_api: CallbackApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live owner room membership is checked before any use is claimed."""
    monkeypatch.setattr("mindroom.api.callbacks.is_user_joined_room", _owner_not_joined)

    response = _fire(callback_api)

    assert response.status_code == 403
    store = CallbackStore(callback_api.runtime_paths)
    record = store.get_record(callback_api.callback_id)
    assert record is not None
    assert record.uses_left == 1


def test_delivery_failure_releases_claimed_use(
    callback_api: CallbackApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Matrix delivery returns the claimed use for a retry."""
    _mock_execute(monkeypatch, event_id=None)

    response = _fire(callback_api)

    assert response.status_code == 502
    store = CallbackStore(callback_api.runtime_paths)
    record = store.get_record(callback_api.callback_id)
    assert record is not None
    assert record.uses_left == 1


def test_private_agent_fire_stamps_trusted_owner(
    private_callback_api: CallbackApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private targets receive the wake with the owner stamped as trusted requester."""
    executed = _mock_execute(monkeypatch)

    response = _fire(private_callback_api)

    assert response.status_code == 200
    snapshot = executed[0]
    assert getattr(snapshot, "owner_user_id", None) == _OWNER
    assert getattr(snapshot, "target_agent", None) == "coder"
