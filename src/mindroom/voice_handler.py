"""Voice message handler with speech-to-text and light transcript normalization."""

from __future__ import annotations

import asyncio
import mimetypes
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from agno.agent import Agent
from agno.media import Audio

from mindroom import model_loading
from mindroom.attachments import register_audio_attachment
from mindroom.authorization import responder_candidate_entities_for_room
from mindroom.constants import ATTACHMENT_IDS_KEY, ORIGINAL_SENDER_KEY, VOICE_PREFIX, VOICE_RAW_AUDIO_FALLBACK_KEY
from mindroom.credentials_sync import get_secret_from_env
from mindroom.entity_resolution import EntityIdentityRegistry, entity_identity_registry
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import parse_current_matrix_user_id
from mindroom.matrix.media import AudioMessageEvent, download_media_bytes, extract_media_caption, media_mime_type
from mindroom.matrix.mentions import format_message_with_mentions, resolve_entity_name_for_mention_localpart

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)
_STT_AUDIO_EXTENSION_BY_MIME_TYPE = {
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/webm": ".webm",
    "audio/x-m4a": ".m4a",
    "audio/x-wav": ".wav",
}
_VOICE_MENTION_PATTERN = re.compile(
    r"(?<![\w])@(?P<localpart>[A-Za-z0-9._=/+\-]+)(?::(?P<domain>[A-Za-z0-9.\-:\[\]]+))?",
)
_VOICE_MENTION_TRAILING_PUNCTUATION = ".,!?:;"


@dataclass(frozen=True)
class _PreparedVoiceMessage:
    """Normalized text + attachment metadata derived from one audio event."""

    text: str
    source: dict[str, Any]


@dataclass(frozen=True)
class _NormalizedVoiceMessage:
    """Cached audio normalization shared across bots for one room/thread event."""

    attachment_id: str | None
    transcribed_message: str | None


_VOICE_NORMALIZATION_CACHE_MAX_ENTRIES = 128
_voice_normalization_cache: OrderedDict[tuple[str, str, str, str], _NormalizedVoiceMessage] = OrderedDict()
_voice_normalization_tasks: dict[tuple[str, str, str, str], asyncio.Task[_NormalizedVoiceMessage | None]] = {}


def _voice_cache_key(
    storage_path: Path,
    room_id: str,
    event_id: str,
    thread_id: str | None,
) -> tuple[str, str, str, str]:
    """Build a stable cache key for one audio event in one room/thread context."""
    return (str(storage_path.resolve()), room_id, event_id, thread_id or "")


def _get_cached_voice_normalization(
    cache_key: tuple[str, str, str, str],
) -> _NormalizedVoiceMessage | None:
    """Return a cached normalization result and refresh its LRU position."""
    cached = _voice_normalization_cache.get(cache_key)
    if cached is None:
        return None
    _voice_normalization_cache.move_to_end(cache_key)
    return cached


def _store_cached_voice_normalization(
    cache_key: tuple[str, str, str, str],
    normalized: _NormalizedVoiceMessage,
) -> None:
    """Persist a normalization result in the bounded in-memory cache."""
    _voice_normalization_cache[cache_key] = normalized
    _voice_normalization_cache.move_to_end(cache_key)
    while len(_voice_normalization_cache) > _VOICE_NORMALIZATION_CACHE_MAX_ENTRIES:
        _voice_normalization_cache.popitem(last=False)


def _finalize_inflight_voice_normalization_task(
    cache_key: tuple[str, str, str, str],
    task: asyncio.Task[_NormalizedVoiceMessage | None],
) -> None:
    """Persist successful results and remove an in-flight normalization task."""
    try:
        normalized = task.result()
    except asyncio.CancelledError:
        normalized = None
    except Exception:
        logger.exception("Voice normalization task failed")
        normalized = None

    if normalized is not None:
        _store_cached_voice_normalization(cache_key, normalized)
    if _voice_normalization_tasks.get(cache_key) is task:
        _voice_normalization_tasks.pop(cache_key, None)


