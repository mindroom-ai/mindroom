"""What the mid-turn compaction hook changes for the rest of a run on the owned loop."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.agent_loop.run import run_agent
from mindroom.config.models import CompactionConfig, ModelConfig
from mindroom.history import mid_turn_compaction
from mindroom.history.mid_turn_compaction import mid_turn_compaction_hook
from mindroom.history.summary_message import is_compaction_summary
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, NativeCompactionReplay
from mindroom.providers.openai.responses_wire import ResponsesWire, responses_request_kwargs
from mindroom.providers.openai.settings import ResponsesSettings
from tests.agent_loop.helpers import collect, context, spec, toolkit
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.history_helpers import _make_config

if TYPE_CHECKING:
    from pathlib import Path

_NATIVE = NativeCompactionReplay(route="test-route", threshold=2000)
_RESPONSES_WIRE = ResponsesWire(route="openai", provider_name="openai", store=True, explicit_breakpoint=False)


def probe() -> str:
    """Return one tool result larger than the replay window."""
    return "r" * 40_000


@pytest.mark.asyncio
@pytest.mark.parametrize("summary_fails", [False, True])
async def test_a_text_fallback_on_a_native_route_stops_asking_the_provider_to_compact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    summary_fails: bool,
) -> None:
    """Once the provider's compaction left a request over the window, later requests of the run run without it.

    On the Responses wire that means no ``context_management`` and the route's own storage setting again; a failed
    fallback changes nothing, so the provider keeps compacting.
    """
    config, runtime_paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(model="summary", reserve_tokens=500),
        models={
            "default": ModelConfig(provider="openai", id="test-model", context_window=4000),
            "summary": ModelConfig(provider="openai", id="summary-model", context_window=100_000),
        },
    )

    async def summarize(**_kwargs: object) -> str:
        if summary_fails:
            msg = "summary model unavailable"
            raise RuntimeError(msg)
        return "SUMMARY"

    monkeypatch.setattr(mid_turn_compaction, "summarize_run_locally", summarize)
    model = ScriptedModel([calls(tool_call("probe", "call_1")), answer("done")])
    hook = mid_turn_compaction_hook(
        config=config,
        runtime_paths=runtime_paths,
        entity_name="test_agent",
        scope_id="test_agent",
        is_team=False,
        model_name="default",
        model=model,
        history_settings=config.resolve_entity("test_agent").history_settings,
        store=None,
        session=None,
        scope=None,
        native_compaction=_NATIVE,
        deferred_tool_names=frozenset(),
        prompt_id=None,
    )

    _, record = await collect(
        run_agent(
            spec(model, toolkit(probe)),
            [Message(role="user", content="Do the task")],
            ctx=context(),
            hooks=LoopHooks(before_request=(hook,)),
            request_template=ModelRequest(messages=(), native_compaction=_NATIVE),
        ),
    )

    assert record.content == "done"
    first, second = model.requests
    assert first.native_compaction == _NATIVE
    first_body = _responses_body(first)
    assert first_body["context_management"] == [{"type": "compaction", "compact_threshold": 2000}]
    assert first_body["store"] is False
    second_body = _responses_body(second)
    if summary_fails:
        assert second.native_compaction == _NATIVE
        assert second_body["context_management"] == first_body["context_management"]
        assert second_body["store"] is False
        assert not any(is_compaction_summary(message) for message in second.messages)
    else:
        assert second.native_compaction is None
        assert "context_management" not in second_body
        assert second_body["store"] is True
        assert is_compaction_summary(second.messages[-1])


def _responses_body(request: ModelRequest) -> dict[str, object]:
    """Return the request body the Responses wire sends for ``request`` on a route that stores responses."""
    kwargs = responses_request_kwargs(
        model_id="test-model",
        request=request,
        settings=ResponsesSettings(),
        wire=_RESPONSES_WIRE,
    )
    return kwargs["extra_body"]
