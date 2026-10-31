"""Per-thread model switching tools for AI agents."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from mindroom.authorization import addressable_responder_names
from mindroom.custom_tools.tool_payloads import custom_tool_payload, tool_context_or_error
from mindroom.thread_models import (
    clear_thread_model_override,
    resolve_thread_model_override,
    set_thread_model_override,
)
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.tool_function import ToolOutcome
from mindroom.turn_model import ModelScope, TurnBudget, resolve_turn_model

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction

_MODEL_SWITCH_WHENS = ("after-toolcall", "next-turn")


class ThreadModelTools(Toolkit):
    """Tools for switching the model used by the requester's agents and teams in the current Matrix thread."""

    def __init__(self) -> None:
        super().__init__(
            name="thread_model",
            tools=[self.list_models, self.get_thread_model, self.switch_thread_model, self.reset_thread_model],
        )

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Mark the model switch as able to stop the run, which a switch for the current response does."""
        if function.name == "switch_thread_model":
            return replace(function, may_stop_run=True)
        return function

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return custom_tool_payload("thread_model", status, **kwargs)

    @classmethod
    def _thread_context(cls) -> tuple[ToolRuntimeContext, str] | str:
        if isinstance(context := tool_context_or_error(cls._payload, "Thread model"), str):
            return context
        if context.resolved_thread_id is None:
            return cls._payload("error", message="Thread model switching requires an active thread context.")
        return context, context.resolved_thread_id

    @staticmethod
    def _addressable_entities(context: ToolRuntimeContext) -> tuple[str, ...]:
        return addressable_responder_names(
            context.requester_id,
            context.room_id,
            context.config,
            context.runtime_paths,
            context.require_agent_reply_memberships(),
        )

    async def list_models(self) -> str:
        """List the configured model aliases available for selection."""
        if isinstance(context := tool_context_or_error(self._payload, "Thread model"), str):
            return context
        return self._payload(
            "ok",
            action="list",
            models=[
                {
                    "name": name,
                    "provider": model.provider,
                    "model_id": model.id,
                }
                for name, model in sorted(context.config.models.items())
            ],
        )

    async def get_thread_model(self) -> str:
        """Return the current thread's model override for each entity it applies to and the available model names."""
        resolved = self._thread_context()
        if isinstance(resolved, str):
            return resolved
        context, thread_id = resolved
        override = resolve_thread_model_override(context.runtime_paths, thread_id, config=context.config)
        stale_fields: dict[str, object] = {}
        if override.stale:
            stale_fields["stale_overrides"] = override.stale
            stale_fields["note"] = (
                "Stale overrides name models that are no longer configured, so room-level model selection applies "
                "to those entities."
            )
        return self._payload(
            "ok",
            action="get",
            thread_id=thread_id,
            overrides=override.active,
            available_models=sorted(context.config.models),
            **stale_fields,
        )

    async def switch_thread_model(
        self,
        model_name: str,
        when: Literal["after-toolcall", "next-turn"] = "next-turn",
    ) -> str | ToolOutcome:
        """Switch the model that the agents and teams the requester may address use in the current thread.

        The override persists for this thread until reset. It can take effect
        after this tool call or from the next message in the thread.

        Args:
            model_name: Configured model name from the `models:` section of
                config.yaml (for example "default" or "opus").
            when: Whether the current response continues with the new model or
                the change starts on the next turn.

        """
        resolved = self._thread_context()
        if isinstance(resolved, str):
            return resolved
        context, thread_id = resolved
        if when not in _MODEL_SWITCH_WHENS:
            return self._payload(
                "error",
                action="switch",
                message=f"Unknown switch timing '{when}'.",
                available_when=list(_MODEL_SWITCH_WHENS),
            )
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
            entity_names=self._addressable_entities(context),
            config=context.config,
        )
        budgeted_model_name = resolve_turn_model(
            context.config,
            context.runtime_paths,
            ModelScope(entity=context.agent_name, explicit=model_name),
            # Only a switch for the current response chooses a reply model now.
            TurnBudget(
                monitor=context.budget_monitor,
                requester_id=context.requester_id,
                log_fallback=when == "after-toolcall",
            ),
        ).model_name
        if when == "after-toolcall":
            fields: dict[str, object] = {
                "note": "The current response will continue with the new model after this tool call.",
            }
            if budgeted_model_name != model_name:
                fields = {
                    "requested_model": model_name,
                    "note": (
                        f"The requester's monthly budget is used up, so the current response continues with "
                        f"`{budgeted_model_name}`; `{model_name}` stays selected for this thread."
                    ),
                }
            model = context.config.models[budgeted_model_name]
            # Only this switch stops the run: the turn continues the response on the model it names.
            return ToolOutcome(
                content=self._payload(
                    "ok",
                    action="switch",
                    thread_id=thread_id,
                    model=budgeted_model_name,
                    provider=model.provider,
                    model_id=model.id,
                    when=when,
                    **fields,
                ),
                stop_after_call=True,
            )
        if budgeted_model_name != model_name:
            note = (
                "The new model applies from the next message in this thread, but the requester's monthly budget "
                f"is used up, so replies use `{budgeted_model_name}` until it resets."
            )
        else:
            note = "The new model applies from the next message in this thread."
        model = context.config.models[model_name]
        return self._payload(
            "ok",
            action="switch",
            thread_id=thread_id,
            model=model_name,
            provider=model.provider,
            model_id=model.id,
            when=when,
            note=note,
        )

    async def reset_thread_model(self) -> str:
        """Remove the thread override of the requester's agents and teams, restoring room-level model selection."""
        resolved = self._thread_context()
        if isinstance(resolved, str):
            return resolved
        context, thread_id = resolved
        cleared = clear_thread_model_override(
            context.runtime_paths,
            thread_id,
            entity_names=self._addressable_entities(context),
            config=context.config,
        )
        return self._payload(
            "ok",
            action="reset",
            thread_id=thread_id,
            cleared=cleared,
        )
