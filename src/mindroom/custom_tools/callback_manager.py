"""Tool for handing a completion callback to a background agent."""

from __future__ import annotations

import shlex
from contextlib import suppress
from typing import TYPE_CHECKING

from agno.tools import Toolkit
from pydantic import ValidationError

from mindroom.callbacks.script import build_callback_script, write_callback_script
from mindroom.callbacks.store import CallbackStore, CallbackStoreError
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context
from mindroom.tool_system.worker_routing import resolve_agent_owned_path

if TYPE_CHECKING:
    from pathlib import Path

_DEFAULT_BASE_URL = "http://127.0.0.1:8765"


class _CallbackManagerError(RuntimeError):
    """Raised when a callback cannot be minted in the current context."""


def _callback_base_url(context: ToolRuntimeContext) -> str:
    configured_url = context.runtime_paths.env_value("MINDROOM_URL")
    base_url = configured_url.strip() if configured_url is not None else ""
    return (base_url or _DEFAULT_BASE_URL).rstrip("/")


class CallbackManagerTools(Toolkit):
    """Mint a script that wakes this agent when a background task finishes."""

    def __init__(self) -> None:
        super().__init__(name="callback_manager", tools=[self.mint_callback])

    @staticmethod
    def _context() -> ToolRuntimeContext:
        context = get_tool_runtime_context()
        if context is None:
            msg = "Callback manager requires live Matrix tool context."
            raise _CallbackManagerError(msg)
        if context.runtime_paths.control_state_root is None:
            msg = "Callback manager requires primary control state."
            raise _CallbackManagerError(msg)
        if not context.requester_id or context.requester_id == context.client.user_id:
            msg = "Callback owner must be a human Matrix requester."
            raise _CallbackManagerError(msg)
        return context

    def mint_callback(self, label: str) -> str:
        """Create a single-use script for one background task.

        Give the returned instruction to the background agent.
        When it runs the script, its result wakes this agent in the current thread.

        Args:
            label: Short name for the background task shown in the wake-up message.

        """
        record = None
        store: CallbackStore | None = None
        script_path: Path | None = None
        try:
            context = self._context()
            store = CallbackStore(context.runtime_paths)
            record, token = store.mint_record(
                owner_user_id=context.requester_id,
                room_id=context.room_id,
                thread_id=context.resolved_thread_id or context.thread_id,
                agent_name=context.agent_name,
                label=label,
            )
            callback_url = f"{_callback_base_url(context)}/api/callbacks/{record.callback_id}"
            script_path = write_callback_script(
                _workspace_callbacks_dir(context),
                callback_id=record.callback_id,
                script_text=build_callback_script(callback_url=callback_url, token=token),
            )
            instruction = f'When finished, run: bash {shlex.quote(str(script_path))} "<short result summary>"'
            return custom_tool_payload(
                "callback_manager",
                "ok",
                script_path=str(script_path),
                instruction=instruction,
            )
        except (_CallbackManagerError, CallbackStoreError, OSError, ValidationError, ValueError) as exc:
            if script_path is not None:
                with suppress(OSError):
                    script_path.unlink(missing_ok=True)
            if record is not None and store is not None:
                with suppress(CallbackStoreError):
                    store.delete(record.callback_id)
            return custom_tool_payload("callback_manager", "error", message=str(exc))


def _workspace_callbacks_dir(context: ToolRuntimeContext) -> Path:
    return resolve_agent_owned_path(
        ".mindroom/callbacks",
        agent_name=context.agent_name,
        base_storage_path=context.runtime_paths.storage_root,
    )
