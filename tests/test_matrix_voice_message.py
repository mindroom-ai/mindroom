"""Tests for the Matrix voice message tool."""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from pydantic import ValidationError

from mindroom.config.main import Config
from mindroom.custom_tools.matrix_voice_message import MatrixVoiceMessageTools, _normalize_openai_base_url
from mindroom.matrix.state import MatrixState
from mindroom.matrix.voice_message import PreparedVoiceAudio
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.voice.openai_realtime_speech import RealtimeSpeechError, pcm_to_wav
from tests.agent_tool_helpers import call_entrypoint, toolkit_offers
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import (
    bind_runtime_paths,
    delivered_matrix_side_effect,
    make_conversation_reader_mock,
    make_latest_thread_event_id_mock,
    make_relation_lookup,
    make_thread_reply_lookup,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.matrix_state_helpers import save_matrix_state

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.tool_system.tool_function import ToolFunction


@pytest.fixture(autouse=True)
def _reset_matrix_voice_message_rate_limit() -> None:
    MatrixVoiceMessageTools._recent_actions.clear()


@pytest.fixture(autouse=True)
def _media_tools_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the ffmpeg preflight pass whether or not the test host has ffmpeg."""
    monkeypatch.setattr("mindroom.matrix.voice_message.shutil.which", lambda name: f"/usr/bin/{name}")


def _payload(raw: str) -> dict[str, object]:
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    return parsed


def _matrix_voice_message_function() -> ToolFunction:
    return toolkit_offers(MatrixVoiceMessageTools(api_key="sk-test"), "matrix_voice_message")


def _mock_client() -> AsyncMock:
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = "@mindroom_general:localhost"
    room = MagicMock()
    room.encrypted = False
    client.rooms = {"!room:localhost": room}
    return client


def _prepared_voice_audio() -> PreparedVoiceAudio:
    return PreparedVoiceAudio(
        audio_bytes=b"prepared-opus-bytes",
        duration_ms=1250,
        waveform=[512] * 30,
        mimetype="audio/ogg",
    )


@dataclass
class _SpeechMocks:
    openai: MagicMock
    prepare_audio: AsyncMock
    send_audio: AsyncMock


@pytest.fixture
def speech() -> Iterator[_SpeechMocks]:
    """Patch a successful voice message: an OpenAI speech response, prepared audio, and a delivered audio event."""
    with (
        patch("mindroom.custom_tools.matrix_voice_message.OpenAI") as openai,
        patch(
            "mindroom.custom_tools.matrix_voice_message.prepare_voice_audio_bytes",
            new_callable=AsyncMock,
            return_value=_prepared_voice_audio(),
        ) as prepare_audio,
        patch(
            "mindroom.custom_tools.matrix_voice_message.send_audio_message",
            new_callable=AsyncMock,
            return_value="$voice-event",
        ) as send_audio,
    ):
        openai.return_value.audio.speech.create.return_value = SimpleNamespace(content=b"voice-bytes")
        yield _SpeechMocks(openai=openai, prepare_audio=prepare_audio, send_audio=send_audio)


def _context(
    tmp_path: Path,
    *,
    room_id: str = "!room:localhost",
    thread_id: str | None = "$thread-root",
) -> ToolRuntimeContext:
    config = bind_runtime_paths(Config(), test_runtime_paths(tmp_path))
    conversation_reader = make_conversation_reader_mock()
    conversation_reader.latest_thread_event_id = make_latest_thread_event_id_mock("$latest")
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=None,
        ),
        requester_id="@user:localhost",
        client=_mock_client(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=conversation_reader,
    )


@pytest.mark.asyncio
async def test_matrix_voice_message_requires_runtime_context() -> None:
    """Tool calls outside Matrix runtime should return a structured error."""
    result = await MatrixVoiceMessageTools(api_key="sk-test").matrix_voice_message("hello")

    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["tool"] == "matrix_voice_message"
    assert payload["message"] == "Matrix voice message tool context is unavailable in this runtime path."


@pytest.mark.asyncio
async def test_matrix_voice_message_rejects_empty_text(tmp_path: Path) -> None:
    """Spoken text is required."""
    with tool_runtime_context(_context(tmp_path)):
        result = await MatrixVoiceMessageTools(api_key="sk-test").matrix_voice_message("  ")

    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["message"] == "text is required and must be non-empty."


@pytest.mark.asyncio
async def test_matrix_voice_message_rejects_non_string_inputs(tmp_path: Path) -> None:
    """A non-string argument fails call validation before the tool body runs."""
    with tool_runtime_context(_context(tmp_path)), pytest.raises(ValidationError):
        await call_entrypoint(_matrix_voice_message_function(), text=123)


@pytest.mark.asyncio
async def test_matrix_voice_message_rejects_unauthorized_room(tmp_path: Path) -> None:
    """Target rooms should use the shared Matrix room authorization check."""
    with (
        tool_runtime_context(_context(tmp_path)),
        patch("mindroom.custom_tools.matrix_voice_message.room_access_allowed", return_value=False),
    ):
        result = await MatrixVoiceMessageTools(api_key="sk-test").matrix_voice_message(
            "hello",
            room_id="!other:localhost",
        )

    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["room_id"] == "!other:localhost"
    assert payload["message"] == "Not authorized to access the target room."


@pytest.mark.asyncio
async def test_matrix_voice_message_generates_speech_and_sends_to_context_thread(
    tmp_path: Path,
    speech: _SpeechMocks,
) -> None:
    """Successful calls should synthesize speech and send it to the active Matrix thread."""
    context = _context(tmp_path, thread_id="$thread-root")

    with tool_runtime_context(context):
        result = await MatrixVoiceMessageTools(
            api_key="sk-test",
            model="tts-test",
            voice="nova",
        ).matrix_voice_message("Read this aloud", caption="Voice reply")

    speech.openai.assert_called_once_with(api_key="sk-test")
    speech.openai.return_value.audio.speech.create.assert_called_once_with(
        model="tts-test",
        voice="nova",
        input="Read this aloud",
        response_format="opus",
    )
    speech.prepare_audio.assert_awaited_once()
    context.conversation_reader.latest_thread_event_id.assert_awaited_once_with(
        room_id="!room:localhost",
        thread_id="$thread-root",
        known_latest_thread_event_id=None,
    )
    speech.send_audio.assert_awaited_once_with(
        context.client,
        "!room:localhost",
        b"prepared-opus-bytes",
        mimetype="audio/ogg",
        filename="voice-message.opus",
        caption="Voice reply",
        duration_ms=1250,
        waveform=[512] * 30,
        thread_id="$thread-root",
        latest_thread_event_id="$latest",
    )

    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["event_id"] == "$voice-event"
    assert payload["room_id"] == "!room:localhost"
    assert payload["thread_id"] == "$thread-root"


@pytest.mark.asyncio
async def test_matrix_voice_message_resolves_room_alias_before_send(tmp_path: Path, speech: _SpeechMocks) -> None:
    """Explicit room aliases should resolve to room IDs before authorization and delivery."""
    context = _context(tmp_path, thread_id=None)
    state = MatrixState()
    state.add_room("ops", room_id="!ops:localhost", alias="#ops:localhost", name="Ops")
    save_matrix_state(state, context.runtime_paths)

    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.matrix_voice_message.room_access_allowed", return_value=True) as mock_access,
    ):
        result = await MatrixVoiceMessageTools(api_key="sk-test", model="tts-test").matrix_voice_message(
            "send to ops",
            room_id="#ops:localhost",
        )

    mock_access.assert_called_once_with(context, "!ops:localhost")
    speech.send_audio.assert_awaited_once()
    assert speech.send_audio.await_args.args[1] == "!ops:localhost"

    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["room_id"] == "!ops:localhost"


@pytest.mark.asyncio
async def test_matrix_voice_message_room_sentinel_forces_room_level_send(tmp_path: Path, speech: _SpeechMocks) -> None:
    """thread_id='room' should not inherit the active thread."""
    context = _context(tmp_path, thread_id="$thread-root")

    with tool_runtime_context(context):
        result = await MatrixVoiceMessageTools(api_key="sk-test", model="tts-test").matrix_voice_message(
            "room-level",
            thread_id="room",
        )

    context.conversation_reader.latest_thread_event_id.assert_not_awaited()
    assert speech.send_audio.await_args.kwargs["thread_id"] is None
    assert speech.send_audio.await_args.kwargs["latest_thread_event_id"] is None

    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["thread_id"] is None


@pytest.mark.asyncio
async def test_matrix_voice_message_to_a_thread_reply_sends_in_its_thread(
    tmp_path: Path,
    speech: _SpeechMocks,
) -> None:
    """A reply event ID as thread_id sends into that reply's thread, since Matrix rejects a reply as a thread root."""
    context = replace(
        _context(tmp_path, thread_id=None),
        relations=make_thread_reply_lookup("$reply-in-thread", "$thread-root"),
    )

    with tool_runtime_context(context):
        result = await MatrixVoiceMessageTools(api_key="sk-test", model="tts-test").matrix_voice_message(
            "into the thread",
            thread_id="$reply-in-thread",
        )

    assert speech.send_audio.await_args.kwargs["thread_id"] == "$thread-root"
    assert _payload(result)["thread_id"] == "$thread-root"


@pytest.mark.asyncio
async def test_matrix_voice_message_companion_message_sends_to_same_thread(
    tmp_path: Path,
    speech: _SpeechMocks,
) -> None:
    """Optional companion text should use the same target before voice delivery."""
    context = _context(tmp_path, thread_id="$thread-root")

    with (
        tool_runtime_context(context),
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$companion-event")),
        ) as mock_text_send,
    ):
        result = await MatrixVoiceMessageTools(api_key="sk-test", model="tts-test").matrix_voice_message(
            "spoken version",
            caption="Audio body",
            companion_message="Readable transcript",
        )

    sent_text_content = mock_text_send.await_args.args[2]
    assert sent_text_content["body"] == "Readable transcript"
    assert sent_text_content["m.relates_to"] == {
        "rel_type": "m.thread",
        "event_id": "$thread-root",
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": "$latest"},
    }
    assert sent_text_content["com.mindroom.skip_mentions"] is True
    speech.send_audio.assert_awaited_once()
    assert speech.send_audio.await_args.kwargs["caption"] == "Audio body"
    assert speech.send_audio.await_args.kwargs["thread_id"] == "$thread-root"
    # The companion text is the newest event in the thread by the time the audio
    # goes out. Its echo has not arrived, so the projection still answers
    # `$latest` -- which is what the companion itself correctly replied to.
    assert speech.send_audio.await_args.kwargs["latest_thread_event_id"] == "$companion-event"
    # The companion text resolved its own fallback from the projection; the
    # audio was handed the companion instead of reading a projection that
    # cannot have seen it yet.
    assert [call.kwargs for call in context.conversation_reader.latest_thread_event_id.await_args_list] == [
        {"room_id": "!room:localhost", "thread_id": "$thread-root", "known_latest_thread_event_id": None},
        {
            "room_id": "!room:localhost",
            "thread_id": "$thread-root",
            "known_latest_thread_event_id": "$companion-event",
        },
    ]

    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["event_id"] == "$voice-event"
    assert payload["companion_event_id"] == "$companion-event"


