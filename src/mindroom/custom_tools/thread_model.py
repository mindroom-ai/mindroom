"""Per-thread model switching tools for AI agents."""

from __future__ import annotations

from agno.tools import Toolkit

from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.thread_models import (
    clear_thread_model_override,
    resolve_thread_model_override,
    set_thread_model_override,
)
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context


class ThreadModelTools(Toolkit):
    """Tools for switching the model used by all agents in the current Matrix thread."""

    def __init__(self) -> None:
        super().__init__(
            name="thread_model",
            tools=[self.get_thread_model, self.switch_thread_model, self.reset_thread_model],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return custom_tool_payload("thread_model", status, **kwargs)

    @classmethod
    def _thread_context(cls) -> tuple[ToolRuntimeContext, str] | str:
        context = get_tool_runtime_context()
        if context is None:
            return cls._payload("error", message="Thread model tool context is unavailable in this runtime path.")
        if context.resolved_thread_id is None:
            return cls._payload("error", message="Thread model switching requires an active thread context.")
        return context, context.resolved_thread_id

    async def get_thread_model(self) -> str:
        """Return the current thread's model override and the available model names."""
        resolved = self._thread_context()
        if isinstance(resolved, str):
            return resolved
        context, thread_id = resolved
        override = resolve_thread_model_override(
            context.runtime_paths,
            thread_id,
            configured_models=context.config.models,
        )
        stale_fields: dict[str, object] = {}
        if override.stale is not None:
            stale_fields["stale_override"] = override.stale
            stale_fields["note"] = (
                "The stored override names a model that is no longer configured, so agents use their configured models."
            )
        return self._payload(
            "ok",
            action="get",
            thread_id=thread_id,
            override=override.active,
            available_models=sorted(context.config.models),
            **stale_fields,
        )

    async def switch_thread_model(self, model_name: str) -> str:
        """Switch the model that all agents and teams use in the current thread.

        The override persists for this thread until reset. It takes effect from
        the next message in the thread; the current response keeps the model it
        started with.

        Args:
            model_name: Configured model name from the `models:` section of
                config.yaml (for example "default" or "opus").

        """
        resolved = self._thread_context()
        if isinstance(resolved, str):
            return resolved
        context, thread_id = resolved
        if model_name not in context.config.models:
            return self._payload(
                "error",
                action="switch",
                message=f"Unknown model '{model_name}'.",
                available_models=sorted(context.config.models),
            )
        set_thread_model_override(
            context.runtime_paths,
            thread_id=thread_id,
            model_name=model_name,
            room_id=context.room_id,
            set_by=context.requester_id,
        )
        model = context.config.models[model_name]
        return self._payload(
            "ok",
            action="switch",
            thread_id=thread_id,
            model=model_name,
            provider=model.provider,
            model_id=model.id,
            note="The new model applies from the next message in this thread.",
        )

    async def reset_thread_model(self) -> str:
        """Remove the model override for the current thread, restoring configured models."""
        resolved = self._thread_context()
        if isinstance(resolved, str):
            return resolved
        context, thread_id = resolved
        cleared = clear_thread_model_override(context.runtime_paths, thread_id)
        return self._payload(
            "ok",
            action="reset",
            thread_id=thread_id,
            cleared=cleared,
        )
