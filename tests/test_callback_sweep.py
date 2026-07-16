"""Tests for the callback expiry sweep."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

import pytest
import yaml

from mindroom import constants
from mindroom.api import config_lifecycle
from mindroom.api import main as api_main
from mindroom.callbacks.store import CallbackDeliverySnapshot, CallbackRecord, CallbackStore
from mindroom.callbacks.sweep import _sweep_expired_callbacks, run_callback_sweep_loop
from mindroom.config.main import Config

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.external_triggers.models import TriggerDeliveryReadiness

_OWNER = "@owner:example.org"


def _stored_record(store: CallbackStore, callback_id: str) -> CallbackRecord | None:
    return next((record for record in store.list_records() if record.callback_id == callback_id), None)


def _write_runtime_config(config_path: Path, *, enabled: bool = True) -> Config:
    payload: dict[str, object] = {
        "models": {"default": {"provider": "openai", "id": "gpt-5.6"}},
        "router": {"model": "default"},
        "agents": {"coder": {"display_name": "Coder", "role": "test", "rooms": ["workroom"]}},
        "rooms": {"workroom": {"display_name": "Workroom"}},
        "callback_policy": {"enabled": enabled},
        "authorization": {"global_users": [_OWNER], "agent_reply_permissions": {"*": [_OWNER]}},
    }
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return Config.model_validate(payload)


def _initialized_app(tmp_path: Path, *, enabled: bool = True) -> tuple[Config, constants.RuntimePaths]:
    config_path = tmp_path / "config.yaml"
    config = _write_runtime_config(config_path, enabled=enabled)
    runtime_paths = constants.resolve_primary_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    api_main.initialize_api_app(api_main.app, runtime_paths)
    assert config_lifecycle.load_config_into_app(runtime_paths, api_main.app) is True
    api_main.unbind_external_trigger_runtime(api_main.app)
    return config, runtime_paths


def _mint_expired(
    runtime_paths: constants.RuntimePaths,
    config: Config,
    *,
    on_expiry: Literal["notify", "silent"] = "notify",
    consumed: bool = False,
    script: Path | None = None,
) -> CallbackRecord:
    store = CallbackStore(runtime_paths)
    record, _token = store.mint_record(
        owner_user_id=_OWNER,
        created_by_agent_name="coder",
        created_in_room_id="workroom",
        created_in_thread_id="$thread-root",
        target_room_id="workroom",
        target_thread_id="$thread-root",
        target_agent="coder",
        label="expiring task",
        ttl_seconds=60,
        max_uses=1,
        on_expiry=on_expiry,
        config=config,
    )
    if consumed:
        store.claim_use(record.callback_id, now=record.created_at)
    if script is not None:
        script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        store.set_script_path(record.callback_id, str(script))
    updated = _stored_record(store, record.callback_id)
    assert updated is not None
    return updated


def _bind_ready_runtime() -> list[TriggerDeliveryReadiness]:
    ready_checks: list[TriggerDeliveryReadiness] = []

    async def is_delivery_target_ready(readiness: TriggerDeliveryReadiness) -> bool:
        ready_checks.append(readiness)
        return True

    api_main.bind_external_trigger_runtime(
        api_main.app,
        client=object(),
        conversation_cache=object(),
        is_delivery_target_ready=is_delivery_target_ready,
    )
    return ready_checks


async def _owner_joined(*_args: object, **_kwargs: object) -> bool:
    return True


def _mock_expiry_notice(
    monkeypatch: pytest.MonkeyPatch,
    *,
    event_id: str | None = "$notice",
) -> list[CallbackDeliverySnapshot]:
    notices: list[CallbackDeliverySnapshot] = []

    async def execute_callback_expiry_notice(**kwargs: object) -> str | None:
        snapshot = kwargs["snapshot"]
        assert isinstance(snapshot, CallbackDeliverySnapshot)
        notices.append(snapshot)
        return event_id

    monkeypatch.setattr("mindroom.callbacks.sweep.execute_callback_expiry_notice", execute_callback_expiry_notice)
    monkeypatch.setattr("mindroom.callbacks.sweep.is_user_joined_room", _owner_joined)
    return notices


@pytest.mark.asyncio
async def test_sweep_notifies_and_deletes_expired_unfired_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired notify callback posts one timeout notice, then record and script go away."""
    config, runtime_paths = _initialized_app(tmp_path)
    script = tmp_path / "cb_script.sh"
    record = _mint_expired(runtime_paths, config, script=script)
    _bind_ready_runtime()
    notices = _mock_expiry_notice(monkeypatch)
    monkeypatch.setattr("mindroom.callbacks.sweep.time.time", lambda: float(record.expires_at + 1))

    await _sweep_expired_callbacks(api_main.app)

    assert len(notices) == 1
    assert notices[0].label == "expiring task"
    assert _stored_record(CallbackStore(runtime_paths), record.callback_id) is None
    assert not script.exists()
    api_main.unbind_external_trigger_runtime(api_main.app)


