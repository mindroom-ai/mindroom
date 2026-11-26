"""Tests for cloud-side desktop response correlation."""

from __future__ import annotations

import asyncio
import gc
import weakref
from unittest.mock import AsyncMock

import pytest
from nio import AuthenticatedDevice, AuthenticatedToDeviceEvent

from mindroom.desktop.client import _ROUTERS, DesktopRequestError, DesktopResponseRouter, desktop_response_router
from mindroom.desktop.protocol import DESKTOP_RESPONSE_EVENT_TYPE, DesktopCommand, DesktopResponse
from mindroom.matrix.olm_to_device import PinnedMatrixDevice

TARGET = PinnedMatrixDevice("@desktop:example.org", "DESKTOP", "fingerprint")


class FakeClient:
    """Keep the response callback registered by the router."""

    def __init__(self) -> None:
        self.callback: object | None = None

    def add_to_device_callback(self, callback: object, _event_type: object) -> None:
        """Record callback."""
        self.callback = callback


def _command(*, request_id: str = "request-1", action: str = "status") -> DesktopCommand:
    return DesktopCommand(
        request_id=request_id,
        session_id="session-1",
        sequence=1,
        issued_at_ms=1_000,
        expires_at_ms=2_000,
        action=action,
        requester_id="@alice:example.org",
        agent_name="computer",
    )


def _event(response: DesktopResponse) -> AuthenticatedToDeviceEvent:
    return AuthenticatedToDeviceEvent(
        source={"content": response.to_content()},
        sender=TARGET.user_id,
        type=DESKTOP_RESPONSE_EVENT_TYPE,
        authenticated_sender=AuthenticatedDevice(TARGET.user_id, TARGET.device_id, "curve", TARGET.ed25519),
    )


@pytest.mark.asyncio
async def test_request_waits_for_exact_correlated_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only matching request and session IDs from the pinned device resolve a waiter."""
    client = FakeClient()
    router = DesktopResponseRouter(client)
    send = AsyncMock()
    monkeypatch.setattr("mindroom.desktop.client.send_encrypted_to_device", send)
    monkeypatch.setattr("mindroom.desktop.client.authenticated_sender_matches", lambda *_args: True)
    command = _command()

    pending = asyncio.create_task(router.request(TARGET, command, timeout_seconds=1))
    await asyncio.sleep(0)
    router.on_to_device_event(_event(DesktopResponse("request-1", "wrong-session", True)))
    assert not pending.done()

    expected = DesktopResponse("request-1", "session-1", True, result={"online": True})
    router.on_to_device_event(_event(expected))

    assert await pending == expected
    send.assert_awaited_once()


@pytest.mark.asyncio
async def test_router_rejects_response_from_unpinned_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    """A correctly correlated payload cannot resolve the request without exact Olm sender authentication."""
    client = FakeClient()
    router = DesktopResponseRouter(client)
    monkeypatch.setattr("mindroom.desktop.client.send_encrypted_to_device", AsyncMock())
    sender_matches = False
    monkeypatch.setattr(
        "mindroom.desktop.client.authenticated_sender_matches",
        lambda *_args: sender_matches,
    )
    response = DesktopResponse("request-1", "session-1", True)

    pending = asyncio.create_task(router.request(TARGET, _command(), timeout_seconds=1))
    await asyncio.sleep(0)
    router.on_to_device_event(_event(response))
    assert not pending.done()

    sender_matches = True
    router.on_to_device_event(_event(response))
    assert await pending == response


@pytest.mark.asyncio
async def test_request_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """An offline desktop cannot hold an agent tool call open indefinitely."""
    client = FakeClient()
    router = DesktopResponseRouter(client)
    monkeypatch.setattr("mindroom.desktop.client.send_encrypted_to_device", AsyncMock())

    with pytest.raises(DesktopRequestError, match="did not answer") as exc_info:
        await router.request(TARGET, _command(), timeout_seconds=0.001)

    assert "mindroom desktop run" in str(exc_info.value)
    assert exc_info.value.request_id == _command().request_id
    assert exc_info.value.action_outcome == "unknown"


@pytest.mark.asyncio
async def test_invalid_command_is_rejected_before_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    """A command the bridge cannot parse fails immediately without unknown-outcome guidance."""
    client = FakeClient()
    router = DesktopResponseRouter(client)
    send = AsyncMock()
    monkeypatch.setattr("mindroom.desktop.client.send_encrypted_to_device", send)
    command = _command(action="click")
    command.parameters["text"] = "x" * (16 * 1024 + 1)

    with pytest.raises(DesktopRequestError, match="invalid and was not sent") as exc_info:
        await router.request(TARGET, command, timeout_seconds=1)

    assert "encoded bytes" in str(exc_info.value)
    assert "outcome is unknown" not in str(exc_info.value)
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_timeout_includes_a_stuck_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """Matrix delivery retries are covered by the same end-to-end tool deadline."""
    client = FakeClient()
    router = DesktopResponseRouter(client)

    async def stuck_send(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(
        "mindroom.desktop.client.send_encrypted_to_device",
        AsyncMock(side_effect=stuck_send),
    )

    with pytest.raises(DesktopRequestError, match="did not answer"):
        await router.request(TARGET, _command(), timeout_seconds=0.001)


@pytest.mark.asyncio
async def test_control_timeout_reports_unknown_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timed-out control action cannot be presented as safe to retry."""
    client = FakeClient()
    router = DesktopResponseRouter(client)
    monkeypatch.setattr("mindroom.desktop.client.send_encrypted_to_device", AsyncMock())

    with pytest.raises(DesktopRequestError, match=r"outcome is unknown.*do not repeat"):
        await router.request(TARGET, _command(action="click"), timeout_seconds=0.001)


