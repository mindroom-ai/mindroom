"""Temporary secrets tool for MindRoom agents.

An agent that needs a secret no Agent Keys service holds, such as a password, a token for a host without a service,
or a pairing code, calls ``request_temporary_secret``. It posts a card in the conversation; the requester types the
value into MindRoom Chat and chooses how long MindRoom keeps it. The value goes from Chat to the backend and from
there into the environment of this agent's shell commands in this conversation. It never passes through the tool,
Matrix, or the model.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from mindroom.approval.tool_approval import resolve_tool_approval_approver
from mindroom.secret_request_cards import SecretRequestCard, ask_with_card, card_reason, chat_refusal
from mindroom.secret_requests import (
    SECRET_REQUEST_TTL_SECONDS,
    TEMPORARY_SECRET_KEEP_SECONDS,
    TemporaryTarget,
    secret_holder,
)
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.secret_requests import PendingSecretRequest
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

# A name every shell takes as a variable.
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_PROVIDED_NOTE = (
    "The user gave the secret. It is in the environment variable `${name}` of your shell commands in this "
    "conversation for {minutes} minutes, and you never see its value. Use the variable inside the command that needs "
    "it, for example `printf '%s\\n' \"${name}\" | sudo -S <command>`. Never print it, write it to a file that stays, "
    "or send it anywhere but the program it is for. Ask again when it has expired."
)
_DECLINED_NOTE = "The user declined, so do not ask again for this secret in this turn."
_EXPIRED_NOTE = (
    f"No answer came in {SECRET_REQUEST_TTL_SECONDS // 60} minutes, so there is no secret. "
    "Ask again if you still need it."
)


class TemporarySecretsTools(Toolkit):
    """Tool that asks the user for a secret this agent's shell commands receive for a limited time."""

    def __init__(self, *, runtime_paths: RuntimePaths) -> None:
        self._runtime_paths = runtime_paths
        super().__init__(name="temporary_secrets", tools=[self.request_temporary_secret])

    async def request_temporary_secret(self, name: str, reason: str) -> str:
        """Ask the user for a secret your shell commands need, with a card in this conversation, and wait.

        Call this for a password, token, passphrase or code that a command needs and that you must not see: the
        user types it into a masked field in MindRoom Chat, and it becomes the environment variable `name` of your
        shell commands in this conversation. The value never reaches you or the chat.
        The user chooses how long it is kept: 15 minutes, or 24 hours. Say in `reason` when the work takes longer than
        15 minutes.
        When you also have `request_secret` and need the API key of a service it can ask for, call that instead:
        such a key never reaches your commands.
        Waits up to 15 minutes and returns `status`:
        `provided` with `minutes`, how long the variable stays set: use `$name` inside the command that needs it,
        and never print, store or send the value;
        `declined` when the user refused: do not ask again for this secret in this turn;
        `expired` when no answer came in 15 minutes.
        An `error` posts no card and says why.

        Args:
            name: The environment variable to set, such as `SUDO_PASSWORD`: letters, digits and underscores.
            reason: What you need the secret for, in a sentence or two the user reads on the card.

        """
        context = get_tool_runtime_context()
        if context is None:
            return _error("No conversation is available in this context, so no card can be posted.")
        card = await self._card(context, name, reason)
        asked = card if isinstance(card, str) else await ask_with_card(context, card, agent_name=context.agent_name)
        return _error(asked) if isinstance(asked, str) else _result(name, asked)

    async def _card(self, context: ToolRuntimeContext, name: str, reason: str) -> SecretRequestCard | str:
        """Return the card asking the turn's requester for the variable `name`, or why no card may be posted.

        The requester is the person who answers, resolved as for approval cards. The value goes to the conversation
        this call runs in, so nobody's access to the agent's stored keys is involved.
        """
        requester_id = resolve_tool_approval_approver(context.current_config, self._runtime_paths, context.requester_id)
        holder = secret_holder(context.execution_identity)
        if requester_id is None or holder is None:
            return "Only a person can answer a card, and this turn comes from an agent or a bot, so no card was posted."
        if _NAME.fullmatch(name) is None:
            return (
                "Name the environment variable your commands should receive: letters, digits and underscores, not "
                "starting with a digit, at most 64 characters."
            )
        shown_reason = card_reason(reason)
        if shown_reason is None:
            return "Give a reason: the card tells the user what you need the secret for."
        if (refusal := await chat_refusal(self._runtime_paths, noun="secret")) is not None:
            return refusal
        agent = context.current_config.agents.get(context.agent_name)
        return SecretRequestCard(
            requester_id=requester_id,
            agent_display_name=(agent.display_name if agent is not None else "") or context.agent_name,
            reason=shown_reason,
            target=TemporaryTarget(name=name, holder=holder),
        )


def _result(name: str, asked: PendingSecretRequest) -> str:
    """Return what the agent learns of a settled request: the variable and how long it stays set, never its value."""
    outcome = asked.outcome.result()
    if outcome == "saved" and asked.kept is not None:
        minutes = TEMPORARY_SECRET_KEEP_SECONDS[asked.kept] // 60
        note = _PROVIDED_NOTE.replace("{name}", name).replace("{minutes}", str(minutes))
        result: dict[str, object] = {
            "status": "provided",
            "name": name,
            "kept": asked.kept,
            "minutes": minutes,
            "note": note,
        }
    elif outcome == "declined":
        result = {"status": "declined", "name": name, "note": _DECLINED_NOTE}
    else:
        result = {"status": "expired", "name": name, "note": _EXPIRED_NOTE}
    return json.dumps(result, sort_keys=True)


def _error(message: str) -> str:
    return json.dumps({"status": "error", "message": message}, sort_keys=True)
