"""Tool for handing a completion callback to a background agent."""

from __future__ import annotations

import secrets
import shlex
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.callbacks.script import build_callback_script, write_callback_script
from mindroom.config.validation import non_empty_stripped
from mindroom.constants import DEFAULT_MINDROOM_URL
from mindroom.custom_tools.external_trigger_context import (
    ExternalTriggerContextError,
    require_external_trigger_owner_context,
)
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.external_triggers.auth import mint_trigger_capability
from mindroom.external_triggers.store import (
    ExternalTriggerStore,
    ExternalTriggerStoreError,
    ExternalTriggerTarget,
)
from mindroom.runtime_state import get_api_server_address
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.sandbox_proxy import tool_placement
from mindroom.tool_system.worker_routing import shared_storage_root
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.runtime import worker_visible_storage_root

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_CALLBACK_KIND = "mindroom.callback.completed"
_MAX_LABEL_LENGTH = 200
_CALLBACKS_DIR = Path(".mindroom/callbacks")


def _callback_base_url(context: ToolRuntimeContext) -> str:
    if (configured_url := context.runtime_paths.env_value("MINDROOM_URL")) is not None:
        return configured_url.strip().rstrip("/")
    api_address = get_api_server_address()
    if api_address is not None:
        return api_address.base_url
    return DEFAULT_MINDROOM_URL


class CallbackManagerTools(Toolkit):
    """Mint a script that wakes this agent when a background task finishes."""

    def __init__(self) -> None:
        super().__init__(name="callback_manager", tools=[self.mint_callback])

    @staticmethod
    def _context() -> ToolRuntimeContext:
        context = require_external_trigger_owner_context("Callback manager")
        if not context.config.external_trigger_policy.enabled:
            msg = "Callback manager requires external triggers to be enabled."
            raise ExternalTriggerContextError(msg)
        return context

    def mint_callback(self, label: str) -> str:
        """Create a single-use script for one background task.

        Give the returned instruction to the background agent.
        When it runs the script, its result wakes this agent in the current thread.
        The wake-up needs the router in this room.
        In a room without it, such as a personal room, call `invite_router` first.

        Args:
            label: Short name for the background task shown in the wake-up message.

        """
        context: ToolRuntimeContext | None = None
        record = None
        store: ExternalTriggerStore | None = None
        try:
            context = self._context()
            store = ExternalTriggerStore(context.runtime_paths)
            normalized_label = _callback_label(label)
            # The agent's own workspace: a private agent's is its requester's, which only that requester's worker mounts.
            workspace_root = context.agent_workspace_root(create=True)
            shell_workspace_root = _shell_workspace_root(context, workspace_root)
            token, token_hash = mint_trigger_capability()
            thread_id = context.resolved_thread_id or context.thread_id
            record = store.create_single_use_capability_record(
                trigger_id=f"callback_{secrets.token_hex(8)}",
                owner_user_id=context.requester_id,
                created_by_agent_name=context.agent_name,
                created_in_room_id=context.room_id,
                created_in_thread_id=thread_id,
                target=ExternalTriggerTarget(
                    room_id=context.room_id,
                    thread_id=thread_id,
                    agent=context.agent_name,
                ),
                capability_token_hash=token_hash,
                description=normalized_label,
                allowed_kinds=(_CALLBACK_KIND,),
                config=context.config,
            )
            callback_url = f"{_callback_base_url(context)}/api/triggers/{record.trigger_id}"
            script_path = write_callback_script(
                workspace_root,
                _CALLBACKS_DIR,
                callback_id=record.trigger_id,
                script_text=build_callback_script(
                    callback_url=callback_url,
                    token=token,
                    label=normalized_label,
                ),
            )
            shell_path = str(shell_workspace_root / script_path.relative_to(workspace_root))
            return custom_tool_payload(
                "callback_manager",
                "ok",
                script_path=shell_path,
                instruction=f'When finished, run: bash {shlex.quote(shell_path)} "<short result summary>"',
            )
        except (
            ExternalTriggerContextError,
            ExternalTriggerStoreError,
            OSError,
            ValueError,
            WorkerBackendError,
        ) as exc:
            if record is not None and store is not None and context is not None:
                with suppress(ExternalTriggerStoreError):
                    store.delete_record(
                        record.trigger_id,
                        actor_user_id=context.requester_id,
                        config=context.config,
                    )
            return custom_tool_payload("callback_manager", "error", message=str(exc))


def _shell_workspace_root(context: ToolRuntimeContext, workspace_root: Path) -> Path:
    """Return the absolute path at which the agent's shell, in the primary or in its worker, sees its workspace."""
    tools = config_plugins(context.config, context.runtime_paths).tools
    agent_tools = resolve_agent_tools(context.config, context.agent_name, tools)
    if not agent_tools.runs_in_worker("shell", tool_placement(context.runtime_paths, tools)):
        return workspace_root
    # Read only for a worker shell: an unconfigured worker backend must not stop a local shell from minting.
    worker_root = worker_visible_storage_root(context.runtime_paths)
    if worker_root is None:
        # A static_runner sandbox that sees agent workspaces mounts them at MindRoom's paths, as the Kubernetes sidecar does.
        return workspace_root
    # A dedicated worker mounts each workspace at its path below the storage root, under its own storage mount.
    return worker_root / workspace_root.relative_to(shared_storage_root(context.runtime_paths.storage_root))


def _callback_label(label: str) -> str:
    """Return one short single-line callback label."""
    normalized = " ".join(non_empty_stripped(label, field_name="label").split())
    if len(normalized) > _MAX_LABEL_LENGTH:
        msg = f"label must be at most {_MAX_LABEL_LENGTH} characters"
        raise ValueError(msg)
    return normalized