@pytest.mark.asyncio
@pytest.mark.parametrize(("caption", "expected_caption"), [("  Audio body  ", "Audio body"), ("   ", None)])
async def test_matrix_voice_message_strips_caption_and_skips_blank_companion(
    tmp_path: Path,
    caption: str,
    expected_caption: str | None,
    speech: _SpeechMocks,
) -> None:
    """A padded caption is stripped, a blank one falls back to the filename, and blank companion text sends nothing."""
    context = _context(tmp_path, thread_id="$thread-root")

    with (
        tool_runtime_context(context),
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new_callable=AsyncMock,
        ) as mock_text_send,
    ):
        result = await MatrixVoiceMessageTools(api_key="sk-test", model="tts-test").matrix_voice_message(
            "spoken version",
            caption=caption,
            companion_message="   ",
        )

    mock_text_send.assert_not_awaited()
    assert speech.send_audio.await_args.kwargs["caption"] == expected_caption
    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["companion_event_id"] is None


@pytest.mark.asyncio
async def test_matrix_voice_message_voice_failure_reports_sent_companion(tmp_path: Path, speech: _SpeechMocks) -> None:
    """A failed voice send should report that the companion text was already delivered."""
    context = _context(tmp_path, thread_id="$thread-root")

    with (
        tool_runtime_context(context),
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$companion-event")),
        ),
    ):
        speech.send_audio.return_value = None

        result = await MatrixVoiceMessageTools(api_key="sk-test", model="tts-test").matrix_voice_message(
            "spoken version",
            companion_message="Readable transcript",
        )

    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["companion_event_id"] == "$companion-event"
    assert payload["message"] == "Failed to send voice message to Matrix."


