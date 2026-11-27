"""Tests for in-turn tool-image hysteresis pruning and loop-hook integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mindroom import loop_hooks
from mindroom.agent_loop.model_loop import EMPTY_REQUEST, SendRequest
from mindroom.config.main import Config
from mindroom.history.message_content import _IN_TURN_TRANSIENT_IMAGE_TRIGGER_BYTES, prune_in_turn_tool_images
from mindroom.history.run_checkpoint import CheckpointHooks
from mindroom.history.types import HistoryScope
from mindroom.llm.media import VIEWED_IMAGE_ID_PREFIX, Audio, Image
from mindroom.llm.messages import MEDIA_FOLLOW_UP_TEXT, PRUNED_MEDIA_FOLLOW_UP_TEXT, Message, is_media_follow_up
from mindroom.llm.provider import DeferredTools
from mindroom.loop_hooks import HookTurn
from mindroom.tool_system.tool_function import ToolValue
from tests.agent_loop.helpers import run_loop, spec, toolkit
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.history_helpers import replay_settings

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_loop.model_loop import BeforeRequestHook, LoopState


def _transient(index: int, size: int = 1024) -> Image:
    return Image(id=f"shot-{index}", content=b"x" * size, mime_type="image/png")


def _viewed(index: int, size: int = 1024) -> Image:
    return Image(id=f"{VIEWED_IMAGE_ID_PREFIX}{index}", content=b"v" * size, mime_type="image/png")


def _step(messages: list[Message], step: int, images: list[Image], *, audio: list[Audio] | None = None) -> Message:
    call_id = f"call-{step}"
    call = {"id": call_id, "type": "function", "function": {"name": "snap", "arguments": "{}"}}
    messages.append(Message(role="assistant", tool_calls=[call]))
    messages.append(Message(role="tool", content="ok", tool_call_id=call_id, tool_name="snap"))
    follow_up = Message(
        role="user",
        content=MEDIA_FOLLOW_UP_TEXT,
        images=images,
        audio=audio,
        created_at=1_000 + step,
    )
    messages.append(follow_up)
    return follow_up


def _image_ids(messages: list[Message]) -> list[str]:
    return [image.id for message in messages for image in message.images or ()]


def test_is_media_follow_up_recognizes_live_and_pruned_prefixes() -> None:
    """Both live and pruned follow-up prefixes (and paragraph suffixes) match; non-follow-ups do not."""
    for text in (
        MEDIA_FOLLOW_UP_TEXT,
        PRUNED_MEDIA_FOLLOW_UP_TEXT,
        f"{MEDIA_FOLLOW_UP_TEXT}\n\n[note]",
        f"{PRUNED_MEDIA_FOLLOW_UP_TEXT}\n\n[note]",
    ):
        assert is_media_follow_up(Message(role="user", content=text))
    assert not is_media_follow_up(Message(role="user", content=f"{MEDIA_FOLLOW_UP_TEXT} extra"))
    assert not is_media_follow_up(Message(role="user", content=f"{PRUNED_MEDIA_FOLLOW_UP_TEXT}\nextra"))
    assert not is_media_follow_up(Message(role="assistant", content=MEDIA_FOLLOW_UP_TEXT))
    assert not is_media_follow_up(Message(role="user", content=[{"type": "text", "text": MEDIA_FOLLOW_UP_TEXT}]))


def test_transient_hysteresis_prunes_at_11_down_to_2_and_preserves_invariants() -> None:
    """Steps 1..10 do nothing, step 11 prunes to 2 preserving metadata/history/unseen, steps 12..19 do nothing."""
    history_msg = Message(
        role="user",
        content=MEDIA_FOLLOW_UP_TEXT,
        images=[Image(id="history-shot", content=b"h" * 512, mime_type="image/png")],
        from_history=True,
    )
    human_msg = Message(
        role="user",
        content="Look at this",
        images=[Image(id="human-photo", content=b"u" * 512, mime_type="image/png")],
    )
    messages: list[Message] = [history_msg, human_msg]
    slots: list[tuple[int, Message]] = []
    for step in range(1, 11):
        slots.append((len(messages) + 2, _step(messages, step, [_transient(step)])))
        assert prune_in_turn_tool_images(messages) is False

    slots.append((len(messages) + 2, _step(messages, 11, [_transient(11)])))
    assert prune_in_turn_tool_images(messages) is True
    assert _image_ids(messages) == ["history-shot", "human-photo", "shot-10", "shot-11"]
    assert messages[0] is history_msg
    assert messages[1] is human_msg
    for slot_index, original in slots[:9]:
        pruned = messages[slot_index]
        assert pruned is not original
        assert original.images is not None
        assert (pruned.id, pruned.created_at, pruned.images) == (original.id, original.created_at, None)
        assert pruned.content == PRUNED_MEDIA_FOLLOW_UP_TEXT
        assert is_media_follow_up(pruned)

    for step in range(12, 20):
        _step(messages, step, [_transient(step)])
        assert prune_in_turn_tool_images(messages) is False
    _step(messages, 20, [_transient(20)])
    assert prune_in_turn_tool_images(messages) is True
    assert _image_ids(messages) == ["history-shot", "human-photo", "shot-19", "shot-20"]


def test_transient_byte_trigger_and_unseen_oversized_batch_retention() -> None:
    """Byte threshold prunes seen images down to <= 1 MiB, retains unseen batches, and never stats filepaths."""
    unseen = [
        Message(role="user", content=MEDIA_FOLLOW_UP_TEXT, images=[_transient(i, size=600_000) for i in range(12)]),
    ]
    assert prune_in_turn_tool_images(unseen) is False
    assert len(unseen[0].images or ()) == 12

    chunk = _IN_TURN_TRANSIENT_IMAGE_TRIGGER_BYTES // 3
    messages: list[Message] = [Message(role="user", content="start")]
    _step(messages, 1, [Image(id="shot-file", filepath="/nonexistent/must-not-stat.png")])
    _step(messages, 2, [_transient(2, size=chunk)])
    _step(messages, 3, [_transient(3, size=chunk)])
    _step(messages, 4, [_transient(4, size=chunk + 64)])
    _step(messages, 5, [_transient(5, size=500_000)])
    assert prune_in_turn_tool_images(messages) is True
    assert _image_ids(messages) == ["shot-5"]


def test_viewed_images_exempt_from_transient_cap_and_bounded_by_backstop() -> None:
    """Viewed images stay untouched by transient pruning and use their 12/15 MiB -> 4/10 MiB backstop."""
    messages: list[Message] = [Message(role="user", content="inspect")]
    for step in range(1, 6):
        _step(messages, step, [_viewed(step)])
    for step in range(6, 17):
        _step(messages, step, [_transient(step)])
    assert prune_in_turn_tool_images(messages) is True
    assert _image_ids(messages) == [*(f"{VIEWED_IMAGE_ID_PREFIX}{i}" for i in range(1, 6)), "shot-15", "shot-16"]

    for step in range(6, 14):
        _step(messages, 100 + step, [_viewed(step)])
    assert prune_in_turn_tool_images(messages) is True
    assert [i for i in _image_ids(messages) if i.startswith(VIEWED_IMAGE_ID_PREFIX)] == [
        f"{VIEWED_IMAGE_ID_PREFIX}{i}" for i in range(10, 14)
    ]
    assert messages[3].images is None
    assert isinstance(messages[3].content, str)
    assert messages[3].content.startswith(f"{PRUNED_MEDIA_FOLLOW_UP_TEXT}\n\n")
    assert "view_file" in messages[3].content
    assert is_media_follow_up(messages[3])

    big_viewed: list[Message] = [Message(role="user", content="big")]
    for step in range(1, 5):
        _step(big_viewed, step, [_viewed(step, size=4 * 1024 * 1024)])
    assert prune_in_turn_tool_images(big_viewed) is True
    assert _image_ids(big_viewed) == [f"{VIEWED_IMAGE_ID_PREFIX}3", f"{VIEWED_IMAGE_ID_PREFIX}4"]


def test_mixed_media_slot_preserves_other_media_and_transitions_cleanly() -> None:
    """Dropping transient images keeps audio and viewed images intact, then transitions cleanly if viewed drops later."""
    messages: list[Message] = [Message(role="user", content="work")]
    audio_clip = Audio(content=b"audio", mime_type="audio/ogg")
    _step(messages, 1, [_transient(1), _viewed(1)], audio=[audio_clip])
    _step(messages, 2, [_transient(2), _viewed(2)])
    for step in range(3, 12):
        _step(messages, step, [_transient(step)])

    assert prune_in_turn_tool_images(messages) is True
    assert [img.id for img in messages[3].images or ()] == [f"{VIEWED_IMAGE_ID_PREFIX}1"]
    assert messages[3].audio == [audio_clip]
    assert isinstance(messages[3].content, str)
    assert messages[3].content.startswith(f"{MEDIA_FOLLOW_UP_TEXT}\n\n")
    assert is_media_follow_up(messages[3])

    for step in range(3, 14):
        _step(messages, 100 + step, [_viewed(step)])
    assert prune_in_turn_tool_images(messages) is True
    assert (messages[3].images, messages[3].audio) == (None, [audio_clip])
    assert isinstance(messages[3].content, str)
    assert messages[3].content.startswith(f"{MEDIA_FOLLOW_UP_TEXT}\n\n")
    assert "view_file" in messages[3].content
    assert messages[6].images is None
    assert isinstance(messages[6].content, str)
    assert messages[6].content.startswith(f"{PRUNED_MEDIA_FOLLOW_UP_TEXT}\n\n")
    assert "view_file" in messages[6].content
    assert is_media_follow_up(messages[6])


@pytest.mark.asyncio
async def test_loop_hooks_prunes_before_compaction_and_checkpoint_without_mutating_prior_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pruning runs before mid-turn compaction and checkpointing, leaving prior ModelRequests untouched."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(Config.model_validate({"agents": {"code": {"display_name": "Code"}}}), runtime_paths)
    counter = 0

    def snap() -> ToolValue:
        """Capture a transient screenshot."""
        nonlocal counter
        counter += 1
        return ToolValue(content=f"shot {counter}", images=[_transient(counter)])

    model = ScriptedModel([*(calls(tool_call("snap", f"c-{i}")) for i in range(1, 12)), answer("done")])
    compaction_counts: list[int] = []
    checkpoint_counts: list[int] = []

    def fake_compaction(**_kwargs: Any) -> BeforeRequestHook:  # noqa: ANN401
        async def _hook(state: LoopState) -> SendRequest:
            compaction_counts.append(sum(len(m.images or ()) for m in state.messages))
            return SendRequest()

        return _hook

    def fake_checkpoint(*_args: Any, **_kwargs: Any) -> CheckpointHooks:  # noqa: ANN401
        async def before_request(state: LoopState) -> SendRequest:
            checkpoint_counts.append(sum(len(m.images or ()) for m in state.messages))
            return SendRequest()

        async def before_dispatch(_state: LoopState, _planned: Any) -> None:  # noqa: ANN401
            return None

        return CheckpointHooks(before_request, before_dispatch)

    monkeypatch.setattr(loop_hooks, "mid_turn_compaction_hook", fake_compaction)
    monkeypatch.setattr(loop_hooks, "checkpoint_hooks", fake_checkpoint)
    turn = HookTurn(
        config=config,
        runtime_paths=runtime_paths,
        model_name="default",
        storage=None,
        replay_plan=None,
        prompt_id=None,
    )
    hooks = loop_hooks._loop_hooks(
        turn,
        model=model,
        scope=HistoryScope(kind="agent", scope_id="code"),
        history_settings=replay_settings(),
        deferred_tools=DeferredTools(),
        tool_middleware=(),
        participation=None,
        job_definitions=None,
        job_notices_for=None,
        queued_notices=False,
        steering=False,
        skill_review_capture=None,
    )

    messages = [Message(role="system", content="You are Code."), Message(role="user", content="capture 11")]
    await run_loop(spec(model, toolkit(snap)), messages, hooks=hooks, request_template=EMPTY_REQUEST)

    expected = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 2]
    assert compaction_counts == expected
    assert checkpoint_counts == expected
    assert [sum(len(m.images or ()) for m in req.messages) for req in model.requests] == expected
    assert [img.id for m in model.requests[10].messages for img in m.images or ()] == [
        f"shot-{i}" for i in range(1, 11)
    ]
    assert [img.id for m in model.requests[11].messages for img in m.images or ()] == ["shot-10", "shot-11"]
