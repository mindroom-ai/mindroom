"""Per-thread model switching tools for AI agents."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from mindroom.authorization import addressable_responder_names
from mindroom.config.models import unsupported_level_message
from mindroom.custom_tools.tool_payloads import custom_tool_payload, tool_context_or_error
from mindroom.thread_models import (
    THREAD_MODEL_STORAGE_ERROR,
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

_ReadAction = Literal["list", "get"]
_WriteAction = Literal["switch", "reset"]
_SwitchWhen = Literal["after-toolcall", "next-turn"]


class ThreadModelTools(Toolkit):
    """Tools for switching the model used by the requester's agents and teams in the current Matrix thread."""

    offer_background_after = False
    offer_output_path = False

    def __init__(self) -> None:
        super().__init__(
            name="thread_model",
            tools=[self.thread_model_read, self.thread_model_write],
        )

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Mark `thread_model_write` as able to stop the run, and allow redirecting `thread_model_read` output."""
        if function.name == "thread_model_write":
            return replace(function, may_stop_run=True)
        if function.name == "thread_model_read":
            return replace(function, offer_output_path=True)
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

    async def thread_model_read(self, action: _ReadAction) -> str:
        """List configured models or inspect the current thread's model override.

        - `list`: List the configured model aliases available for selection.
        - `get`: Return the current thread's model override for each entity it applies to and the available model names.

        Args:
            action: Read operation to perform (`list` or `get`).

        """
        if action == "list":
            return await self._list_models()
        if action == "get":
            return await self._get_thread_model()
        return self._payload(
            "error",
            message=f"Unsupported thread_model_read action {action!r}. Expected one of: list, get.",
        )

    async def thread_model_write(
        self,
        action: _WriteAction,
        model_name: str | None = None,
        when: _SwitchWhen | None = None,
    ) -> str | ToolOutcome:
        """Switch or clear the model override for the current Matrix thread.

        - `switch`: Switch the model that the requester's addressable agents and teams use in the current thread until reset, taking effect after this tool call or from the next turn.
        - `reset`: Remove the thread override of the requester's agents and teams, restoring room-level model selection.

        Args:
            action: Write operation to perform (`switch` or `reset`).
            model_name: Configured model name from `models:` in config.yaml (for example "default" or "opus"), or `name@level` for one of the reasoning levels `thread_model_read(action="list")` shows (required for `switch`).
            when: Whether the current response continues with the new model (`after-toolcall`) or the change starts on the next turn (`next-turn`, default) (`switch` only).

        """
        if action == "switch":
            if not isinstance(model_name, str) or not model_name.strip():
                return self._payload("error", action="switch", message="model_name is required for action='switch'.")
            resolved_when: _SwitchWhen = "next-turn" if when is None else when
            if resolved_when not in ("after-toolcall", "next-turn"):
                return self._payload(
                    "error",
                    action="switch",
                    message=f"Unsupported when value {resolved_when!r}. Expected 'after-toolcall' or 'next-turn'.",
                )
            return await self._switch_thread_model(model_name.strip(), when=resolved_when)
        if action == "reset":
            unexpected = [name for name, value in (("model_name", model_name), ("when", when)) if value is not None]
            if unexpected:
                names = ", ".join(f"'{name}'" for name in unexpected)
                return self._payload(
                    "error",
                    action="reset",
                    message=f"Parameter(s) {names} cannot be used with action='reset'.",
                )
            return await self._reset_thread_model()
        return self._payload(
            "error",
            message=f"Unsupported thread_model_write action {action!r}. Expected one of: switch, reset.",
        )

    async def _list_models(self) -> str:
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
                    **({"reasoning": model.reasoning.model_dump()} if model.reasoning else {}),
                }
                for name, model in sorted(context.config.models.items())
            ],
        )

    async def _get_thread_model(self) -> str:
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

    async def _switch_thread_model(
        self,
        model_name: str,
        *,
        when: _SwitchWhen = "next-turn",
    ) -> str | ToolOutcome:
        resolved = self._thread_context()
        if isinstance(resolved, str):
            return resolved
        context, thread_id = resolved
        # The turn runs on its own config while live reloads move on: a model removed since must not be saved for
        # later turns, and this turn cannot continue on one added since.
        current = context.current_config
        switchable = context.config.models.keys() & current.models.keys()
        if model_name not in context.config.models or model_name not in current.models:
            reason = unsupported_level_message(current.models, model_name)
            return self._payload(
                "error",
                action="switch",
                message=f"{reason}." if reason else f"Unknown model '{model_name}'.",
                available_models=sorted(switchable),
            )
        entity_names = self._addressable_entities(context)
        try:
            set_thread_model_override(
                context.runtime_paths,
                thread_id=thread_id,
                model_name=model_name,
                entity_names=entity_names,
                config=context.config,
            )
        except OSError:
            return self._payload("error", action="switch", message=THREAD_MODEL_STORAGE_ERROR)
        # A switch reads the config it takes effect in: the turn's own for the current response, the current one for
        # later turns.
        config = context.config if when == "after-toolcall" else current
        budgeted_model_name = resolve_turn_model(
            config,
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
        model = config.models[model_name]
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

    async def _reset_thread_model(self) -> str:
        resolved = self._thread_context()
        if isinstance(resolved, str):
            return resolved
        context, thread_id = resolved
        entity_names = self._addressable_entities(context)
        try:
            cleared = clear_thread_model_override(
                context.runtime_paths,
                thread_id,
                entity_names=entity_names,
                config=context.config,
            )
        except OSError:
            return self._payload("error", action="reset", message=THREAD_MODEL_STORAGE_ERROR)
        return self._payload(
            "ok",
            action="reset",
            thread_id=thread_id,
            cleared=cleared,
        )
