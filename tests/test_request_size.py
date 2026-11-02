"""One rule sizes every request a reply sends: from the latest answer whose request it extends, else by estimate.

``RequestSizer`` is the owning seam; the answers carry the identity of the request they answered, as the model loop
records it (``llm.request_identity``).
"""
# ruff: noqa: D103

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.history.request_size import RequestSize, request_sizer
from mindroom.llm.media import File, Image
from mindroom.llm.messages import (
    QUEUED_MESSAGE_NOTICE_MARKER_KEY,
    REWRITTEN_ATTEMPT_KEY,
    Message,
    model_provenance,
)
from mindroom.llm.provider import ModelIdentity, ModelRequest, NativeCompactionReplay, ToolSpec
from mindroom.llm.request_identity import REQUEST_IDENTITY_KEY, RequestDigests
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.native_compaction import NATIVE_CHECKPOINT_KEY
from mindroom.session_storage.agent_storage import create_session_storage
from tests.conftest import bind_runtime_paths, prepare_history_for_run_for_test, seed_session, test_runtime_paths
from tests.fakes.scripted_model import ScriptedModel
from tests.history_helpers import _completed_run, _make_config, _session, stamp_request_identity
from tests.providers.golden_support import load_golden
from tests.providers.openai_family_support import golden_provider

if TYPE_CHECKING:
    from mindroom.llm.provider import ModelProvider

_REPORTED = 1_000


def _config(tmp_path: Path) -> Config:
    authored = {"models": {"default": {"provider": "openai", "id": "scripted-model"}}, "router": {"model": "default"}}
    return bind_runtime_paths(Config.model_validate(authored), test_runtime_paths(tmp_path))


def _answered(model: ScriptedModel, request: ModelRequest, content: str, *, reported: int = _REPORTED) -> Message:
    """Return the answer ``model`` gave ``request``, stamped as the loop stamps it."""
    answer = Message(
        role="assistant",
        content=content,
        metrics=MessageMetrics(tokens=TokenCounts(input_tokens=reported, output_tokens=5)),
        provider_data=model_provenance(model.identity.id, model.identity.provider),
    )
    stamp_request_identity(answer, model, request)
    assert REQUEST_IDENTITY_KEY in (answer.provider_data or {})
    return answer


def _sized(tmp_path: Path, model: ScriptedModel, request: ModelRequest) -> RequestSize:
    return request_sizer(model, _config(tmp_path), "default").size(request)


def _first(*messages: Message) -> ModelRequest:
    return ModelRequest(messages=(Message(role="system", content="You help."), *messages))


def test_a_request_after_an_answer_is_its_reported_size_plus_what_follows(tmp_path: Path) -> None:
    model = ScriptedModel([])
    first = _first(Message(role="user", content="Hi"))
    answer = _answered(model, first, "Hello")
    later = replace(first, messages=(*first.messages, answer, Message(role="user", content="More please")))

    size = _sized(tmp_path, model, later)

    assert size == RequestSize(_REPORTED + model.request_tokens(later, start=len(first.messages)), reported=True)


def test_a_history_copy_of_an_answer_anchors_the_next_reply(tmp_path: Path) -> None:
    model = ScriptedModel([])
    first = _first(Message(role="user", content="Hi"))
    answer = _answered(model, first, "Hello")
    history = [replace(message, from_history=True) for message in (*first.messages[1:], answer)]
    reply = _first(*history, Message(role="user", content="Next question"))

    assert _sized(tmp_path, model, reply).reported


@pytest.mark.parametrize(
    "change",
    ["system prompt", "tools", "earlier message", "another model"],
)
def test_a_changed_request_before_the_answer_is_sized_by_estimate(tmp_path: Path, change: str) -> None:
    model = ScriptedModel([])
    first = _first(Message(role="user", content="Hi"))
    answer = _answered(model, first, "Hello")
    messages = [*first.messages, answer, Message(role="user", content="More")]
    request = replace(first, messages=tuple(messages))
    if change == "system prompt":
        request = replace(request, messages=(Message(role="system", content="You help briefly."), *messages[1:]))
    elif change == "tools":
        request = replace(request, tools=(ToolSpec(name="probe", description=None, parameters={}, strict=None),))
    elif change == "earlier message":
        request = replace(request, messages=(messages[0], Message(role="user", content="Hey"), *messages[2:]))
    else:
        model = ScriptedModel([], identity=ModelIdentity(id="other-model", provider="Synthetic"))

    assert _sized(tmp_path, model, request) == RequestSize(model.request_tokens(request), reported=False)


def test_a_queued_notice_the_reply_moved_keeps_the_answer_anchoring(tmp_path: Path) -> None:
    model = ScriptedModel([])
    notice = Message(
        role="user",
        content="A newer message waits.",
        provider_data={QUEUED_MESSAGE_NOTICE_MARKER_KEY: True},
    )
    first = _first(Message(role="user", content="Hi"), notice)
    answer = _answered(model, first, "Hello")
    later = _first(Message(role="user", content="Hi"), answer, notice)

    assert _sized(tmp_path, model, later).reported


def test_an_answer_to_a_rewritten_attempt_never_anchors(tmp_path: Path) -> None:
    model = ScriptedModel([])
    first = _first(Message(role="user", content="Hi"))
    answer = _answered(model, first, "Hello")
    answer.provider_data = {
        key: value for key, value in (answer.provider_data or {}).items() if key != REQUEST_IDENTITY_KEY
    } | {REWRITTEN_ATTEMPT_KEY: True}
    later = replace(first, messages=(*first.messages, answer, Message(role="user", content="More")))

    assert not _sized(tmp_path, model, later).reported


