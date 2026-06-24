"""Local-only tool for managing external trigger records."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.external_triggers.store import (
    ExternalTriggerRecord,
    ExternalTriggerStore,
    ExternalTriggerStoreError,
    ExternalTriggerTarget,
    public_key_fingerprint,
)
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable


class _ExternalTriggerManagerError(RuntimeError):
    """Raised when the manager tool cannot run in the current tool context."""


class ExternalTriggerManagerTools(Toolkit):
    """Manage tool-created external trigger records in the primary runtime."""

    def __init__(self) -> None:
        super().__init__(
            name="external_trigger_manager",
            tools=[
                self.create_trigger,
                self.list_triggers,
                self.disable_trigger,
                self.delete_trigger,
                self.rotate_trigger_key,
            ],
        )

    @staticmethod
    def _payload(status: str, **fields: object) -> str:
        return custom_tool_payload("external_trigger_manager", status, **fields)

    @classmethod
    def _context(cls) -> ToolRuntimeContext:
        context = get_tool_runtime_context()
        if context is None:
            msg = "External trigger manager requires live Matrix tool context."
            raise _ExternalTriggerManagerError(msg)
        if context.runtime_paths.control_state_root is None:
            msg = "External trigger manager requires primary control state."
            raise _ExternalTriggerManagerError(msg)
        if not context.requester_id or context.requester_id == context.client.user_id:
            msg = "External trigger owner must be a human Matrix requester."
            raise _ExternalTriggerManagerError(msg)
        return context

    @classmethod
    def _with_store(cls, action: Callable[[ToolRuntimeContext, ExternalTriggerStore], str]) -> str:
        try:
            context = cls._context()
            return action(context, ExternalTriggerStore(context.runtime_paths))
        except (_ExternalTriggerManagerError, ExternalTriggerStoreError) as exc:
            return cls._payload("error", message=str(exc))

    @staticmethod
    def _is_admin(context: ToolRuntimeContext) -> bool:
        return context.requester_id in context.config.external_trigger_policy.admin_users

    @classmethod
    def _target(
        cls,
        context: ToolRuntimeContext,
        *,
        target_agent: str | None,
        target_room_id: str | None,
        target_thread_id: str | None,
        new_thread: bool,
    ) -> ExternalTriggerTarget:
        is_admin = cls._is_admin(context)
        if not is_admin and target_agent not in (None, context.agent_name):
            msg = "Only external trigger admins can target a different agent or team."
            raise ExternalTriggerStoreError(msg)
        if not is_admin and target_room_id not in (None, context.room_id):
            msg = "Only external trigger admins can target a different room."
            raise ExternalTriggerStoreError(msg)
        return ExternalTriggerTarget(
            room_id=target_room_id or context.room_id,
            thread_id=target_thread_id,
            agent=target_agent or context.agent_name,
            new_thread=new_thread,
        )

    @staticmethod
    def _record_payload(record: ExternalTriggerRecord) -> dict[str, object]:
        return {
            "trigger_id": record.trigger_id,
            "enabled": record.enabled,
            "description": record.description,
            "owner_user_id": record.owner_user_id,
            "target": record.target.model_dump(mode="json"),
            "key_id": record.key_id,
            "public_key_fingerprint": record.public_key_fingerprint,
            "allowed_kinds": list(record.allowed_kinds),
            "replay_window_seconds": record.replay_window_seconds,
            "max_body_bytes": record.max_body_bytes,
            "version": record.version,
            "auth_epoch": record.auth_epoch,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    def create_trigger(
        self,
        trigger_id: str,
        public_key: str,
        key_id: str = "default",
        description: str = "",
        target_agent: str | None = None,
        target_room_id: str | None = None,
        target_thread_id: str | None = None,
        new_thread: bool = False,
        allowed_kinds: list[str] | None = None,
        replay_window_seconds: int | None = None,
        max_body_bytes: int | None = None,
    ) -> str:
        """Create an external trigger endpoint using caller-supplied public key material.

        Args:
            trigger_id: Route-safe trigger id for `/api/triggers/{trigger_id}`.
            public_key: Base64 Ed25519 public key for request verification.
            key_id: Expected signing key id header.
            description: Human-readable purpose.
            target_agent: Optional target agent/team; non-admin callers use the current one.
            target_room_id: Optional target room; non-admin callers use the current room.
            target_thread_id: Optional Matrix thread id for delivery.
            new_thread: Start a fresh thread instead of appending to target_thread_id.
            allowed_kinds: Optional list of accepted payload kind values.
            replay_window_seconds: Optional replay window capped by config policy.
            max_body_bytes: Optional body cap capped by config policy.

        """

        def create(context: ToolRuntimeContext, store: ExternalTriggerStore) -> str:
            target = self._target(
                context,
                target_agent=target_agent,
                target_room_id=target_room_id,
                target_thread_id=target_thread_id,
                new_thread=new_thread,
            )
            record = store.create_record(
                trigger_id=trigger_id,
                owner_user_id=context.requester_id,
                created_by_agent_name=context.agent_name,
                created_in_room_id=context.room_id,
                created_in_thread_id=context.resolved_thread_id or context.thread_id,
                target=target,
                public_key=public_key,
                key_id=key_id,
                description=description,
                allowed_kinds=allowed_kinds or (),
                replay_window_seconds=replay_window_seconds,
                max_body_bytes=max_body_bytes,
                config=context.config,
            )
            return self._payload(
                "ok",
                action="create",
                trigger=self._record_payload(record),
                endpoint_path=f"/api/triggers/{record.trigger_id}",
                public_key_fingerprint=record.public_key_fingerprint,
            )

        return self._with_store(create)

    def list_triggers(self) -> str:
        """List external triggers owned by the requester, or all triggers for admins."""

        def list_records(context: ToolRuntimeContext, store: ExternalTriggerStore) -> str:
            owner_user_id = None if self._is_admin(context) else context.requester_id
            records = store.list_records(owner_user_id=owner_user_id)
            return self._payload(
                "ok",
                action="list",
                triggers=[self._record_payload(record) for record in records],
            )

        return self._with_store(list_records)

    def disable_trigger(self, trigger_id: str, enabled: bool = False) -> str:
        """Enable or disable a trigger owned by the requester, unless requester is admin."""

        def set_enabled(context: ToolRuntimeContext, store: ExternalTriggerStore) -> str:
            record = store.set_enabled(
                trigger_id,
                enabled=enabled,
                actor_user_id=context.requester_id,
                config=context.config,
            )
            return self._payload("ok", action="set_enabled", trigger=self._record_payload(record))

        return self._with_store(set_enabled)

    def delete_trigger(self, trigger_id: str) -> str:
        """Delete a trigger owned by the requester, unless requester is admin."""

        def delete(context: ToolRuntimeContext, store: ExternalTriggerStore) -> str:
            store.delete_record(
                trigger_id,
                actor_user_id=context.requester_id,
                config=context.config,
            )
            return self._payload("ok", action="delete", trigger_id=trigger_id)

        return self._with_store(delete)

    def rotate_trigger_key(self, trigger_id: str, public_key: str, key_id: str = "default") -> str:
        """Rotate a trigger public key without accepting or returning private key material."""

        def rotate(context: ToolRuntimeContext, store: ExternalTriggerStore) -> str:
            record = store.rotate_key(
                trigger_id,
                public_key=public_key,
                key_id=key_id,
                actor_user_id=context.requester_id,
                config=context.config,
            )
            return self._payload(
                "ok",
                action="rotate_key",
                trigger=self._record_payload(record),
                public_key_fingerprint=public_key_fingerprint(public_key),
            )

        return self._with_store(rotate)