@pytest.mark.asyncio
async def test_matrix_voice_message_generation_error_is_sanitized(tmp_path: Path, speech: _SpeechMocks) -> None:
    """Provider errors should not leak raw API failure text into Matrix."""
    context = _context(tmp_path, thread_id="$thread-root")

    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.matrix_voice_message.logger") as mock_logger,
    ):
        speech.openai.return_value.audio.speech.create.side_effect = RuntimeError("bad key sk-secret")

        result = await MatrixVoiceMessageTools(api_key="sk-test", model="tts-test").matrix_voice_message("hello")

    speech.send_audio.assert_not_awaited()
    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["message"] == "Failed to generate speech."
    assert "sk-secret" not in result
    mock_logger.error.assert_called_once()


@pytest.mark.asyncio
async def test_matrix_voice_message_missing_thread_fallback_is_structured_error(
    tmp_path: Path,
    speech: _SpeechMocks,
) -> None:
    """Missing thread fallback should not escape as a delivery ValueError."""
    context = _context(tmp_path, thread_id="$thread-root")
    context.conversation_reader.latest_thread_event_id = AsyncMock(return_value=None)

    with tool_runtime_context(context):
        result = await MatrixVoiceMessageTools(api_key="sk-test", model="tts-test").matrix_voice_message("hello")

    speech.openai.assert_not_called()
    speech.send_audio.assert_not_awaited()
    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["message"] == "Failed to resolve Matrix thread fallback for voice message."