@pytest.mark.asyncio
async def test_browser_control_timeout_requests_browser_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Browser timeouts direct the agent to fresh tab state rather than desktop accessibility state."""
    client = FakeClient()
    router = DesktopResponseRouter(client)
    monkeypatch.setattr("mindroom.desktop.client.send_encrypted_to_device", AsyncMock())

    with pytest.raises(
        DesktopRequestError,
        match=r"browser\(action='tabs' or 'snapshot', target='desktop'\)",
    ):
        await router.request(TARGET, _command(action="browser_control"), timeout_seconds=0.001)


@pytest.mark.asyncio
async def test_only_one_request_per_target_can_be_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parallel tool calls cannot reorder or preplan multiple mutating desktop actions."""
    client = FakeClient()
    router = DesktopResponseRouter(client)
    send = AsyncMock()
    monkeypatch.setattr("mindroom.desktop.client.send_encrypted_to_device", send)

    first = asyncio.create_task(router.request(TARGET, _command(action="click"), timeout_seconds=1))
    await asyncio.sleep(0)
    with pytest.raises(DesktopRequestError, match="already in progress"):
        await router.request(TARGET, _command(request_id="request-2", action="click"), timeout_seconds=1)

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    send.assert_awaited_once()


def test_cached_router_does_not_keep_closed_matrix_client_alive() -> None:
    """Hot-reloaded clients disappear from the weak router cache after collection."""
    _ROUTERS.clear()
    client = FakeClient()
    router = desktop_response_router(client)
    client_ref = weakref.ref(client)
    router_ref = weakref.ref(router)

    del client
    del router
    gc.collect()

    assert client_ref() is None
    assert router_ref() is None
    assert len(_ROUTERS) == 0