async def _compute_normalized_voice_message(
    client: nio.AsyncClient,
    storage_path: Path,
    room: nio.MatrixRoom,
    event: AudioMessageEvent,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    thread_id: str | None,
) -> _NormalizedVoiceMessage | None:
    """Download, register, and transcribe one audio event."""
    audio = await _download_audio(client, event)
    if audio is None or audio.content is None:
        logger.error("Failed to download audio file")
        return None

    attachment_record = await register_audio_attachment(
        storage_path,
        event_id=event.event_id,
        audio_bytes=audio.content,
        mime_type=audio.mime_type,
        room_id=room.room_id,
        thread_id=thread_id,
        sender=event.sender,
        filename=event.body if isinstance(event.body, str) else None,
    )

    transcribed_message = await _handle_voice_message(
        client,
        room,
        event,
        config,
        runtime_paths,
        audio=audio,
    )
    if not isinstance(transcribed_message, str) or not transcribed_message.strip():
        transcribed_message = None

    return _NormalizedVoiceMessage(
        attachment_id=attachment_record.attachment_id if attachment_record is not None else None,
        transcribed_message=transcribed_message,
    )


async def _normalize_voice_message(
    client: nio.AsyncClient,
    storage_path: Path,
    room: nio.MatrixRoom,
    event: AudioMessageEvent,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    thread_id: str | None,
) -> _NormalizedVoiceMessage | None:
    """Download, register, and transcribe one audio event at most once per context."""
    cache_key = _voice_cache_key(storage_path, room.room_id, event.event_id, thread_id)
    cached = _get_cached_voice_normalization(cache_key)
    if cached is not None:
        return cached

    task = _voice_normalization_tasks.get(cache_key)
    if task is None:
        task = asyncio.create_task(
            _compute_normalized_voice_message(
                client,
                storage_path,
                room,
                event,
                config,
                runtime_paths,
                thread_id=thread_id,
            ),
        )
        _voice_normalization_tasks[cache_key] = task
        task.add_done_callback(lambda done_task: _finalize_inflight_voice_normalization_task(cache_key, done_task))

    return await asyncio.shield(task)


async def prepare_voice_message(
    client: nio.AsyncClient,
    storage_path: Path,
    room: nio.MatrixRoom,
    event: AudioMessageEvent,
    config: Config,
    *,
    runtime_paths: RuntimePaths,
    thread_id: str | None,
) -> _PreparedVoiceMessage | None:
    """Download/register audio and normalize it into a synthetic text event."""
    normalized = await _normalize_voice_message(
        client,
        storage_path,
        room,
        event,
        config,
        runtime_paths,
        thread_id=thread_id,
    )
    if normalized is None:
        return None

    attachment_id = normalized.attachment_id
    text = (
        normalized.transcribed_message
        or f"{VOICE_PREFIX}{extract_media_caption(event, default='[Attached voice message]')}"
    )

    extra_content: dict[str, Any] = {ORIGINAL_SENDER_KEY: event.sender}
    if attachment_id is not None:
        extra_content[ATTACHMENT_IDS_KEY] = [attachment_id]
    if normalized.transcribed_message is None:
        extra_content[VOICE_RAW_AUDIO_FALLBACK_KEY] = True
    original_content = event.source.get("content") if isinstance(event.source, dict) else None
    inherited_mentions = original_content.get("m.mentions") if isinstance(original_content, dict) else None
    if isinstance(inherited_mentions, dict):
        extra_content["m.mentions"] = inherited_mentions

    source = dict(event.source) if isinstance(event.source, dict) else {}
    content: dict[str, Any] = {}
    if isinstance(original_content, dict):
        relates_to = original_content.get("m.relates_to")
        if isinstance(relates_to, dict):
            content["m.relates_to"] = relates_to
    content.update(
        format_message_with_mentions(
            config,
            runtime_paths,
            text,
            extra_content=extra_content,
        ),
    )
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    source["content"] = content

    return _PreparedVoiceMessage(
        text=text,
        source=source,
    )