@pytest.mark.asyncio
async def test_matrix_voice_message_can_use_local_openai_compatible_tts(tmp_path: Path, speech: _SpeechMocks) -> None:
    """Local TTS base URLs should not require or send the user's OpenAI key."""
    context = _context(tmp_path, thread_id=None)

    with tool_runtime_context(context):
        result = await MatrixVoiceMessageTools(
            model="kokoro",
            base_url="http://pc.local:10201",
            voice="af_heart",
            response_format="wav",
        ).matrix_voice_message("local speech")

    speech.openai.assert_called_once_with(api_key="sk-no-key-required", base_url="http://pc.local:10201/v1")
    speech.openai.return_value.audio.speech.create.assert_called_once_with(
        model="kokoro",
        voice="af_heart",
        input="local speech",
        response_format="wav",
    )
    speech.send_audio.assert_awaited_once()
    payload = _payload(result)
    assert payload["status"] == "ok"


@pytest.mark.asyncio
async def test_matrix_voice_message_routes_openrouter_models_through_openrouter(
    tmp_path: Path,
    speech: _SpeechMocks,
) -> None:
    """Provider-prefixed model IDs should use OpenRouter's speech endpoint with OPENROUTER_API_KEY and mp3 audio."""
    context = _context(tmp_path, thread_id=None)

    def fake_secret(name: str, _runtime_paths: object) -> str | None:
        return "sk-or-test" if name == "OPENROUTER_API_KEY" else None

    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.matrix_voice_message.get_secret_from_env", side_effect=fake_secret),
    ):
        result = await MatrixVoiceMessageTools(
            model="hexgrad/kokoro-82m",
            voice="alloy",
        ).matrix_voice_message("openrouter speech")

    speech.openai.assert_called_once_with(api_key="sk-or-test", base_url="https://openrouter.ai/api/v1")
    speech.openai.return_value.audio.speech.create.assert_called_once_with(
        model="hexgrad/kokoro-82m",
        voice="alloy",
        input="openrouter speech",
        response_format="mp3",
    )
    assert speech.prepare_audio.await_args.kwargs["response_format"] == "mp3"
    payload = _payload(result)
    assert payload["status"] == "ok"


@pytest.mark.asyncio
async def test_matrix_voice_message_openrouter_model_without_key_is_structured_error(tmp_path: Path) -> None:
    """OpenRouter voice models without OPENROUTER_API_KEY should fail preflight with a targeted message."""
    context = _context(tmp_path, thread_id=None)

    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.matrix_voice_message.get_secret_from_env", return_value=None),
        patch("mindroom.custom_tools.matrix_voice_message.OpenAI") as mock_openai,
    ):
        result = await MatrixVoiceMessageTools(model="hexgrad/kokoro-82m").matrix_voice_message("hello")

    mock_openai.assert_not_called()
    payload = _payload(result)
    assert payload["status"] == "error"
    assert (
        payload["message"]
        == "OPENROUTER_API_KEY is required to use an OpenRouter voice model with matrix_voice_message."
    )


