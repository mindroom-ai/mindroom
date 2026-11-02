"""A Responses request is sized as its explicit replay sends it, pricing images by their visual patches.

The route's ``request_tokens`` owns the rule; the history estimates and the reply's preparation size through it.
"""

from __future__ import annotations

import base64
import json
from base64 import b64encode
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from random import Random
from typing import TYPE_CHECKING, Any

import pytest
from PIL import Image as PillowImage
from structlog.testing import capture_logs

from mindroom.config.models import CompactionConfig, ModelConfig
from mindroom.config.resolution import entity_model_name
from mindroom.history.policy import classify_compaction_decision
from mindroom.history.replay import (
    estimate_prompt_visible_history_tokens,
    estimate_summary_tokens,
    prompt_visible_history,
)
from mindroom.history.request_messages import TurnMessages
from mindroom.history.request_size import RequestFrame, request_sizer
from mindroom.history.runtime import (
    finalize_history_preparation,
    prepare_scope_history,
    resolve_entity_preparation_inputs,
)
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryScope, ScopeSummary
from mindroom.llm.media import Image
from mindroom.llm.messages import MODEL_PROVENANCE_KEY, Message, stable_serialize
from mindroom.llm.provider import ModelRequest, NativeCompactionReplay
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.providers.openai.replay_estimate import responses_replay_uses_visual_tokens
from mindroom.providers.openai.responses_wire import conversation_digest, responses_input, responses_replay
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.token_counting.counts import count, opaque_tokens
from mindroom.token_counting.requests import MEDIA_ALLOWANCE_TOKENS
from tests.conftest import seed_session
from tests.fakes.scripted_model import ScriptedModel
from tests.history_helpers import _completed_run, _make_config, _session, record_native_checkpoint, replay_settings
from tests.providers.golden_support import pipeline_provider
from tests.providers.openai_family_support import responses_adapter

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.providers.openai.responses import ResponsesAdapter

_SCOPE = HistoryScope(kind="agent", scope_id="test_agent")
_ALL_HISTORY = replay_settings()


def _replayed_input(adapter: ResponsesAdapter, messages: list[Message]) -> list[Any]:
    """Return the ``input`` an explicitly replayed request on ``adapter`` sends for ``messages``."""
    return responses_input(
        responses_replay(messages, store=False, portable_replay=True, native_compaction=None),
        cache_system_prompt=adapter.settings.cache_system_prompt,
        explicit_breakpoint=adapter.wire.explicit_breakpoint,
        provider=adapter.identity.provider,
    )


def _sized(adapter: ResponsesAdapter, *messages: Message, start: int = 0) -> int:
    """Return what the request holding ``messages`` gives ``adapter``'s model, from message ``start`` on."""
    return adapter.request_tokens(ModelRequest(messages=messages), start=start)


def _png(size: tuple[int, int] = (256, 256), *, compress_level: int = 6) -> bytes:
    buffer = BytesIO()
    PillowImage.new("RGB", size, "blue").save(buffer, format="PNG", compress_level=compress_level)
    return buffer.getvalue()