async def _handle_voice_message(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,
    event: AudioMessageEvent,
    config: Config,
    runtime_paths: RuntimePaths,
    audio: Audio | None = None,
) -> str | None:
    """Handle a voice message event.

    Args:
        client: Matrix client
        room: Matrix room
        event: Voice message event
        config: Application configuration
        runtime_paths: Explicit runtime context for secrets and agent mention resolution
        audio: Optional pre-downloaded audio payload to reuse across fallbacks

    Returns:
        The transcribed and formatted message, or None if transcription failed

    """
    if not config.voice.enabled:
        return None

    try:
        voice_audio = audio or await _download_audio(client, event)
        if voice_audio is None or voice_audio.content is None:
            logger.error("Failed to download audio file")
            return None

        # Transcribe the audio
        transcription = await _transcribe_audio(
            voice_audio.content,
            config,
            runtime_paths,
            mime_type=voice_audio.mime_type,
        )
        if not transcription:
            logger.warning("Failed to transcribe audio or empty transcription")
            return None

        logger.info("voice_transcription_received", transcription=transcription)

        available_agent_names, available_team_names = await _get_available_entities_for_sender(
            client,
            room,
            event.sender,
            config,
            runtime_paths,
        )

        # Process transcription with AI for command/agent recognition
        formatted_message = await _process_transcription(
            transcription,
            config,
            runtime_paths,
            available_agent_names=available_agent_names,
            available_team_names=available_team_names,
        )

        logger.info("voice_message_formatted", formatted_message=formatted_message)

        if formatted_message:
            # Add a note that this was transcribed from voice
            return f"{VOICE_PREFIX}{formatted_message}"

    except Exception:
        logger.exception("Error handling voice message")
        return None
    return None


async def _download_audio(
    client: nio.AsyncClient,
    event: AudioMessageEvent,
) -> Audio | None:
    """Download Matrix audio and convert it to an agno Audio media object."""
    audio_data = await download_media_bytes(client, event)
    if audio_data is None:
        return None

    return Audio(content=audio_data, mime_type=media_mime_type(event))


def _stt_upload_filename_and_mime_type(mime_type: str | None) -> tuple[str, str]:
    """Return multipart filename and MIME type for an STT upload."""
    normalized_mime_type = (mime_type or "audio/ogg").split(";", 1)[0].strip().lower() or "audio/ogg"
    extension = _STT_AUDIO_EXTENSION_BY_MIME_TYPE.get(normalized_mime_type)
    if extension is None:
        extension = mimetypes.guess_extension(normalized_mime_type) or ".bin"
    return f"audio{extension}", normalized_mime_type


async def _transcribe_audio(
    audio_data: bytes,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    mime_type: str | None = None,
) -> str | None:
    """Transcribe audio using OpenAI-compatible API.

    Args:
        audio_data: Audio file bytes
        config: Application configuration
        runtime_paths: Explicit runtime context for STT credential lookup
        mime_type: Matrix-declared MIME type for the audio payload

    Returns:
        Transcription text or None if failed

    """
    try:
        stt_host = config.voice.stt.host
        url = f"{stt_host}/v1/audio/transcriptions" if stt_host else "https://api.openai.com/v1/audio/transcriptions"

        api_key = config.voice.stt.api_key or get_secret_from_env("OPENAI_API_KEY", runtime_paths)
        if not api_key:
            logger.error("No OpenAI-compatible STT API key configured for voice transcription")
            return None
        headers = {"Authorization": f"Bearer {api_key}"}

        filename, upload_mime_type = _stt_upload_filename_and_mime_type(mime_type)
        files = {"file": (filename, audio_data, upload_mime_type)}
        form_data = {"model": config.voice.stt.model}

        async with httpx.AsyncClient(verify=False) as http_client:  # noqa: S501
            response = await http_client.post(url, headers=headers, files=files, data=form_data)
            if response.status_code != 200:
                logger.error(
                    "stt_api_error",
                    status_code=response.status_code,
                    error=response.text,
                )
                return None

            result = response.json()
            return result.get("text", "").strip()

    except Exception:
        logger.exception("Error transcribing audio")
        return None


