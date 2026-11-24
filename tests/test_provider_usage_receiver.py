"""Encrypted Matrix to-device RPC tests for provider usage limits."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from nio.crypto import OlmDevice
from structlog.testing import capture_logs

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.usage.limits_receiver import register_provider_usage_receiver
from mindroom.usage.provider_limits import (
    DailyAccountUsage,
    ProviderLimitAccount,
    ProviderUsageSnapshot,
)
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

ADMIN = "@admin:example.org"
OTHER = "@member:example.org"
ROOM = "!room:example.org"


def _request(
    *,
    sender: str = ADMIN,
    device_id: str = "DEV1",
    ed25519: str = "ed-key",
    curve25519: str = "curve-key",
    **changes: object,
) -> nio.AuthenticatedToDeviceEvent:
    content: dict[str, object] = {"version": 1, "request_id": "req-usage-1", "room_id": ROOM}
    content.update(changes)
    return nio.AuthenticatedToDeviceEvent(
        source={"content": content},
        sender=sender,
        type="io.mindroom.usage.request",
        authenticated_sender=nio.AuthenticatedDevice(sender, device_id, curve25519, ed25519),
    )


def _sample_snapshot(*, large: bool = False) -> ProviderUsageSnapshot:
    keys = tuple(f"model-key-{i:04d}" for i in range(4000)) if large else ("haiku",)
    return ProviderUsageSnapshot(
        checked_at="2026-11-23T20:00:00Z",
        partial=False,
        accounts=(
            ProviderLimitAccount(
                id="anthropic:default",
                provider="anthropic",
                display_name="Anthropic",
                account_label="Pay-as-you-go",
                plan="Pay-as-you-go",
                model_keys=keys,
                windows=(),
                unavailable_reason=None,
                checked_at="2026-11-23T20:00:00Z",
                stale=False,
                tokens_7d=1200,
                cost_usd_7d=0.5,
                cost_estimated=True,
            ),
        ),
        daily_7d=(DailyAccountUsage(day="2026-11-23", by_account={"anthropic:default": 1200}, unattributed_tokens=0),),
        unattributed_tokens_7d=0,
        unattributed_cost_usd_7d=None,
    )


def _setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    answers: bool = True,
    snapshot_fn: AsyncMock | None = None,
) -> tuple[Any, MagicMock, list[Config], AsyncMock, AsyncMock]:
    paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(administrators=[ADMIN], models={"haiku": ModelConfig(provider="anthropic", id="claude-haiku-4-5")}),
        paths,
    )
    client = MagicMock(spec=nio.AsyncClient)
    client.olm = SimpleNamespace(
        device_store={
            ADMIN: {"DEV1": OlmDevice(ADMIN, "DEV1", {"ed25519": "ed-key", "curve25519": "curve-key"})},
            OTHER: {"DEV2": OlmDevice(OTHER, "DEV2", {"ed25519": "ed-other", "curve25519": "curve-other"})},
        },
    )
    sent = AsyncMock()
    probe = snapshot_fn or AsyncMock(return_value=_sample_snapshot())
    monkeypatch.setattr("mindroom.usage.limits_receiver.send_encrypted_to_device", sent)
    monkeypatch.setattr("mindroom.usage.limits_receiver.get_provider_usage_snapshot", probe)
    current = [config]
    register_provider_usage_receiver(
        client=client,
        runtime_paths=paths,
        config_getter=lambda: current[0],
        answers_in_room=lambda _room_id: answers,
        callback_wrapper=lambda cb: cb,
    )
    callback = client.add_to_device_callback.call_args.args[0]
    return callback, client, current, sent, probe


def _drops(logs: list[dict[str, object]]) -> list[object]:
    return [entry["reason"] for entry in logs if entry.get("event") == "provider_usage_dropped"]


@pytest.mark.asyncio
async def test_admin_receives_encrypted_usage_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Platform administrator gets an encrypted response sent to their pinned device."""
    callback, _client, _current, sent, probe = _setup(tmp_path, monkeypatch)
    await callback(_request(force_refresh=True))

    probe.assert_awaited_once()
    assert probe.await_args.kwargs["force_refresh"] is True
    sent.assert_awaited_once()
    _client_arg, target = sent.await_args.args
    assert (target.user_id, target.device_id, target.ed25519) == (ADMIN, "DEV1", "ed-key")
    assert sent.await_args.kwargs["event_type"] == "io.mindroom.usage.response"
    content = sent.await_args.kwargs["content"]
    assert content["version"] == 1
    assert content["request_id"] == "req-usage-1"
    assert content["partial"] is False
    assert content["accounts"][0]["id"] == "anthropic:default"