@pytest.mark.parametrize("representation", ["bytes", "path", "data_url", "content_block"])
def test_image_history_after_summary_does_not_repeat_compaction_for_encoded_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    representation: str,
) -> None:
    """One uncompressed PNG must fit as visual input after a saved summary."""
    image_file = tmp_path / "image.png"
    PillowImage.new("RGB", (256, 256), "blue").save(image_file, compress_level=0)
    image_bytes = image_file.read_bytes()
    data_url = "data:image/png;base64," + b64encode(image_bytes).decode()
    if representation == "content_block":
        message = Message(
            role="user",
            content=[
                {"type": "input_text", "text": "Describe this image."},
                {"type": "input_image", "image_url": data_url},
            ],
        )
    else:
        image = {
            "bytes": Image(content=image_bytes, format="png"),
            "path": Image(filepath=str(image_file)),
            "data_url": Image(url=data_url),
        }[representation]
        message = Message(role="user", content="Describe this image.", images=[image])
    session = _session(
        "session",
        runs=[_completed_run("recent", messages=[message])],
        summary=ScopeSummary(text="Earlier work is complete.", updated_at=datetime.now(UTC)),
    )
    config, runtime_paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(reserve_tokens=1000, replay_window_tokens=10_000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=20_000)},
    )
    adapter = responses_adapter(tmp_path, store=True)
    resolved = resolve_entity_preparation_inputs(
        config=config,
        runtime_paths=runtime_paths,
        history_settings=config.resolve_scope("test_agent").history_settings,
        active_model_name=entity_model_name(config, "test_agent"),
        active_context_window=None,
    )
    before = deepcopy(session)
    request_before = _replayed_input(adapter, [message])
    serialized_sizes = []
    serialize = json.dumps

    def record_serialization(value: object, **kwargs: Any) -> str:  # noqa: ANN401
        result = serialize(value, **kwargs)
        serialized_sizes.append(len(result))
        return result

    monkeypatch.setattr(json, "dumps", record_serialization)
    for _ in range(2):
        # A request holding only the summary and the history.
        tokens = estimate_prompt_visible_history_tokens(
            session=session,
            history_settings=resolved.history_settings,
            model=adapter,
        )
        summary_tokens = estimate_summary_tokens(session, adapter)
        decision = classify_compaction_decision(
            plan=resolved.execution_plan,
            force_compact_before_next_run=False,
            request_tokens=tokens,
            summary_request_tokens=summary_tokens,
            allow_native_compaction=True,
        )
        assert decision.mode == "none", (tokens, decision.reason)
        # The summary plus the image's visual cost, never its encoded bytes.
        assert summary_tokens < tokens < 2000
    monkeypatch.undo()
    assert serialized_sizes
    # Estimation must not serialize image transport even for a discarded fallback.
    assert max(serialized_sizes) < 10_000
    assert session == before
    assert _replayed_input(adapter, [message]) == request_before


@pytest.mark.parametrize("representation", ["content_block", "attachment"])
def test_a_model_without_a_patch_budget_prices_an_image_as_its_replay_sends_it(
    tmp_path: Path,
    representation: str,
) -> None:
    """A model with no patch budget counts an image block as the text it sends and an attachment at its allowance.

    The attachment's encoded bytes never count.
    """
    buffer = BytesIO()
    # Seeded noise keeps the encoded attachment far larger than its allowance.
    PillowImage.frombytes("RGB", (256, 256), Random(0).randbytes(256 * 256 * 3)).save(  # noqa: S311
        buffer,
        format="PNG",
        compress_level=0,
    )
    adapter = responses_adapter(tmp_path, "unrecognized-image-model")
    assert not responses_replay_uses_visual_tokens(adapter.identity.id)
    assert responses_replay_uses_visual_tokens(responses_adapter(tmp_path).identity.id)
    if representation == "content_block":
        block = {"type": "input_image", "image_url": "data:image/png;base64," + b64encode(buffer.getvalue()).decode()}
        message = Message(role="user", content=[block])
        expected = count(adapter.token_counter, stable_serialize(_replayed_input(adapter, [message])))
    else:
        message = Message(role="user", content="Describe.", images=[Image(content=buffer.getvalue(), format="png")])
        assert count(adapter.token_counter, stable_serialize(_replayed_input(adapter, [message]))) > 100_000
        expected = _sized(adapter, Message(role="user", content="Describe.")) + MEDIA_ALLOWANCE_TOKENS
    before = deepcopy(message)

    assert _sized(adapter, message) == expected
    assert message == before


def test_responses_image_budget_preserves_visual_cost_and_ignores_png_encoding_size(tmp_path: Path) -> None:
    """The same pixels cost the same budget across lossless encodings."""
    adapter = responses_adapter(tmp_path)
    estimates = [
        _sized(
            adapter,
            Message(
                role="user",
                content="Describe.",
                images=[Image(content=_png(compress_level=compression), format="png")],
            ),
        )
        for compression in (0, 9)
    ]
    assert estimates[0] == estimates[1]
    assert 77 <= estimates[0] < 500
    assert estimates[0] > _sized(adapter, Message(role="user", content="Describe."))