@pytest.mark.asyncio
async def test_shell_start_timeout_never_advises_running_the_command_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A shell start may be awaiting approval, may have run, or may still be running when the cloud stops waiting."""
    client = FakeClient()
    router = DesktopResponseRouter(client)
    monkeypatch.setattr("mindroom.desktop.client.send_encrypted_to_device", AsyncMock())

    with pytest.raises(DesktopRequestError) as exc_info:
        await router.request(TARGET, _command(action="run_shell"), timeout_seconds=0.001)

    message = str(exc_info.value)
    assert "do not run the command again" in message
    assert "request_status with request_id='request-1'" in message
    assert exc_info.value.action_outcome == "unknown"


@pytest.mark.asyncio
async def test_handle_check_timeout_recovers_through_request_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """A completed handle is handed over once, so a lost check must be recovered from its receipt."""
    client = FakeClient()
    router = DesktopResponseRouter(client)
    monkeypatch.setattr("mindroom.desktop.client.send_encrypted_to_device", AsyncMock())

    with pytest.raises(DesktopRequestError) as exc_info:
        await router.request(TARGET, _command(action="check_shell"), timeout_seconds=0.001)

    message = str(exc_info.value)
    assert "do not call check_shell again" in message
    assert "only if request_status reports not_found" in message


@pytest.mark.asyncio
async def test_management_actions_run_concurrently_with_run_shell_and_serialize_sends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One management action can run alongside run_shell with serialized sends, while conflicting lanes still reject."""
    client = FakeClient()
    router = DesktopResponseRouter(client)
    monkeypatch.setattr("mindroom.desktop.client.authenticated_sender_matches", lambda *_args: True)

    first_send_entered = asyncio.Event()
    release_first_send = asyncio.Event()
    send_events: list[tuple[str, str]] = []

    async def tracked_send(_client: object, _target: object, *, event_type: str, content: dict[str, object]) -> None:
        del event_type
        req_id = str(content["request_id"])
        send_events.append(("start", req_id))
        if req_id == "shell-1":
            first_send_entered.set()
            await release_first_send.wait()
        send_events.append(("end", req_id))

    monkeypatch.setattr("mindroom.desktop.client.send_encrypted_to_device", tracked_send)

    shell_task = asyncio.create_task(
        router.request(TARGET, _command(request_id="shell-1", action="run_shell"), timeout_seconds=1),
    )
    await first_send_entered.wait()

    # Management command starts while run_shell is still sending: allowed, but waits for send_lock.
    status_task = asyncio.create_task(
        router.request(TARGET, _command(request_id="status-1", action="status"), timeout_seconds=1),
    )
    await asyncio.sleep(0)
    assert send_events == [("start", "shell-1")]

    # Conflicting commands while both lanes are occupied are rejected.
    for req_id, action in (("shell-2", "run_shell"), ("click-1", "click")):
        with pytest.raises(DesktopRequestError, match="already in progress"):
            await router.request(TARGET, _command(request_id=req_id, action=action), timeout_seconds=1)

    release_first_send.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert send_events == [("start", "shell-1"), ("end", "shell-1"), ("start", "status-1"), ("end", "status-1")]

    # Resolve management command while run_shell is still waiting on its future.
    status_resp = DesktopResponse("status-1", "session-1", True, result={"online": True})
    router.on_to_device_event(_event(status_resp))
    assert await status_task == status_resp

    # Non-management actions remain rejected while run_shell is still in flight; another management action is allowed.
    with pytest.raises(DesktopRequestError, match="already in progress"):
        await router.request(TARGET, _command(request_id="click-2", action="click"), timeout_seconds=1)

    check_task = asyncio.create_task(
        router.request(TARGET, _command(request_id="check-1", action="check_shell"), timeout_seconds=1),
    )
    await asyncio.sleep(0)
    check_resp = DesktopResponse("check-1", "session-1", True, result={"state": "running"})
    router.on_to_device_event(_event(check_resp))
    assert await check_task == check_resp

    shell_resp = DesktopResponse("shell-1", "session-1", True, result={"state": "completed"})
    router.on_to_device_event(_event(shell_resp))
    assert await shell_task == shell_resp

    # Symmetric check: non-management in flight rejects run_shell.
    click_task = asyncio.create_task(
        router.request(TARGET, _command(request_id="click-3", action="click"), timeout_seconds=1),
    )
    await asyncio.sleep(0)
    with pytest.raises(DesktopRequestError, match="already in progress"):
        await router.request(TARGET, _command(request_id="shell-3", action="run_shell"), timeout_seconds=1)
    click_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await click_task

    assert not router._pending
    assert TARGET not in router._send_locks