async def _process_transcription(
    transcription: str,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    available_agent_names: list[str] | None = None,
    available_team_names: list[str] | None = None,
) -> str:
    """Process transcription to normalize mentions and light ASR cleanup.

    Args:
        transcription: Raw transcription text
        config: Application configuration
        runtime_paths: Explicit runtime context for agent and team mention resolution
        available_agent_names: Optional room-scoped list of available agent names
        available_team_names: Optional room-scoped list of available team names

    Returns:
        Formatted message with proper mentions and cleanup

    """
    try:
        # Get list of available agents and teams
        agent_names = available_agent_names if available_agent_names is not None else list(config.agents.keys())
        team_names = available_team_names if available_team_names is not None else list(config.teams.keys())
        agent_display_names = {name: config.agents[name].display_name for name in agent_names if name in config.agents}
        team_display_names = {name: config.teams[name].display_name for name in team_names if name in config.teams}
        registry = entity_identity_registry(config, runtime_paths)

        agent_list = (
            "\n".join(
                [
                    f"  - @{name} or {registry.current_id(name).full_id} (spoken as: {agent_display_names[name]})"
                    for name in agent_names
                ],
            )
            if agent_names
            else "  (none)"
        )
        team_list = (
            "\n".join(
                [
                    f"  - @{name} or {registry.current_id(name).full_id} (spoken as: {team_display_names[name]})"
                    for name in team_names
                ],
            )
            if team_names
            else "  (none)"
        )

        # Build the prompt for the AI
        prompt = config.render_prompt(
            "VOICE_TRANSCRIPTION_NORMALIZER_PROMPT_TEMPLATE",
            agent_list=agent_list,
            team_list=team_list,
            transcription=transcription,
        )

        # Get the AI model to process the transcription
        model = model_loading.get_model_instance(config, runtime_paths, config.voice.intelligence.model)

        # Create an agent for voice command processing
        agent = Agent(
            name="VoiceTranscriptionNormalizer",
            role="Normalize voice transcriptions while preserving natural language and mention intent",
            model=model,
            telemetry=False,
        )

        # Process the transcription with the agent
        session_id = f"voice_process_{uuid.uuid4()}"
        response = await agent.arun(prompt, session_id=session_id)

        # Extract the content from the response
        if response and response.content:
            return _sanitize_unavailable_mentions(
                response.content.strip(),
                allowed_entities=set(agent_names) | set(team_names),
                config=config,
                runtime_paths=runtime_paths,
            )

    except Exception as e:
        logger.exception("Error processing transcription")
        # Return error message so user knows what happened
        from mindroom.error_handling import get_user_friendly_error_message  # noqa: PLC0415

        return get_user_friendly_error_message(e, "VoiceProcessor")
    else:
        # Return original transcription if no valid response from model
        return transcription


async def _get_available_entities_for_sender(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,
    sender_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[list[str], list[str]]:
    """Return available agent and team names in this room for a specific sender."""
    available_agent_names: list[str] = []
    available_team_names: list[str] = []
    registry = entity_identity_registry(config, runtime_paths)

    for matrix_id in await responder_candidate_entities_for_room(
        client,
        room,
        sender_id,
        config,
        runtime_paths,
    ):
        name = registry.current_entity_name_for_user_id(matrix_id.full_id, include_router=False)
        if name is None:
            continue
        if name in config.agents:
            available_agent_names.append(name)
        elif name in config.teams:
            available_team_names.append(name)

    return available_agent_names, available_team_names


def _sanitize_unavailable_mentions(
    text: str,
    *,
    allowed_entities: set[str],
    config: Config,
    runtime_paths: RuntimePaths,
) -> str:
    """Strip @ from mentions that target configured but unavailable entities."""
    if not text:
        return text

    allowed_lower = {name.lower() for name in allowed_entities}
    registry = entity_identity_registry(config, runtime_paths)

    def _replace(match: re.Match[str]) -> str:
        raw_token = match.group(0)
        token = raw_token.rstrip(_VOICE_MENTION_TRAILING_PUNCTUATION)
        trailing_punctuation = raw_token[len(token) :]
        configured_name = _voice_mention_entity_name(token, registry, config)
        if configured_name is None:
            return raw_token
        if configured_name.lower() in allowed_lower:
            return raw_token
        # Strip only '@', preserving exact matched token shape (mindroom_ prefix/domain suffix/case).
        return f"{token[1:]}{trailing_punctuation}"

    return _VOICE_MENTION_PATTERN.sub(_replace, text)


def _voice_mention_entity_name(
    token: str,
    registry: EntityIdentityRegistry,
    config: Config,
) -> str | None:
    """Resolve one voice-normalizer mention token using Matrix mention semantics."""
    body = token[1:]
    localpart, separator, _domain = body.partition(":")
    if separator:
        try:
            user_id = parse_current_matrix_user_id(token)
        except ValueError:
            return None
        return registry.current_entity_name_for_user_id(user_id, include_router=False)

    return resolve_entity_name_for_mention_localpart(localpart, config)