@pytest.mark.asyncio
async def test_matrix_voice_message_explicit_base_url_overrides_openrouter_model_routing(
    tmp_path: Path,
    speech: _SpeechMocks,
) -> None:
    """An explicit base URL should win over OpenRouter model detection and keep the configured format."""
    context = _context(tmp_path, thread_id=None)

    with tool_runtime_context(context):
        result = await MatrixVoiceMessageTools(
            model="hexgrad/kokoro",
            base_url="http://pc.local:10201",
            response_format="wav",
        ).matrix_voice_message("local speech")

    speech.openai.assert_called_once_with(api_key="sk-no-key-required", base_url="http://pc.local:10201/v1")
    assert speech.openai.return_value.audio.speech.create.call_args.kwargs["response_format"] == "wav"
    payload = _payload(result)
    assert payload["status"] == "ok"


@pytest.mark.asyncio
async def test_matrix_voice_message_openrouter_base_url_uses_openrouter_key(
    tmp_path: Path,
    speech: _SpeechMocks,
) -> None:
    """An explicit OpenRouter base URL should use OPENROUTER_API_KEY, not the local placeholder key."""
    context = _context(tmp_path, thread_id=None)

    def fake_secret(name: str, _runtime_paths: object) -> str | None:
        return "sk-or-test" if name == "OPENROUTER_API_KEY" else None

    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.matrix_voice_message.get_secret_from_env", side_effect=fake_secret),
    ):
        result = await MatrixVoiceMessageTools(
            model="mistralai/voxtral-mini-tts",
            base_url="https://openrouter.ai/api/v1",
        ).matrix_voice_message("openrouter speech")

    speech.openai.assert_called_once_with(api_key="sk-or-test", base_url="https://openrouter.ai/api/v1")
    assert speech.openai.return_value.audio.speech.create.call_args.kwargs["response_format"] == "mp3"
    payload = _payload(result)
    assert payload["status"] == "ok"


@pytest.mark.asyncio
async def test_matrix_voice_message_rejects_invalid_response_format_even_for_openrouter(tmp_path: Path) -> None:
    """Misconfigured response formats should fail preflight before OpenRouter mp3 coercion hides them."""
    context = _context(tmp_path, thread_id=None)

    def fake_secret(name: str, _runtime_paths: object) -> str | None:
        return "sk-or-test" if name == "OPENROUTER_API_KEY" else None

    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.matrix_voice_message.get_secret_from_env", side_effect=fake_secret),
        patch("mindroom.custom_tools.matrix_voice_message.OpenAI") as mock_openai,
    ):
        result = await MatrixVoiceMessageTools(
            model="hexgrad/kokoro-82m",
            response_format="ogg",
        ).matrix_voice_message("hello")

    mock_openai.assert_not_called()
    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["message"] == "response_format must be one of: aac, flac, mp3, opus, wav."


@pytest.mark.asyncio
async def test_matrix_voice_message_rejects_unknown_response_format(tmp_path: Path) -> None:
    """Unsupported response formats should fail preflight before any TTS call."""
    context = _context(tmp_path, thread_id=None)

    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.matrix_voice_message.OpenAI") as mock_openai,
    ):
        result = await MatrixVoiceMessageTools(
            api_key="sk-test",
            response_format="pcm",
        ).matrix_voice_message("hello")

    mock_openai.assert_not_called()
    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["message"] == "response_format must be one of: aac, flac, mp3, opus, wav."


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("http://pc.local:10201", "http://pc.local:10201/v1"),
        ("http://pc.local:10201/", "http://pc.local:10201/v1"),
        ("http://pc.local:10201/v1", "http://pc.local:10201/v1"),
        ("http://pc.local:10201/v1/", "http://pc.local:10201/v1"),
        ("http://pc.local:10201/v1/audio/speech", "http://pc.local:10201/v1"),
        # A full endpoint URL without /v1 means the server serves without a /v1 prefix.
        ("http://pc.local:10201/audio/speech", "http://pc.local:10201"),
    ],
)
def test_normalize_openai_base_url(base_url: str | None, expected: str | None) -> None:
    """Base URLs should normalize to what the OpenAI client expects."""
    assert _normalize_openai_base_url(base_url) == expected


