"""Core reaches every built-in integration through the registry helpers, in registry order."""

from __future__ import annotations

import pytest

from mindroom import integrations
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.integrations import Integration, bind_integrations, reply_mirror_for, unbind_integrations


def _register(monkeypatch: pytest.MonkeyPatch, *registered: Integration) -> None:
    monkeypatch.setattr(integrations, "_integrations", lambda: registered)


def test_bind_and_unbind_reach_each_integration_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unbinding returns each runtime that was bound, by name, and skips integrations with nothing bound."""
    app, bots, memberships = object(), {}, AgentReplyMembershipIndex()
    bound: list[tuple[str, object, object, object]] = []
    first_runtime, last_runtime = object(), object()
    runtimes = {"first": first_runtime, "last": last_runtime}

    def hooks(name: str) -> Integration:
        return Integration(
            name=name,
            bind=lambda *args: bound.append((name, *args)),
            unbind=lambda unbound_app: runtimes.pop(name, None) if unbound_app is app else None,
        )

    _register(monkeypatch, hooks("first"), Integration(name="without_hooks"), hooks("idle"), hooks("last"))

    bind_integrations(app, bots, memberships)
    assert bound == [(name, app, bots, memberships) for name in ("first", "idle", "last")]
    assert unbind_integrations(app) == [("first", first_runtime), ("last", last_runtime)]
    assert unbind_integrations(app) == []


@pytest.mark.asyncio
async def test_reply_mirror_is_the_first_integration_that_takes_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """Integrations after the one that takes a reply are not asked, and a reply none takes starts nothing."""
    envelope, config, runtime_paths = object(), object(), object()
    asked: list[str] = []
    mirrored: list[str] = []

    async def mirror(response_text: str) -> None:
        mirrored.append(response_text)

    def mirror_hook(name: str, *, takes: bool) -> Integration:
        def reply_mirror(*args: object) -> object:
            assert args[0] is envelope
            assert args[2:] == (config, runtime_paths)
            asked.append(name)
            return mirror(f"{name}: {args[1]}") if takes else None

        return Integration(name=name, reply_mirror=reply_mirror)

    _register(
        monkeypatch,
        Integration(name="without_hooks"),
        mirror_hook("declines", takes=False),
        mirror_hook("takes", takes=True),
        mirror_hook("later", takes=True),
    )
    taken = reply_mirror_for(envelope, "done", config=config, runtime_paths=runtime_paths)
    assert taken is not None
    await taken
    assert asked == ["declines", "takes"]
    assert mirrored == ["takes: done"]

    _register(monkeypatch, mirror_hook("declines", takes=False))
    assert reply_mirror_for(envelope, "done", config=config, runtime_paths=runtime_paths) is None