def test_responses_image_budget_reads_only_bounded_header_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Estimating a large image must not allocate another full decoded image."""
    payload = "data:image/png;base64," + b64encode(_png(compress_level=0)).decode()
    decoded_sizes = []
    decode = base64.b64decode

    def record_decode(value: str, *, validate: bool = False) -> bytes:
        result = decode(value, validate=validate)
        decoded_sizes.append(len(result))
        return result

    adapter = responses_adapter(tmp_path)
    monkeypatch.setattr(base64, "b64decode", record_decode)
    tokens = _sized(adapter, Message(role="user", content=[{"type": "input_image", "image_url": payload}]))
    assert 77 <= tokens < 200
    assert decoded_sizes
    assert max(decoded_sizes) <= 64 * 1024


def test_responses_jpeg_header_beyond_scan_budget_uses_conservative_allowance(tmp_path: Path) -> None:
    """Valid JPEG metadata can push dimensions beyond the bounded header scan."""
    buffer = BytesIO()
    PillowImage.new("RGB", (1024, 1024), "blue").save(buffer, format="JPEG")
    image = buffer.getvalue()
    metadata = b"metadata" * 8000
    segment = b"\xff\xef" + (len(metadata) + 2).to_bytes(2, "big") + metadata
    image = image[:2] + segment + image[2:]
    with PillowImage.open(BytesIO(image)) as decoded:
        decoded.load()
        assert decoded.size == (1024, 1024)
    tokens = _sized(
        responses_adapter(tmp_path),
        Message(role="user", content="Describe.", images=[Image(content=image, format="jpeg")]),
    )
    assert 36_000 <= tokens < 36_100


def test_responses_unrecognized_webp_header_uses_conservative_allowance(tmp_path: Path) -> None:
    """A WebP chunk with no known dimensions is priced at the ceiling, never at made-up dimensions."""
    header = b"RIFF\x10\x00\x00\x00WEBPJUNK\x04\x00\x00\x00test"
    tokens = _sized(
        responses_adapter(tmp_path),
        Message(
            role="user",
            content=[{"type": "input_image", "image_url": "data:image/webp;base64," + b64encode(header).decode()}],
        ),
    )
    assert 36_000 <= tokens < 36_100


def test_native_checkpoint_tail_images_keep_visual_cost_without_transport_bytes(tmp_path: Path) -> None:
    """A checkpoint keeps its opaque budget while new image input uses visual tokens."""
    adapter = responses_adapter(tmp_path, store=False)
    native = adapter.native_compaction_settings(threshold=1024, history_generation="", allow_authored=False)
    assert native is not None
    checkpoint = Message(role="assistant", content="Ready")
    items = [{"type": "compaction", "id": "cmp", "encrypted_content": "opaque-checkpoint" * 100}]
    record_native_checkpoint(checkpoint, items, native)
    estimates = []
    for compression in (0, 9):
        tail = Message(
            role="user",
            content=[
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64," + b64encode(_png(compress_level=compression)).decode(),
                },
            ],
        )
        session = _session("session", runs=[_completed_run("recent", messages=[checkpoint, tail])])
        estimates.append(
            estimate_prompt_visible_history_tokens(
                session=session,
                history_settings=_ALL_HISTORY,
                model=adapter,
                native=native,
            ),
        )
    assert estimates[0] == estimates[1]
    # The checkpoint's ciphertext counts a quarter token per character.
    assert opaque_tokens(len(items[0]["encrypted_content"])) < estimates[0] < 2000


@pytest.mark.parametrize(
    ("image_format", "color", "options"),
    [
        ("PNG", "blue", {}),
        ("JPEG", "blue", {}),
        ("GIF", "blue", {}),
        ("WEBP", "blue", {}),
        ("WEBP", (0, 0, 255, 128), {}),
        ("WEBP", "blue", {"lossless": True}),
    ],
    ids=["png", "jpeg", "gif", "webp-lossy", "webp-extended", "webp-lossless"],
)
@pytest.mark.parametrize("detail", ["low", "high", "original", "auto"])
def test_responses_image_header_dimensions_keep_nonzero_visual_budget(
    tmp_path: Path,
    image_format: str,
    color: str | tuple[int, ...],
    options: dict[str, Any],
    detail: str,
) -> None:
    """Supported image headers all describe the same visual patch area, WebP in each of its three layouts."""
    buffer = BytesIO()
    mode = "RGBA" if isinstance(color, tuple) else "RGB"
    PillowImage.new(mode, (1024, 1024), color).save(buffer, format=image_format, **options)
    tokens = _sized(
        responses_adapter(tmp_path),
        Message(
            role="user",
            content="Describe.",
            images=[Image(content=buffer.getvalue(), format=image_format.lower(), detail=detail)],
        ),
    )
    visual_tokens = 308 if detail == "low" else 1229
    assert visual_tokens <= tokens < visual_tokens + 100


def _bmp() -> bytes:
    buffer = BytesIO()
    PillowImage.new("RGB", (64, 64), "blue").save(buffer, format="BMP")
    return buffer.getvalue()


@pytest.mark.parametrize(
    "source",
    [
        "https://example.test/image.png",
        "data:image/png;base64,invalid",
        "data:image/png;base64,aGVsbG8=",
        "data:image/bmp;base64," + b64encode(_bmp()).decode(),
    ],
    ids=["remote", "invalid-base64", "too-short", "unsupported-format"],
)
@pytest.mark.parametrize(
    ("detail", "visual_tokens"),
    [("low", 308), ("high", 3000), ("original", 36_000), ("auto", 36_000)],
)
def test_responses_unknown_image_dimensions_use_documented_allowance(
    tmp_path: Path,
    source: str,
    detail: str,
    visual_tokens: int,
) -> None:
    """Remote or unreadable images retain cost without fetching or parsing arbitrary text."""
    tokens = _sized(
        responses_adapter(tmp_path),
        Message(role="user", content=[{"type": "input_image", "image_url": source, "detail": detail}]),
    )
    assert visual_tokens <= tokens < visual_tokens + 100


@pytest.mark.parametrize(
    ("model_id", "size", "detail", "visual_tokens"),
    [
        ("gpt-6-astra", (2048, 2048), "high", 3000),
        ("gpt-6-astra", (2048, 2048), "original", 4916),
        ("gpt-6-astra", (4096, 512), "high", 2458),
        ("gpt-5.6-sol", (2048, 2048), "auto", 4916),
        ("gpt-5.5", (2048, 2048), "auto", 4916),
        ("gpt-5.4", (2048, 2048), "auto", 3000),
    ],
)
def test_responses_image_budget_obeys_documented_patch_bounds(
    tmp_path: Path,
    model_id: str,
    size: tuple[int, int],
    detail: str,
    visual_tokens: int,
) -> None:
    """Documented patch examples and default detail policies constrain image sizing."""
    tokens = _sized(
        responses_adapter(tmp_path, model_id),
        Message(role="user", content="Describe.", images=[Image(content=_png(size), format="png", detail=detail)]),
    )
    assert visual_tokens <= tokens < visual_tokens + 100


def test_responses_image_file_id_retains_unknown_dimension_allowance(tmp_path: Path) -> None:
    """An uploaded image has visual cost even without locally available bytes."""
    tokens = _sized(
        responses_adapter(tmp_path),
        Message(role="user", content=[{"type": "input_image", "file_id": "file_image", "detail": "high"}]),
    )
    assert 3000 <= tokens < 3100


def test_responses_history_with_an_unreadable_attachment_is_still_sized(tmp_path: Path) -> None:
    """An attachment file that is gone still leaves the request a size, with a cost for the image it held.

    Sending the request is where the media fallback replaces the attachment with its note.
    """
    adapter = responses_adapter(tmp_path)
    gone = Image(filepath=str(tmp_path / "gone.png"), mime_type="image/png")
    question = Message(role="user", content="Describe.", images=[gone], from_history=True)
    turn = Message(role="user", content="And now?")

    assert _sized(adapter, question, turn) > _sized(adapter, replace(question, images=None), turn)


def _reasoned_answer(ciphertext: str, *, reasoning_tokens: int = 0, own_turn: bool = False) -> list[Message]:
    """Return a question and an answer whose stored output carries one encrypted reasoning item."""
    output = [
        {"type": "reasoning", "id": "rs_plan", "summary": [], "encrypted_content": ciphertext},
        {
            "type": "message",
            "id": "msg_answer",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "Ready", "annotations": []}],
        },
    ]
    provider_data: dict[str, Any] = {"mindroom_response_output": output}
    if own_turn:
        provider_data[MODEL_PROVENANCE_KEY] = {"id": "gpt-6-astra", "provider": "openai"}
    answer = Message(
        role="assistant",
        content="Ready",
        provider_data=provider_data,
        metrics=MessageMetrics(tokens=TokenCounts(reasoning_tokens=reasoning_tokens)),
        from_history=not own_turn,
    )
    return [Message(role="user", content="Plan the trip.", from_history=not own_turn), answer]


def test_responses_budget_ignores_encrypted_reasoning_length(tmp_path: Path) -> None:
    """Encrypted reasoning is opaque base64; its length says nothing about the tokens the provider counts."""
    adapter = responses_adapter(tmp_path, store=False)
    short, long = _reasoned_answer("gAAAA"), _reasoned_answer("gAAAA" + "Zm9vYmFy" * 5000)
    assert any(item.get("type") == "reasoning" for item in _replayed_input(adapter, long))
    assert _sized(adapter, *long) == _sized(adapter, *short)


@pytest.mark.parametrize("model_id", ["gpt-6-astra", "gpt-5.2-codex"])
@pytest.mark.parametrize("own_turn", [False, True])
@pytest.mark.parametrize("start", [0, 1])
def test_responses_budget_charges_replayed_reasoning_its_reported_tokens(
    tmp_path: Path,
    model_id: str,
    start: int,
    *,
    own_turn: bool,
) -> None:
    """Encrypted reasoning a request replays counts the tokens its answer reported, the run's own answer or a copy.

    From ``start`` 1 the request is sized from the answer on, as a request anchored on the answer before it is.
    """
    adapter = responses_adapter(tmp_path, model_id, store=False)
    reasoned = _reasoned_answer("gAAAA", reasoning_tokens=20_000, own_turn=own_turn)
    unreasoned = _reasoned_answer("gAAAA", own_turn=own_turn)
    assert any(item.get("type") == "reasoning" for item in _replayed_input(adapter, reasoned))
    assert _sized(adapter, *reasoned, start=start) - _sized(adapter, *unreasoned, start=start) == 20_000


@pytest.mark.parametrize(("own_turn", "charged"), [(True, 700), (False, 0)])
def test_a_chained_request_charges_the_reasoning_of_the_runs_own_answers(
    tmp_path: Path,
    *,
    own_turn: bool,
    charged: int,
) -> None:
    """A request that continues a stored response sends no reasoning, but the provider keeps the run's own reasoning.

    An earlier turn's answer, a copy from history, keeps none.
    """
    adapter = responses_adapter(tmp_path, store=True)
    question = Message(role="user", content="Plan the trip.", from_history=not own_turn)
    answer = Message(
        role="assistant",
        content="Ready",
        provider_data={"response_id": "resp_plan", "mindroom_conversation_sha256": conversation_digest([question])},
        metrics=MessageMetrics(tokens=TokenCounts(reasoning_tokens=700)),
        from_history=not own_turn,
    )
    follow_up = Message(role="user", content="Book it.")
    request = ModelRequest(messages=(question, answer, follow_up))
    assert responses_replay(
        request.messages,
        store=True,
        portable_replay=False,
        native_compaction=None,
    ).previous_response_id

    unreasoned = replace(answer, metrics=MessageMetrics())
    assert adapter.request_tokens(request) - _sized(adapter, question, unreasoned, follow_up) == charged


def test_responses_budget_preserves_literal_data_urls_files_and_tool_arguments(tmp_path: Path) -> None:
    """Only typed Responses image input is discounted, never similar-looking text."""
    literal = "data:image/png;base64," + "aGVsbG8=" * 1000
    messages = [
        Message(
            role="user",
            content=[
                {"type": "input_text", "text": literal},
                {"type": "input_file", "filename": "example.txt", "file_data": literal},
            ],
        ),
        Message(
            role="assistant",
            tool_calls=[
                {
                    "id": "call_example",
                    "type": "function",
                    "function": {
                        "name": "example",
                        "arguments": stable_serialize({"type": "input_image", "image_url": literal}),
                    },
                },
            ],
        ),
        Message(role="tool", tool_call_id="call_example", content=literal),
    ]
    adapter = responses_adapter(tmp_path, store=False)
    assert _sized(adapter, *messages) == count(
        adapter.token_counter,
        stable_serialize(_replayed_input(adapter, messages)),
    )


def test_native_checkpoint_estimate_does_not_count_the_summary_it_replaced(tmp_path: Path) -> None:
    """A checkpoint already covers the summary message before it, which the route's projection drops."""
    route = "route-1"
    checkpoint = Message(
        role="assistant",
        content="",
        provider_data={
            "mindroom_native_compaction": {
                "route": route,
                "threshold": 100,
                "checkpoint_prefix": True,
                "items": [{"type": "compaction", "content": "provider checkpoint"}],
            },
        },
    )
    runs = [_completed_run("run-1", messages=[Message(role="user", content="question"), checkpoint])]
    adapter = responses_adapter(tmp_path, store=False)
    native = NativeCompactionReplay(route=route, threshold=100)

    without_summary = estimate_prompt_visible_history_tokens(
        session=_session("session", runs=runs),
        history_settings=_ALL_HISTORY,
        model=adapter,
        native=native,
    )
    with_summary = estimate_prompt_visible_history_tokens(
        session=_session("session", runs=runs, summary=ScopeSummary(text="S" * 400, updated_at=datetime.now(UTC))),
        history_settings=_ALL_HISTORY,
        model=adapter,
        native=native,
    )

    assert with_summary == without_summary