@pytest.mark.asyncio
async def test_non_admin_and_non_poster_dropped_before_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-admins and non-elected room devices are silently dropped before probing."""
    callback, _client, _current, sent, probe = _setup(tmp_path, monkeypatch, answers=True)
    with capture_logs() as logs:
        await callback(_request(sender=OTHER, device_id="DEV2", ed25519="ed-other", curve25519="curve-other"))
        await callback(_request(version=2))
        await callback(_request(force_refresh="yes"))
    assert _drops(logs) == ["not_admin", "unparseable", "unparseable"]
    probe.assert_not_awaited()
    sent.assert_not_awaited()

    non_poster_cb, _c2, _cur2, sent2, probe2 = _setup(tmp_path, monkeypatch, answers=False)
    with capture_logs() as logs2:
        await non_poster_cb(_request())
    assert _drops(logs2) == ["not_room_poster"]
    probe2.assert_not_awaited()
    sent2.assert_not_awaited()

    with capture_logs() as logs3:
        await callback(_request(device_id="UNKNOWN", ed25519="unknown-key"))
    assert _drops(logs3) == ["unknown_sender_device"]
    probe.assert_not_awaited()
    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_revocation_and_device_key_rotation_block_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revoking admin status or changing sender device keys mid-probe prevents sending the reply."""
    gate = asyncio.Event()
    started = asyncio.Event()

    async def _slow_probe(**_kw: object) -> ProviderUsageSnapshot:
        started.set()
        await gate.wait()
        return _sample_snapshot()

    callback, client, current, sent, _probe = _setup(
        tmp_path,
        monkeypatch,
        snapshot_fn=AsyncMock(side_effect=_slow_probe),
    )

    with capture_logs() as logs:
        task = asyncio.create_task(callback(_request()))
        await started.wait()
        current[0] = current[0].model_copy(update={"administrators": []})
        gate.set()
        await task
    assert _drops(logs) == ["not_admin"]
    sent.assert_not_awaited()

    # Restore admin, then rotate sender device key mid-flight.
    current[0] = current[0].model_copy(update={"administrators": [ADMIN]})
    gate.clear()
    started.clear()
    with capture_logs() as logs2:
        task2 = asyncio.create_task(callback(_request(request_id="req-usage-2")))
        await started.wait()
        client.olm.device_store[ADMIN]["DEV1"] = OlmDevice(
            ADMIN,
            "DEV1",
            {"ed25519": "rotated", "curve25519": "curve-key"},
        )
        gate.set()
        await task2
    assert _drops(logs2) == ["sender_device_changed"]
    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_receiver_enforces_48kb_device_payload_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Oversized snapshots are trimmed under 48 KB with partial=True before to-device encryption."""
    callback, _client, _current, sent, _probe = _setup(
        tmp_path,
        monkeypatch,
        snapshot_fn=AsyncMock(return_value=_sample_snapshot(large=True)),
    )
    await callback(_request(request_id="req-big"))
    sent.assert_awaited_once()
    content = sent.await_args.kwargs["content"]
    assert content["partial"] is True
    assert content["daily_7d"] == []
    assert len(json.dumps(content).encode("utf-8")) <= 48_000
