"""Local-only tool for minting one-shot completion callbacks."""

from __future__ import annotations

from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from agno.tools import Toolkit
from pydantic import ValidationError

from mindroom.callbacks.script import build_callback_script, write_callback_script
from mindroom.callbacks.store import CallbackRecord, CallbackStore, CallbackStoreError
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context
from mindroom.tool_system.worker_routing import resolve_agent_owned_path

if TYPE_CHECKING:
    from collections.abc import Callable

_DEFAULT_BASE_URL = "http://127.0.0.1:8765"
_ON_EXPIRY_MODES = ("notify", "silent")


class _CallbackManagerError(RuntimeError):
    """Raised when the manager tool cannot run in the current tool context."""


def _expires_at_iso(expires_at: int) -> str:
    return datetime.fromtimestamp(expires_at, tz=UTC).isoformat(timespec="seconds")


def _callback_base_url(context: ToolRuntimeContext) -> str:
    base_url = context.runtime_paths.env_value("MINDROOM_URL") or _DEFAULT_BASE_URL
    return base_url.strip().rstrip("/")


class CallbackManagerTools(Toolkit):
    """Mint, list, and revoke one-shot completion callbacks in the primary runtime."""

    def __init__(self) -> None:
        super().__init__(
            name="callback_manager",
            tools=[
                self.mint_callback,
                self.list_callbacks,
                self.revoke_callback,
            ],
        )

    @staticmethod
    def _payload(status: str, **fields: object) -> str:
        return custom_tool_payload("callback_manager", status, **fields)

    @classmethod
    def _context(cls) -> ToolRuntimeContext:
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

    @classmethod
    def _with_store(cls, action: Callable[[ToolRuntimeContext, CallbackStore], str]) -> str:
        try:
            context = cls._context()
            return action(context, CallbackStore(context.runtime_paths))
        except (_CallbackManagerError, CallbackStoreError, ValidationError) as exc:
            return cls._payload("error", message=str(exc))

    @staticmethod
    def _is_admin(context: ToolRuntimeContext) -> bool:
        return context.requester_id in context.config.external_trigger_policy.admin_users

    @staticmethod
    def _record_payload(record: CallbackRecord) -> dict[str, object]:
        return {
            "callback_id": record.callback_id,
            "label": record.label,
            "owner_user_id": record.owner_user_id,
            "target": {
                "room_id": record.target_room_id,
                "thread_id": record.target_thread_id,
                "agent": record.target_agent,
            },
            "on_expiry": record.on_expiry,
            "max_uses": record.max_uses,
            "uses_left": record.uses_left,
            "consumed": record.uses_left == 0,
            "script_path": record.script_path,
            "created_at": record.created_at,
            "expires_at": _expires_at_iso(record.expires_at),
        }

    def mint_callback(
        self,
        label: str,
        ttl_seconds: int = 86400,
        max_uses: int = 1,
        on_expiry: str = "notify",
    ) -> str:
        """Mint one ephemeral bearer-token callback bound to the current room and thread.

        The result includes a ready-to-run script in this agent's workspace, a raw curl
        line, and a one-sentence brief to paste into a spawned sub-agent's prompt. The
        sub-agent needs only bash + curl; when it runs the script, the completion message
        lands in this thread and wakes this agent. Unfired callbacks with
        ``on_expiry="notify"`` post a timeout notice instead, so exactly one wake-up is
        guaranteed.

        Args:
            label: Human tag shown in the wake-up message.
            ttl_seconds: Callback lifetime, capped by ``callback_policy.max_ttl_seconds``.
            max_uses: Allowed fires (>1 allows progress pings), capped by policy.
            on_expiry: "notify" posts a timeout notice when the callback expires unfired;
                "silent" just deletes it.

        """

        def mint(context: ToolRuntimeContext, store: CallbackStore) -> str:
            if not context.config.callback_policy.enabled:
                msg = "Callbacks are disabled by callback_policy.enabled."
                raise _CallbackManagerError(msg)
            if on_expiry not in _ON_EXPIRY_MODES:
                msg = "on_expiry must be 'notify' or 'silent'."
                raise _CallbackManagerError(msg)
            on_expiry_mode: Literal["notify", "silent"] = "notify" if on_expiry == "notify" else "silent"
            record, token = store.mint_record(
                owner_user_id=context.requester_id,
                created_by_agent_name=context.agent_name,
                created_in_room_id=context.room_id,
                created_in_thread_id=context.resolved_thread_id or context.thread_id,
                target_room_id=context.room_id,
                target_thread_id=context.resolved_thread_id or context.thread_id,
                target_agent=context.agent_name,
                label=label,
                ttl_seconds=ttl_seconds,
                max_uses=max_uses,
                on_expiry=on_expiry_mode,
                config=context.config,
            )
            callback_url = f"{_callback_base_url(context)}/api/callbacks/{record.callback_id}"
            expires_at_iso = _expires_at_iso(record.expires_at)
            try:
                script_path = write_callback_script(
                    _workspace_callbacks_dir(context),
                    callback_id=record.callback_id,
                    script_text=build_callback_script(
                        label=record.label,
                        callback_url=callback_url,
                        token=token,
                        expires_at_text=expires_at_iso,
                    ),
                )
            except (OSError, ValueError) as exc:
                store.delete_record(record.callback_id, actor_user_id=context.requester_id, config=context.config)
                msg = f"Failed to write callback script: {exc}"
                raise _CallbackManagerError(msg) from exc
            record = store.set_script_path(record.callback_id, str(script_path))
            curl_snippet = (
                f"curl -fsS -X POST '{callback_url}' "
                f"-H 'Authorization: Bearer {token}' -H 'Content-Type: application/json' "
                '--data \'{"status":"done","message":"<one-line summary>"}\''
            )
            brief_snippet = (
                f'When finished, run: bash {script_path} done "<one-line summary>" '
                "(use 'failed' or 'blocked' instead of 'done' when appropriate)."
            )
            return self._payload(
                "ok",
                action="mint",
                callback_id=record.callback_id,
                script_path=str(script_path),
                curl_snippet=curl_snippet,
                brief_snippet=brief_snippet,
                expires_at=expires_at_iso,
                callback=self._record_payload(record),
            )

        return self._with_store(mint)

    def list_callbacks(self) -> str:
        """List callbacks owned by the requester, or all callbacks for admins."""

        def list_records(context: ToolRuntimeContext, store: CallbackStore) -> str:
            owner_user_id = None if self._is_admin(context) else context.requester_id
            records = store.list_records(owner_user_id=owner_user_id)
            return self._payload(
                "ok",
                action="list",
                callbacks=[self._record_payload(record) for record in records],
            )

        return self._with_store(list_records)

    def revoke_callback(self, callback_id: str) -> str:
        """Revoke one callback owned by the requester (admins may revoke any owner's)."""

        def revoke(context: ToolRuntimeContext, store: CallbackStore) -> str:
            record = store.delete_record(
                callback_id,
                actor_user_id=context.requester_id,
                config=context.config,
            )
            _delete_script_best_effort(record.script_path)
            return self._payload("ok", action="revoke", callback_id=callback_id)

        return self._with_store(revoke)


def _workspace_callbacks_dir(context: ToolRuntimeContext) -> Path:
    """Return the calling agent's workspace directory for callback scripts."""
    return resolve_agent_owned_path(
        ".mindroom/callbacks",
        agent_name=context.agent_name,
        base_storage_path=context.runtime_paths.storage_root,
    )


def _delete_script_best_effort(script_path: str | None) -> None:
    """Remove one generated callback script without failing the revoke."""
    if script_path is None:
        return
    with suppress(OSError):
        Path(script_path).unlink(missing_ok=True)
