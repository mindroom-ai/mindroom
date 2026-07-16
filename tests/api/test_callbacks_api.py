"""API tests for single-use callback delivery."""

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
from mindroom.callbacks.store import CallbackRecord, CallbackStore
from mindroom.config.main import Config

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from httpx import Response

    from mindroom.external_triggers.models import TriggerDeliveryReadiness

_OWNER = "@owner:example.org"


@dataclass(frozen=True)
class CallbackApiContext:
    """Live API test state with one minted callback."""

    client: TestClient
    record: CallbackRecord
    token: str
    runtime_paths: constants.RuntimePaths
    ready_checks: list[TriggerDeliveryReadiness]


def _config_payload(*, owner_authorized: bool = True, private_coder: bool = False) -> dict[str, object]:
    authorization: dict[str, object] = {"agent_reply_permissions": {"*": [_OWNER]}}
    if owner_authorized:
        authorization["global_users"] = [_OWNER]
    payload: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "gpt-5.6"}},
        "router": {"model": "default"},
        "agents": {"coder": {"display_name": "Coder", "role": "test", "rooms": ["workroom"]}},
        "rooms": {"workroom": {"display_name": "Workroom"}},
        "authorization": authorization,
    }
    if private_coder:
        agents = cast("dict[str, dict[str, object]]", payload["agents"])
        agents["coder"]["private"] = {"per": "user", "root": "coder_data"}
    return payload


def _write_runtime_config(
    config_path: Path,
    *,
    owner_authorized: bool = True,
    private_coder: bool = False,
) -> Config:
    payload = _config_payload(owner_authorized=owner_authorized, private_coder=private_coder)
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return Config.model_validate(payload)


def _mint_record(runtime_paths: constants.RuntimePaths) -> tuple[CallbackRecord, str]:
    return CallbackStore(runtime_paths).mint_record(
        owner_user_id=_OWNER,
        room_id="workroom",
        thread_id="$thread-root",
        agent_name="coder",
        label="issue-042 implementer",
    )


def _bind_runtime(ready_checks: list[TriggerDeliveryReadiness], *, ready: bool = True) -> None:
    async def is_delivery_target_ready(readiness: TriggerDeliveryReadiness) -> bool:
        ready_checks.append(readiness)
        return ready

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
) -> Iterator[CallbackApiContext]:
    config_path = tmp_path / "config.yaml"
    _write_runtime_config(config_path, private_coder=private_coder)
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    api_main.initialize_api_app(api_main.app, runtime_paths)
    assert config_lifecycle.load_config_into_app(runtime_paths, api_main.app) is True
    record, token = _mint_record(runtime_paths)
    api_main.unbind_external_trigger_runtime(api_main.app)
    ready_checks: list[TriggerDeliveryReadiness] = []
    _bind_runtime(ready_checks)
    monkeypatch.setattr("mindroom.api.callbacks.is_user_joined_room", _owner_joined)

    try:
        with TestClient(api_main.app) as client:
            yield CallbackApiContext(client, record, token, runtime_paths, ready_checks)
    finally:
        api_main.unbind_external_trigger_runtime(api_main.app)


@pytest.fixture
def callback_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CallbackApiContext]:
    """Return a public-agent callback API context."""
    yield from _callback_api_context(tmp_path, monkeypatch)


@pytest.fixture
def private_callback_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[CallbackApiContext]:
    """Return a private-agent callback API context."""
    yield from _callback_api_context(tmp_path, monkeypatch, private_coder=True)


def _fire(
    context: CallbackApiContext,
    *,
    token: str | None = None,
    callback_id: str | None = None,
    body: dict[str, object] | None = None,
    raw_body: bytes | None = None,
) -> Response:
    content = raw_body if raw_body is not None else json.dumps(body or {"message": "all green"}).encode()
    headers = {"Content-Type": "application/json"}
    resolved_token = context.token if token is None else token
    if resolved_token:
        headers["Authorization"] = f"Bearer {resolved_token}"
    return context.client.post(
        f"/api/callbacks/{callback_id or context.record.callback_id}",
        content=content,
        headers=headers,
    )


def _mock_execute(
    monkeypatch: pytest.MonkeyPatch,
    *,
    event_id: str | None = "$matrix-event",
) -> list[CallbackRecord]:
    executed: list[CallbackRecord] = []

    async def execute_callback_fire(**kwargs: object) -> str | None:
        record = kwargs["record"]
        assert isinstance(record, CallbackRecord)
        executed.append(record)
        return event_id

    monkeypatch.setattr("mindroom.api.callbacks.execute_callback_fire", execute_callback_fire)
    return executed


