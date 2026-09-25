"""MindRoom compatibility adapter for the Gemini API."""

from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agno.models.google import Gemini
from agno.utils.message import normalize_tool_messages
from google.genai.types import (
    FunctionCallingConfig,
    FunctionCallingConfigMode,
    GenerateContentConfig,
    HttpOptions,
    Tool,
    ToolConfig,
)

from mindroom.model_defaults import GOOGLE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES
from mindroom.provider_tool_policy import decision_response_schema, provider_tools_disabled

if TYPE_CHECKING:
    from typing import Any

    from agno.models.message import Message
    from google.genai.types import ToolListUnion

_SAMPLING_CONTROL_NAMES = ("temperature", "top_p", "top_k")


def _provider_tool_call_id(value: object) -> str | None:
    """Return a non-empty provider tool-call ID."""
    return value if isinstance(value, str) and value else None


def _decision_config(config: object, *, vertexai: bool) -> GenerateContentConfig:
    """Build a decision request that keeps function schemas for the shared prefix and drops native tools.

    JSON output matching the caller's schema is requested wherever the endpoint is known to accept it.
    """
    generation_config = GenerateContentConfig.model_validate(config).model_copy(deep=True)
    if generation_config.cached_content:
        # Cached content may contain native tools that this request cannot inspect.
        msg = "Participation decisions cannot inspect tools in Gemini cached content"
        raise ValueError(msg)
    if generation_config.http_options is not None and generation_config.http_options.extra_body:
        msg = "Participation decisions cannot safely apply Gemini body overrides"
        raise ValueError(msg)
    # Function-calling NONE does not disable grounding or other native tools.
    declaration_tools: ToolListUnion = [
        Tool(function_declarations=tool.function_declarations)
        for tool in generation_config.tools or []
        if isinstance(tool, Tool) and tool.function_declarations
    ]
    # Without declarations, function-calling settings govern nothing and are dropped.
    function_calling = FunctionCallingConfig(mode=FunctionCallingConfigMode.NONE)
    tool_config = ToolConfig(function_calling_config=function_calling) if declaration_tools else None
    # Gemini can emit function calls under NONE and even without declarations; JSON output cannot.
    # The Gemini API accepts Gemini 2.5 JSON output beside declarations only under NONE. Vertex AI
    # acceptance is unverified, and a rejection would fail every decision, so it gets no JSON there.
    json_output = not (vertexai and declaration_tools)
    # The reply's authored output format never shapes a decision.
    return generation_config.model_copy(
        update={
            "tools": declaration_tools or None,
            "tool_config": tool_config,
            "response_mime_type": "application/json" if json_output else None,
            "response_schema": None,
            "response_json_schema": decision_response_schema() if json_output else None,
        },
    )


@dataclass
class MindRoomGoogleGemini(Gemini):
    """Gemini model that preserves provider call IDs across tool loops."""

    def get_request_params(
        self,
        system_message: str | None = None,
        response_format: dict[str, Any] | type[Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build request parameters accepted by the selected Gemini generation."""
        request_model = self
        if provider_tools_disabled():
            client_http_options = (self.client_params or {}).get("http_options")
            if client_http_options is not None and HttpOptions.model_validate(client_http_options).extra_body:
                msg = "Participation decisions cannot safely apply Gemini body overrides"
                raise ValueError(msg)
            # AGNO_COMPAT: Gemini request building mutates an authored generation_config dict.
            # Reason: Agno's Gemini.get_request_params merges request settings into the authored
            # dict in place, so a decision's tool_config and system instruction could leak
            # into later reply requests.
            # Upstream issue: https://github.com/agno-agi/agno/issues/10161, open.
            # Upstream PR: https://github.com/agno-agi/agno/pull/10162, open; copies the config.
            # Remove when: the pinned Agno builds the request from a copy of generation_config;
            # keep applying _decision_config to the returned request only.
            # Coverage: tests/test_provider_tool_policy.py::test_gemini_removes_native_tools_without_mutating_authored_config.
            request_model = copy(self)
            request_model.generation_config = deepcopy(self.generation_config)
        request_params = super(MindRoomGoogleGemini, request_model).get_request_params(
            system_message=system_message,
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
        )
        if provider_tools_disabled() and (generation_config := request_params.get("config")) is not None:
            request_params["config"] = _decision_config(generation_config, vertexai=self.get_client().vertexai)
        if not self.id.casefold().endswith(GOOGLE_PROVIDER_DEFAULT_SAMPLING_MODEL_SUFFIXES):
            return request_params

        generation_config = request_params.get("config")
        if isinstance(generation_config, GenerateContentConfig):
            generation_config.temperature = None
            generation_config.top_p = None
            generation_config.top_k = None
        elif isinstance(generation_config, dict):
            for parameter_name in _SAMPLING_CONTROL_NAMES:
                generation_config.pop(parameter_name, None)
        return request_params

    def _format_messages(
        self,
        messages: list[Message],
        compress_tool_results: bool = False,
    ) -> tuple[list[object], object]:
        normalized_messages = normalize_tool_messages(messages)
        tool_call_ids = [
            _provider_tool_call_id(tool_call.get("id"))
            for message in normalized_messages
            for tool_call in (message.tool_calls or [])
        ]
        tool_response_ids = [
            _provider_tool_call_id(message.tool_call_id)
            for message in normalized_messages
            if message.role == "tool" and message.tool_call_id is not None and message.tool_name is not None
        ]

        formatted_messages, system_message = super()._format_messages(
            normalized_messages,
            compress_tool_results=compress_tool_results,
        )
        tool_call_id_iter = iter(tool_call_ids)
        tool_response_id_iter = iter(tool_response_ids)
        for message in formatted_messages:
            for part in message.parts:
                if part.function_call is not None:
                    tool_call_id = next(tool_call_id_iter, None)
                    if tool_call_id is not None:
                        part.function_call.id = tool_call_id
                if part.function_response is not None:
                    tool_response_id = next(tool_response_id_iter, None)
                    if tool_response_id is not None:
                        part.function_response.id = tool_response_id

        return formatted_messages, system_message
