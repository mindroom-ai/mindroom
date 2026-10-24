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
from mindroom.providers.pipeline.wrapped import WrappedProvider
from tests.agent_loop.helpers import collect, context, spec, toolkit
from tests.conftest import install_model_providers
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.fakes.summary_model import summary_model
from tests.history_helpers import _make_config
from tests.providers.openai_family_support import responses_adapter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.agent_loop.model_loop import BeforeRequestHook
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelProvider, StreamEvent

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
        replay_plan=None,
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


@pytest.mark.asyncio
async def test_a_later_loop_of_the_run_sizes_and_falls_back_by_its_own_request_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A team leader's continuation reuses the turn's hook with the turn's native settings; a fold there turns them off.

    The hook reads the provider's compaction from the request it is about to send, so a fallback in an earlier loop
    of the run cannot leave the hook compacting as text while the continuation's requests still ask the provider to.
    """
    config, runtime_paths = _small_window_config(tmp_path)

    async def summarize(**_kwargs: object) -> str:
        return "SUMMARY"

    monkeypatch.setattr(mid_turn_compaction, "summarize_run_locally", summarize)
    model = ScriptedModel(
        [calls(tool_call("probe", "call_1")), answer("paused"), calls(tool_call("probe", "call_2")), answer("done")],
    )
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
        replay_plan=None,
        deferred_tool_names=frozenset(),
        prompt_id=None,
    )

    for prompt in ("Do the task", "Continue the task"):
        await collect(
            run_agent(
                spec(model, toolkit(probe)),
                [Message(role="user", content=prompt)],
                ctx=context(),
                hooks=LoopHooks(before_request=(hook,)),
                request_template=ModelRequest(messages=(), native_compaction=_NATIVE),
            ),
        )

    assert [request.native_compaction for request in model.requests] == [_NATIVE, None, _NATIVE, None]
    assert [is_compaction_summary(request.messages[-1]) for request in model.requests] == [False, True, False, True]


def _responses_body(request: ModelRequest) -> dict[str, object]:
    """Return the request body the Responses wire sends for ``request`` on a route that stores responses."""
    kwargs = responses_request_kwargs(
        model_id="test-model",
        request=request,
        settings=ResponsesSettings(),
        wire=_RESPONSES_WIRE,
    )
    return {**kwargs, **kwargs.get("extra_body", {})}


def _small_window_config(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    return _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(model="summary", reserve_tokens=500),
        models={
            "default": ModelConfig(provider="openai", id="test-model", context_window=4000),
            "summary": ModelConfig(provider="openai", id="summary-model", context_window=100_000),
        },
    )


def _run_local_hook(config: Config, runtime_paths: RuntimePaths, model: ModelProvider) -> BeforeRequestHook:
    return mid_turn_compaction_hook(
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
        replay_plan=None,
        deferred_tool_names=frozenset(),
        prompt_id=None,
    )


def spaced() -> str:
    """Return a tool result whose tokens outnumber its characters / 4 about twice over."""
    return "x " * 5000


@pytest.mark.asyncio
@pytest.mark.parametrize("responses_route", [False, True])
async def test_a_responses_route_sizes_each_request_as_its_replay_sends_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    responses_route: bool,
) -> None:
    """The characters / 4 estimate fits a request the Responses route's own count does not, so only it compacts.

    The fold runs end to end on the owned path: the summary model comes from the provider registry.
    """
    config, runtime_paths = _small_window_config(tmp_path)
    summarizer = summary_model("SUMMARY")
    install_model_providers(monkeypatch, {"summary": summarizer})
    scripted = ScriptedModel([calls(tool_call("spaced", "call_1")), answer("done")])

    def scripted_stage(_inner: ModelProvider, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        return scripted.stream(request)

    model = (
        WrappedProvider(inner=responses_adapter(tmp_path, "test-model"), stage=scripted_stage)
        if responses_route
        else scripted
    )

    _, record = await collect(
        run_agent(
            spec(model, toolkit(spaced)),
            [Message(role="user", content="Do the task")],
            ctx=context(),
            hooks=LoopHooks(before_request=(_run_local_hook(config, runtime_paths, model),)),
        ),
    )

    assert record.content == "done"
    second = scripted.requests[1]
    if responses_route:
        assert len(summarizer.requests) == 1
        assert is_compaction_summary(second.messages[-1])
        assert "SUMMARY" in str(second.messages[-1].content)
    else:
        assert summarizer.requests == []
        assert not any(is_compaction_summary(message) for message in second.messages)