@pytest.mark.asyncio
async def test_concurrent_read_only_requests_serialize_in_order_while_mutations_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pure read-only calls serialize cleanly through send_lock; GUI mutations still conflict with reads."""
    client = FakeClient()
    router = DesktopResponseRouter(client)
    monkeypatch.setattr("mindroom.desktop.client.authenticated_sender_matches", lambda *_args: True)

    first_send_entered = asyncio.Event()
    release_first_send = asyncio.Event()
    send_events: list[tuple[str, str]] = []

    async def tracked_send(_client: object, _target: object, *, event_type: str, content: dict[str, object]) -> None:
        del event_type
        req_id = str(content["request_id"])
        send_events.append(("start", req_id))
        if req_id == "read-1":
            first_send_entered.set()
            await release_first_send.wait()
        send_events.append(("end", req_id))

    monkeypatch.setattr("mindroom.desktop.client.send_encrypted_to_device", tracked_send)

    state_task = asyncio.create_task(
        router.request(TARGET, _command(request_id="read-1", action="get_app_state"), timeout_seconds=1),
    )
    await first_send_entered.wait()
    status_task = asyncio.create_task(
        router.request(TARGET, _command(request_id="read-2", action="status"), timeout_seconds=1),
    )
    file_task = asyncio.create_task(
        router.request(TARGET, _command(request_id="read-3", action="read_file"), timeout_seconds=1),
    )
    await asyncio.sleep(0)
    assert send_events == [("start", "read-1")]

    # Mutating GUI actions still fail fast while reads are in flight.
    with pytest.raises(DesktopRequestError, match="already in progress"):
        await router.request(TARGET, _command(request_id="click-1", action="click"), timeout_seconds=1)

    release_first_send.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert send_events == [
        ("start", "read-1"),
        ("end", "read-1"),
        ("start", "read-2"),
        ("end", "read-2"),
        ("start", "read-3"),
        ("end", "read-3"),
    ]

    responses = [
        DesktopResponse("read-1", "session-1", True, result={"step": 1}),
        DesktopResponse("read-2", "session-1", True, result={"step": 2}),
        DesktopResponse("read-3", "session-1", True, result={"step": 3}),
    ]
    for response in responses:
        router.on_to_device_event(_event(response))
    assert await asyncio.gather(state_task, status_task, file_task) == responses

    # Symmetric check: read while GUI mutation is in flight fails fast immediately.
    in_flight = asyncio.create_task(
        router.request(TARGET, _command(request_id="in-click", action="click"), timeout_seconds=1),
    )
    await asyncio.sleep(0)
    with pytest.raises(DesktopRequestError, match="already in progress"):
        await router.request(TARGET, _command(request_id="rej-state", action="get_app_state"), timeout_seconds=1)
    in_flight.cancel()
    with pytest.raises(asyncio.CancelledError):
        await in_flight

    assert not router._pending
    assert TARGET not in router._send_locks


@pytest.mark.asyncio
async def test_shell_handle_actions_conflict_only_on_same_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """check_shell/kill_shell conflict only on the same handle (or None), never with other handles, GUI, or reads."""
    client = FakeClient()
    router = DesktopResponseRouter(client)
    monkeypatch.setattr("mindroom.desktop.client.authenticated_sender_matches", lambda *_args: True)
    monkeypatch.setattr("mindroom.desktop.client.send_encrypted_to_device", AsyncMock())

    def cmd(request_id: str, action: str, handle: str | None = None) -> DesktopCommand:
        base = _command(request_id=request_id, action=action)
        params = {"handle": handle} if handle is not None else {}
        return DesktopCommand(
            request_id=base.request_id,
            session_id=base.session_id,
            sequence=base.sequence,
            issued_at_ms=base.issued_at_ms,
            expires_at_ms=base.expires_at_ms,
            action=base.action,
            requester_id=base.requester_id,
            agent_name=base.agent_name,
            parameters=params,
        )

    h1_check = asyncio.create_task(router.request(TARGET, cmd("c1", "check_shell", "shell:1"), timeout_seconds=1))
    await asyncio.sleep(0)

    # Same handle (check or kill) or missing handle conflicts.
    for req_id, action, handle in (
        ("c1-dup", "check_shell", "shell:1"),
        ("k1-dup", "kill_shell", "shell:1"),
        ("c-none", "check_shell", None),
    ):
        with pytest.raises(DesktopRequestError, match="already in progress"):
            await router.request(TARGET, cmd(req_id, action, handle), timeout_seconds=1)

    # Different handle, read-only status, GUI click, and run_shell all run concurrently with h1_check.
    h2_check = asyncio.create_task(router.request(TARGET, cmd("c2", "check_shell", "shell:2"), timeout_seconds=1))
    click_task = asyncio.create_task(
        router.request(TARGET, _command(request_id="g1", action="click"), timeout_seconds=1),
    )
    await asyncio.sleep(0)

    for req_id in ("c1", "c2", "g1"):
        router.on_to_device_event(_event(DesktopResponse(req_id, "session-1", True, result={"ok": req_id})))
    results = await asyncio.gather(h1_check, h2_check, click_task)
    assert [r.result["ok"] for r in results] == ["c1", "c2", "g1"]
    assert not router._pending
