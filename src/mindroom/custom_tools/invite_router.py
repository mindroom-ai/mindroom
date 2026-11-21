"""Deprecated agent tool that once invited the router to the current Matrix room."""

from __future__ import annotations

from mindroom.tool_system.authoring import Toolkit

# LEGACY_COMPAT: the `invite_router` tool name in agent configs and prompts.
# Legacy format: an agent's `tools` list, or a prompt, that names `invite_router`, which invited the router account.
# Last legacy release: every release before the router-free change ships, v2026.11.459 the newest when written,
#   offers the working tool; replacement: unreleased, rooms need no router account, so nothing replaces the call.
# Handling: the name stays registered so such configs load; the call invites nobody and says it is not needed.
#   Remove the stub after the first router-free release.
# Coverage: tests/test_invite_router_tool.py::test_invite_router_answers_no_longer_needed


class InviteRouterTools(Toolkit):
    """Keep the retired `invite_router` name callable."""

    def __init__(self) -> None:
        super().__init__(name="invite_router", tools=[self.invite_router])

    async def invite_router(self) -> str:
        """No longer needed: agents need no router."""
        return "invite_router is no longer needed: approvals, commands and access work without another account."