@pytest.mark.asyncio
async def test_sweep_deletes_silent_and_consumed_callbacks_without_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silent-mode and fully consumed callbacks are cleaned up without a message."""
    config, runtime_paths = _initialized_app(tmp_path)
    silent = _mint_expired(runtime_paths, config, on_expiry="silent")
    consumed = _mint_expired(runtime_paths, config, consumed=True)
    _bind_ready_runtime()
    notices = _mock_expiry_notice(monkeypatch)
    latest_expiry = max(silent.expires_at, consumed.expires_at)
    monkeypatch.setattr("mindroom.callbacks.sweep.time.time", lambda: float(latest_expiry + 1))

    await _sweep_expired_callbacks(api_main.app)

    assert notices == []
    store = CallbackStore(runtime_paths)
    assert _stored_record(store, silent.callback_id) is None
    assert _stored_record(store, consumed.callback_id) is None
    api_main.unbind_external_trigger_runtime(api_main.app)


@pytest.mark.asyncio
async def test_sweep_keeps_notify_callback_when_runtime_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a delivery runtime the notice is retried on a later sweep tick."""
    config, runtime_paths = _initialized_app(tmp_path)
    record = _mint_expired(runtime_paths, config)
    notices = _mock_expiry_notice(monkeypatch)
    monkeypatch.setattr("mindroom.callbacks.sweep.time.time", lambda: float(record.expires_at + 1))

    await _sweep_expired_callbacks(api_main.app)

    assert notices == []
    assert _stored_record(CallbackStore(runtime_paths), record.callback_id) is not None


@pytest.mark.asyncio
async def test_sweep_gives_up_on_notice_after_grace_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A notice that stays undeliverable past the grace window is dropped silently."""
    config, runtime_paths = _initialized_app(tmp_path)
    record = _mint_expired(runtime_paths, config)
    notices = _mock_expiry_notice(monkeypatch)
    monkeypatch.setattr(
        "mindroom.callbacks.sweep.time.time",
        lambda: float(record.expires_at + 86400 + 1),
    )

    await _sweep_expired_callbacks(api_main.app)

    assert notices == []
    assert _stored_record(CallbackStore(runtime_paths), record.callback_id) is None


@pytest.mark.asyncio
async def test_sweep_is_inert_when_policy_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disabled callback policy leaves stored records untouched."""
    config, runtime_paths = _initialized_app(tmp_path)
    record = _mint_expired(runtime_paths, config)
    _write_runtime_config(runtime_paths.config_path, enabled=False)
    assert config_lifecycle.load_config_into_app(runtime_paths, api_main.app) is True
    notices = _mock_expiry_notice(monkeypatch)
    monkeypatch.setattr("mindroom.callbacks.sweep.time.time", lambda: float(record.expires_at + 1))

    await _sweep_expired_callbacks(api_main.app)

    assert notices == []
    assert _stored_record(CallbackStore(runtime_paths), record.callback_id) is not None


@pytest.mark.asyncio
async def test_sweep_keeps_record_when_notice_delivery_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Matrix notice send leaves the record for the next tick."""
    config, runtime_paths = _initialized_app(tmp_path)
    record = _mint_expired(runtime_paths, config)
    _bind_ready_runtime()
    notices = _mock_expiry_notice(monkeypatch, event_id=None)
    monkeypatch.setattr("mindroom.callbacks.sweep.time.time", lambda: float(record.expires_at + 1))

    await _sweep_expired_callbacks(api_main.app)

    assert len(notices) == 1
    assert _stored_record(CallbackStore(runtime_paths), record.callback_id) is not None
    api_main.unbind_external_trigger_runtime(api_main.app)


@pytest.mark.asyncio
async def test_callback_sweep_loop_runs_on_its_own_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker-cleanup configuration cannot delay the callback expiry cadence."""
    stop_event = asyncio.Event()
    swept_apps: list[object] = []

    async def sweep_once(api_app: object) -> None:
        swept_apps.append(api_app)
        stop_event.set()

    monkeypatch.setattr("mindroom.callbacks.sweep._sweep_expired_callbacks", sweep_once)
    await run_callback_sweep_loop(stop_event, api_main.app, interval_seconds=0.001)

    assert swept_apps == [api_main.app]