def test_a_rewritten_attachment_ends_anchoring(tmp_path: Path) -> None:
    model = ScriptedModel([])
    path = tmp_path / "notes.txt"
    path.write_text("short")
    prompt = Message(role="user", content="Read this", files=[File(filepath=str(path))])
    first = _first(prompt)
    answer = _answered(model, first, "Read")
    later = replace(first, messages=(*first.messages, answer, Message(role="user", content="And now?")))
    assert _sized(tmp_path, model, later).reported

    path.write_text("a much longer file than before")

    assert not _sized(tmp_path, model, later).reported


def test_a_rewritten_inline_attachment_ends_anchoring(tmp_path: Path) -> None:
    model = ScriptedModel([])
    image = Image(content=b"first chart", mime_type="image/png")
    prompt = Message(role="user", content="Read this", images=[image])
    first = _first(prompt)
    answer = _answered(model, first, "Read")
    later = replace(first, messages=(*first.messages, answer, Message(role="user", content="And now?")))
    assert _sized(tmp_path, model, later).reported

    prompt.images = [replace(image, content=b"second chart")]

    assert not _sized(tmp_path, model, later).reported


def test_media_at_a_url_leaves_every_later_request_without_identity(tmp_path: Path) -> None:
    """Media fetched from a URL can change between requests unseen, so no answer after it anchors."""
    model = ScriptedModel([])
    looked = Message(role="user", content="Look", images=[Image(url="https://example.org/a.png")])
    later = _first(
        Message(role="user", content="Hi"),
        Message(role="assistant", content="Hello"),
        looked,
        Message(role="assistant", content="Seen"),
        Message(role="user", content="More"),
    )

    prefixes = RequestDigests(model).prefixes(later)

    assert all(prefixes[: later.messages.index(looked) + 1])
    assert prefixes[later.messages.index(looked) + 1 :] == [None, None, None]
    assert not _sized(tmp_path, model, later).reported


@pytest.mark.parametrize("stored", ["id", "created_at", "metrics"])
def test_what_storing_an_earlier_answer_adds_keeps_a_later_answer_anchoring(tmp_path: Path, stored: str) -> None:
    """Storing a message gives it an id, a time, and timings no route sends, so the requests holding it keep their identity."""
    model = ScriptedModel([])
    first = _first(Message(role="user", content="Hi"))
    earlier = _answered(model, first, "Hello")
    second = replace(first, messages=(*first.messages, earlier, Message(role="user", content="More")))
    answer = _answered(model, second, "Sure", reported=_REPORTED + 50)
    later = replace(second, messages=(*second.messages, answer, Message(role="user", content="Thanks")))
    expected = _sized(tmp_path, model, later)

    if stored == "metrics":
        earlier.metrics = MessageMetrics(tokens=earlier.metrics.tokens, duration=2.5, time_to_first_token=0.3)
    else:
        setattr(earlier, stored, {"id": "stored-message-7", "created_at": 1_700_000_000}[stored])

    assert expected == RequestSize(_REPORTED + 50 + model.request_tokens(later, start=4), reported=True)
    assert _sized(tmp_path, model, later) == expected


def test_a_native_checkpoint_bounds_the_answers_that_anchor(tmp_path: Path) -> None:
    model = ScriptedModel([])
    native = NativeCompactionReplay(route="route-1", threshold=1_000)
    first = replace(_first(Message(role="user", content="Hi")), native_compaction=native)
    answer = _answered(model, first, "Hello")
    checkpoint = {"route": "route-1", "items": [{"type": "compaction", "content": "Earlier turns."}]}
    answer.provider_data = {**(answer.provider_data or {}), NATIVE_CHECKPOINT_KEY: checkpoint}
    later = replace(first, messages=(*first.messages, answer, Message(role="user", content="More")))

    assert not _sized(tmp_path, model, later).reported


@pytest.mark.asyncio
async def test_the_latest_answer_s_replayed_reasoning_counts_before_the_next_reply(tmp_path: Path) -> None:
    """On the Kimi route the next reply's first request sends the latest answer's ``reasoning_content`` back.

    Its size is the answer's reported request plus that answer as the route sends it, reasoning included.
    """
    config, runtime_paths = _make_config(tmp_path)
    kimi = _kimi_route(tmp_path)
    question = Message(role="user", content="What should I pack for Paris tomorrow?")
    answered = ModelRequest(messages=(question,))
    answer = Message(
        role="assistant",
        content="Pack a light rain jacket.",
        reasoning_content=_REASONING,
        metrics=MessageMetrics(tokens=TokenCounts(input_tokens=_REPORTED, output_tokens=40)),
        provider_data=model_provenance(kimi.identity.id, kimi.identity.provider),
    )
    stamp_request_identity(answer, kimi, answered)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    session = seed_session(storage, _session("session-1", runs=[_completed_run("run-1", messages=[question, answer])]))

    prepared = await prepare_history_for_run_for_test(
        agent_name="test_agent",
        full_prompt="And for the evening?",
        session_id="session-1",
        runtime_paths=runtime_paths,
        config=config,
        execution_identity=None,
        model=kimi,
        storage=storage,
        session=session,
    )

    reasoning = kimi.request_tokens(ModelRequest(messages=(Message(role="user", content=_REASONING),)))
    assert prepared.compaction_decision.request_tokens is not None
    assert prepared.compaction_decision.request_tokens >= _REPORTED + reasoning


def _kimi_route(tmp_path: Path) -> ModelProvider:
    """Return the Kimi Chat adapter one of its goldens configures, without network access."""
    golden = load_golden(sorted((_FIXTURES / "kimi").glob("*.json"))[0])
    root = tmp_path / "kimi"
    root.mkdir()
    return golden_provider(golden, root).adapter


_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agno_parity" / "providers"
_REASONING = "The forecast says rain in the evening, so a light jacket and closed shoes are the right call. " * 12