@pytest.mark.parametrize(
    ("items", "logged"),
    [
        ([{"type": "compaction", "content": "provider checkpoint"}, {"type": "text", "text": "Done"}], True),
        ([{"type": "compaction", "content": None}, {"type": "text", "text": "Done"}], False),
        ([{"type": "text", "text": "Done"}], False),
    ],
)
def test_provider_compaction_logs_once_per_checkpoint(items: list[dict[str, Any]], *, logged: bool) -> None:
    """Only a response that carries a real checkpoint reports that the provider compacted."""
    native = NativeCompactionReplay(route="route-1", threshold=60_000)

    with capture_logs() as logs:
        record_native_checkpoint(Message(role="assistant", content="Done"), items, native)

    events = [log for log in logs if log["event"] == "Native compaction checkpoint"]
    expected = {
        "event": "Native compaction checkpoint",
        "log_level": "info",
        "threshold_tokens": 60_000,
        "checkpoint_chars": len(json.dumps(items[:1], ensure_ascii=False)),
    }
    assert events == ([expected] if logged else [])


@pytest.mark.asyncio
async def test_reply_preparation_sizes_and_fits_history_with_the_route_estimate(tmp_path: Path) -> None:
    """A reply on a Responses route sizes its first request and fits its replay with the route's own count."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    spaced_answer = Message(role="assistant", content="x " * 2000)
    session = seed_session(
        storage,
        _session(
            "session-1",
            runs=[_completed_run("run-1", messages=[Message(role="user", content="Hi"), spaced_answer])],
        ),
    )
    model = pipeline_provider(responses_adapter(tmp_path))
    model_name = entity_model_name(config, "test_agent")
    inputs = resolve_entity_preparation_inputs(
        config=config,
        runtime_paths=runtime_paths,
        history_settings=config.resolve_scope("test_agent").history_settings,
        active_model_name=model_name,
        active_context_window=None,
    )
    frame = RequestFrame(
        ModelRequest(messages=()),
        (Message(role="system", content="You answer briefly."),),
        TurnMessages(prompt=Message(role="user", content="And now?")),
    )
    history = prompt_visible_history(session, inputs.history_settings)
    route_size = model.request_tokens(frame.request(summary=None, history=history))
    canonical_size = ScriptedModel([]).request_tokens(frame.request(summary=None, history=history))
    without_history = model.request_tokens(frame.request(summary=None, history=()))

    prepared = await prepare_scope_history(
        sizer=request_sizer(model, config, model_name),
        frame=frame,
        resolved_inputs=inputs,
        runtime_paths=runtime_paths,
        config=config,
        scope_context=ScopeSessionContext(
            scope=_SCOPE,
            storage=storage,
            session=session,
            session_id="session-1",
            storage_factory=lambda: storage,
        ),
        allow_native_compaction=False,
        compaction_lifecycle=None,
        pipeline_timing=None,
    )
    final = finalize_history_preparation(prepared_scope_history=prepared, frame=frame, pipeline_timing=None)

    assert route_size > canonical_size
    assert prepared.compaction_decision.request_tokens == route_size
    assert final.replay_plan is not None
    assert final.replay_plan.estimated_tokens == route_size - without_history
    assert final.prepared_context_tokens == route_size