def test_success_wakes_agent_once_and_deletes_record(
    callback_api: CallbackApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid fire wakes the bound agent and consumes the callback."""
    executed = _mock_execute(monkeypatch)

    response = _fire(callback_api)

    assert response.status_code == 204
    assert callback_api.ready_checks[0].target_agent == "coder"
    assert callback_api.ready_checks[0].resolved_room_id == "workroom"
    assert executed[0].owner_user_id == _OWNER
    assert executed[0].thread_id == "$thread-root"
    assert CallbackStore(callback_api.runtime_paths).get_record(callback_api.record.callback_id) is None
    assert _fire(callback_api).status_code == 404


@pytest.mark.parametrize(
    ("token", "callback_id"),
    [
        ("", None),
        ("mrcb_wrong", None),
        (None, "cb_0000000000000000"),
    ],
)
def test_invalid_capability_returns_not_found(
    callback_api: CallbackApiContext,
    token: str | None,
    callback_id: str | None,
) -> None:
    """Missing, wrong, and unknown capabilities look identical."""
    response = _fire(callback_api, token=token, callback_id=callback_id)

    assert response.status_code == 404
    assert response.json()["detail"] == "Callback not found"


def test_expired_callback_returns_gone_and_is_deleted(
    callback_api: CallbackApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid but expired capability is rejected and collected."""
    monkeypatch.setattr("mindroom.api.callbacks.time.time", lambda: float(callback_api.record.expires_at))
    _mock_execute(monkeypatch)

    response = _fire(callback_api)

    assert response.status_code == 410
    assert "expired" in response.json()["detail"]
    assert CallbackStore(callback_api.runtime_paths).get_record(callback_api.record.callback_id) is None


def test_invalid_or_oversized_body_does_not_claim_callback(callback_api: CallbackApiContext) -> None:
    """Malformed input fails before the single-use claim."""
    invalid = _fire(callback_api, body={"status": "done", "message": "all green"})
    oversized = _fire(callback_api, raw_body=json.dumps({"message": "x" * 70_000}).encode())

    assert invalid.status_code == 422
    assert oversized.status_code == 413
    record = CallbackStore(callback_api.runtime_paths).get_record(callback_api.record.callback_id)
    assert record is not None
    assert record.claimed is False


def test_current_owner_authorization_is_required(callback_api: CallbackApiContext) -> None:
    """Authorization removed after minting blocks callback delivery."""
    config = _write_runtime_config(callback_api.runtime_paths.config_path, owner_authorized=False)
    assert config_lifecycle._publish_runtime_config_into_app(config, callback_api.runtime_paths, api_main.app)
    _bind_runtime(callback_api.ready_checks)

    response = _fire(callback_api)

    assert response.status_code == 403
    record = CallbackStore(callback_api.runtime_paths).get_record(callback_api.record.callback_id)
    assert record is not None
    assert record.claimed is False


def test_owner_must_still_be_joined(
    callback_api: CallbackApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The callback owner must remain joined to the target room."""
    monkeypatch.setattr("mindroom.api.callbacks.is_user_joined_room", _owner_not_joined)

    response = _fire(callback_api)

    assert response.status_code == 403
    record = CallbackStore(callback_api.runtime_paths).get_record(callback_api.record.callback_id)
    assert record is not None
    assert record.claimed is False


def test_target_runtime_must_be_ready(callback_api: CallbackApiContext) -> None:
    """The bound agent and room must be live before claiming."""
    _bind_runtime(callback_api.ready_checks, ready=False)

    response = _fire(callback_api)

    assert response.status_code == 503
    record = CallbackStore(callback_api.runtime_paths).get_record(callback_api.record.callback_id)
    assert record is not None
    assert record.claimed is False


def test_delivery_failure_releases_claim_for_retry(
    callback_api: CallbackApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Matrix send leaves the callback available for retry."""
    _mock_execute(monkeypatch, event_id=None)
    failed = _fire(callback_api)

    record = CallbackStore(callback_api.runtime_paths).get_record(callback_api.record.callback_id)
    assert failed.status_code == 502
    assert record is not None
    assert record.claimed is False

    _mock_execute(monkeypatch)
    assert _fire(callback_api).status_code == 204


def test_private_agent_callback_keeps_trusted_owner(
    private_callback_api: CallbackApiContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private-agent wakes retain the human requester identity."""
    executed = _mock_execute(monkeypatch)

    assert _fire(private_callback_api).status_code == 204
    assert executed[0].owner_user_id == _OWNER
    assert executed[0].agent_name == "coder"
