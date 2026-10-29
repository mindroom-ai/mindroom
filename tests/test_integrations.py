"""Core reaches every built-in integration through the registry helpers, in registry order."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import FastAPI

from mindroom import integrations
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.integrations import Integration, bind_integrations, reply_mirror_for, unbind_integrations
from mindroom.slack.integration import SLACK_INTEGRATION


def test_bind_and_unbind_run_in_registry_order_and_return_the_bound_runtimes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown drains the runtime each bound integration returns and skips an idle one."""
    bound: list[str] = []
    runtime = object()
    owner = replace(SLACK_INTEGRATION, name="owner", bind=lambda *_: bound.append("owner"), unbind=lambda _: runtime)
    idle = replace(SLACK_INTEGRATION, name="idle", bind=lambda *_: bound.append("idle"), unbind=lambda _: None)
    monkeypatch.setattr(integrations, "_integrations", lambda: (owner, idle))

    bind_integrations(FastAPI(), {}, AgentReplyMembershipIndex())
    assert bound == ["owner", "idle"]
    assert unbind_integrations(FastAPI()) == [("owner", runtime)]


@pytest.mark.asyncio
async def test_reply_mirror_is_the_first_integration_that_takes_the_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """Integrations after the one that takes a reply are not asked."""
    asked: list[str] = []

    async def mirror() -> None:
        asked.append("mirrored")

    def integration(name: str, *, takes: bool) -> Integration:
        def reply_mirror(*_: object) -> object:
            asked.append(name)
            return mirror() if takes else None

        return replace(SLACK_INTEGRATION, name=name, reply_mirror=reply_mirror)

    registered = (
        integration("declines", takes=False),
        integration("takes", takes=True),
        integration("later", takes=True),
    )
    monkeypatch.setattr(integrations, "_integrations", lambda: registered)

    taken = reply_mirror_for(object(), "done", config=object(), runtime_paths=object())
    assert taken is not None
    await taken
    assert asked == ["declines", "takes", "mirrored"]