def test_matrix_voice_message_description_covers_critical_behavior() -> None:
    """Processed description should explain the key Matrix voice behavior."""
    function = _matrix_voice_message_function()
    description = function.description

    assert description is not None
    assert len(description) <= 800
    assert "send a Matrix voice message" in description
    assert "configured text-to-speech" in description
    assert 'thread_id="room"' in description
    assert "companion_message" in description
    docstring = inspect.getdoc(MatrixVoiceMessageTools.matrix_voice_message)
    assert docstring is not None
    assert len(docstring) <= 800


def test_matrix_voice_message_parameter_descriptions_are_exposed() -> None:
    """Docstring Args should populate the tool parameter schema."""
    function = _matrix_voice_message_function()
    properties = function.parameters["properties"]

    assert "spoken content" in properties["text"]["description"]
    assert "Matrix event body" in properties["caption"]["description"]
    assert "Normal text message" in properties["companion_message"]["description"]
    assert 'thread_id="room"' in properties["thread_id"]["description"]


@pytest.mark.asyncio
async def test_matrix_voice_message_realtime_model_sends_wav_through_voice_preparation(
    tmp_path: Path,
    speech: _SpeechMocks,
) -> None:
    """The default Realtime model reads the text aloud and its WAV becomes the voice note."""
    context = _context(tmp_path, thread_id="$thread-root")

    with (
        tool_runtime_context(context),
        patch(
            "mindroom.custom_tools.matrix_voice_message.synthesize_realtime_speech",
            new_callable=AsyncMock,
            return_value=b"\x00\x00",
        ) as mock_synthesize,
    ):
        result = await MatrixVoiceMessageTools(api_key="sk-test").matrix_voice_message("Read this aloud")

    assert _payload(result)["status"] == "ok"
    mock_synthesize.assert_awaited_once_with(
        "Read this aloud",
        model="gpt-realtime-2.1-mini",
        voice="alloy",
        api_key="sk-test",
        base_url=None,
    )
    speech.prepare_audio.assert_awaited_once_with(pcm_to_wav(b"\x00\x00"), response_format="wav")
    speech.openai.assert_not_called()


@pytest.mark.asyncio
async def test_matrix_voice_message_realtime_failure_is_sanitized(tmp_path: Path) -> None:
    """A Realtime failure reports a generic error and sends nothing."""
    context = _context(tmp_path, thread_id="$thread-root")

    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.matrix_voice_message.logger") as mock_logger,
        patch(
            "mindroom.custom_tools.matrix_voice_message.synthesize_realtime_speech",
            new_callable=AsyncMock,
            side_effect=RealtimeSpeechError(
                "boom sk-secret",
                code="invalid_value",
                param="session.audio.output.voice",
            ),
        ),
        patch("mindroom.custom_tools.matrix_voice_message.send_audio_message", new_callable=AsyncMock) as mock_send,
    ):
        result = await MatrixVoiceMessageTools(api_key="sk-test").matrix_voice_message("hello")

    mock_send.assert_not_awaited()
    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["message"] == "Failed to generate speech."
    assert "sk-secret" not in result
    log_fields = mock_logger.error.call_args.kwargs
    assert log_fields["error_code"] == "invalid_value"
    assert log_fields["error_param"] == "session.audio.output.voice"
    assert "sk-secret" not in str(mock_logger.error.call_args)


@pytest.mark.asyncio
async def test_matrix_voice_message_without_ffmpeg_fails_before_sending_or_speaking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host without ffmpeg rejects a Realtime voice note before the companion text or the paid speech."""
    monkeypatch.setattr("mindroom.matrix.voice_message.shutil.which", lambda _name: None)
    context = _context(tmp_path, thread_id="$thread-root")

    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.matrix_conversation_operations.send_message_result") as mock_text_send,
        patch(
            "mindroom.custom_tools.matrix_voice_message.synthesize_realtime_speech",
            new_callable=AsyncMock,
        ) as mock_synthesize,
    ):
        result = await MatrixVoiceMessageTools(api_key="sk-test").matrix_voice_message(
            "hello",
            companion_message="Readable transcript",
        )

    payload = _payload(result)
    assert payload["status"] == "error"
    assert "ffmpeg" in str(payload["message"])
    mock_text_send.assert_not_called()
    mock_synthesize.assert_not_awaited()
