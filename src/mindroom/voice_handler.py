"""Voice message handler with speech-to-text and intelligent command recognition."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from agno.agent import Agent
from agno.media import Audio

from mindroom.ai import get_model_instance
from mindroom.attachments import register_audio_attachment
from mindroom.authorization import get_available_agents_for_sender
from mindroom.commands.parsing import get_command_list
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    VOICE_PREFIX,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
)
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import agent_username_localpart
from mindroom.matrix.media import download_media_bytes, extract_media_caption, media_mime_type
from mindroom.matrix.mentions import format_message_with_mentions

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.config.main import Config

logger = get_logger(__name__)
_VOICE_MENTION_PATTERN = re.compile(
    r"(?<![\w])@(?:(?P<prefix>mindroom_))?(?P<name>[A-Za-z0-9_]+)(?::[A-Za-z0-9.\-]+)?",
)
_VOICE_COMMAND_PATTERN = re.compile(r"^!(?P<command>[a-zA-Z][a-zA-Z0-9_-]*)\b")
_VOICE_SKILL_INTENT_PATTERN = re.compile(
    r"^\s*skill\b|\b(?:run|use|execute|invoke|trigger)\s+(?:the\s+)?skill\b|\b(?:bang|exclamation(?:\s+mark)?)\s+skill\b",
)
_VOICE_HELP_INTENT_PATTERN = re.compile(
    r"^\s*help\b|\bshow(?: me)?\s+(?:the\s+)?help\b|\bhelp\s+command\b|\bwhat\s+commands?\b",
)


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
    event: nio.RoomMessageAudio | nio.RoomEncryptedAudio,
    config: Config,
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

    transcribed_message = await _handle_voice_message(client, room, event, config, audio=audio)
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
    event: nio.RoomMessageAudio | nio.RoomEncryptedAudio,
    config: Config,
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
    event: nio.RoomMessageAudio | nio.RoomEncryptedAudio,
    config: Config,
    *,
    sender_domain: str,
    thread_id: str | None,
) -> _PreparedVoiceMessage | None:
    """Download/register audio and normalize it into a synthetic text event."""
    normalized = await _normalize_voice_message(
        client,
        storage_path,
        room,
        event,
        config,
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
    copied_content = source.get("content")
    content = dict(copied_content) if isinstance(copied_content, dict) else {}
    content.update(
        format_message_with_mentions(
            config,
            text,
            sender_domain=sender_domain,
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
    event: nio.RoomMessageAudio | nio.RoomEncryptedAudio,
    config: Config,
    audio: Audio | None = None,
) -> str | None:
    """Handle a voice message event.

    Args:
        client: Matrix client
        room: Matrix room
        event: Voice message event
        config: Application configuration
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
        transcription = await _transcribe_audio(voice_audio.content, config)
        if not transcription:
            logger.warning("Failed to transcribe audio or empty transcription")
            return None

        logger.info(f"Raw transcription: {transcription}")

        available_agent_names, available_team_names = _get_available_entities_for_sender(room, event.sender, config)

        # Process transcription with AI for command/agent recognition
        formatted_message = await _process_transcription(
            transcription,
            config,
            available_agent_names=available_agent_names,
            available_team_names=available_team_names,
        )

        logger.info(f"Formatted message: {formatted_message}")

        if formatted_message:
            # Add a note that this was transcribed from voice
            return f"{VOICE_PREFIX}{formatted_message}"

    except Exception:
        logger.exception("Error handling voice message")
        return None
    return None


async def _download_audio(
    client: nio.AsyncClient,
    event: nio.RoomMessageAudio | nio.RoomEncryptedAudio,
) -> Audio | None:
    """Download Matrix audio and convert it to an agno Audio media object."""
    audio_data = await download_media_bytes(client, event)
    if audio_data is None:
        return None

    return Audio(content=audio_data, mime_type=media_mime_type(event))


async def _transcribe_audio(audio_data: bytes, config: Config) -> str | None:
    """Transcribe audio using OpenAI-compatible API.

    Args:
        audio_data: Audio file bytes
        config: Application configuration

    Returns:
        Transcription text or None if failed

    """
    try:
        stt_host = config.voice.stt.host
        url = f"{stt_host}/v1/audio/transcriptions" if stt_host else "https://api.openai.com/v1/audio/transcriptions"

        api_key = config.voice.stt.api_key or os.getenv("OPENAI_API_KEY")
        headers = {"Authorization": f"Bearer {api_key}"}

        files = {"file": ("audio.ogg", audio_data, "audio/ogg")}
        form_data = {"model": config.voice.stt.model}

        async with httpx.AsyncClient(verify=False) as http_client:  # noqa: S501
            response = await http_client.post(url, headers=headers, files=files, data=form_data)
            if response.status_code != 200:
                logger.error(f"STT API error: {response.status_code} - {response.text}")
                return None

            result = response.json()
            return result.get("text", "").strip()

    except Exception:
        logger.exception("Error transcribing audio")
        return None


async def _process_transcription(
    transcription: str,
    config: Config,
    *,
    available_agent_names: list[str] | None = None,
    available_team_names: list[str] | None = None,
) -> str:
    """Process transcription to recognize commands and agent names.

    Args:
        transcription: Raw transcription text
        config: Application configuration
        available_agent_names: Optional room-scoped list of available agent names
        available_team_names: Optional room-scoped list of available team names

    Returns:
        Formatted message with proper commands and mentions

    """
    try:
        # Get list of available agents and teams
        agent_names = available_agent_names if available_agent_names is not None else list(config.agents.keys())
        team_names = available_team_names if available_team_names is not None else list(config.teams.keys())

        agent_display_names = {name: config.agents[name].display_name for name in agent_names if name in config.agents}
        team_display_names = {name: config.teams[name].display_name for name in team_names if name in config.teams}

        agent_list = (
            "\n".join(
                [
                    f"  - @{name} or @{agent_username_localpart(name)} (spoken as: {agent_display_names[name]})"
                    for name in agent_names
                ],
            )
            if agent_names
            else "  (none)"
        )
        team_list = (
            "\n".join([f"  - @{name} (spoken as: {team_display_names[name]})" for name in team_names])
            if team_names
            else "  (none)"
        )

        # Build the prompt for the AI
        prompt = f"""You are a voice command processor for a Matrix chat bot system.
Your task is to lightly normalize spoken transcriptions while preserving user intent.

Available agents (use EXACT agent name after @):
{agent_list}

Available teams (use EXACT team name after @):
{team_list}

Examples of correct formatting:
- User says "HomeAssistant turn on the fan" → "@home turn on the fan"  (NOT @homeassistant)
- User says "schedule turn off the lights in 10 minutes" → "!schedule in 10 minutes turn off the lights"
- User says "hey home assistant agent schedule to turn off the guest room lights in 10 seconds" → "!schedule in 10 seconds @home turn off the guest room lights"
- User says "cancel schedule ABC123" → "!cancel_schedule ABC123"
- User says "list my schedules" → "!list_schedules"

{get_command_list()}

CRITICAL RULES:
1. ALWAYS use the EXACT agent name (the part before the parentheses) after @, NOT the display name
   - If agent is listed as "@home (spoken as: HomeAssistant)", use "@home" NOT "@homeassistant"
2. DEFAULT: keep natural language exactly as-is (except minor ASR fixes and mention normalization)
3. Only emit a !command when command intent is explicit and unambiguous
   - Explicit command intent examples: "schedule ...", "run skill ...", "cancel schedule ...", "help command"
   - Non-command examples that must stay natural language:
     - "What is my schedule today?" (question, not !list_schedules)
     - "How do agent sessions work?" (question, not !skill session list)
     - "Can you explain skills?" (question, not !skill)
4. If command intent is uncertain, DO NOT create any !command
5. !schedule commands MUST include a time (in X minutes, at 3pm, tomorrow, etc.)
   - The time should come right after !schedule
6. When both command AND agent are mentioned, command comes FIRST
7. Agent mentions come FIRST when just addressing them (no command):
   - "research agent, find papers" → "@research find papers"
   - "ask the email agent to check mail" → "@email check mail"
8. Fix common speech recognition errors (e.g., "at research" → "@research")
9. Be smart about intent - "ask the research agent" means "@research"
10. Keep the natural language but add proper formatting
11. ONLY mention agents/teams listed above as available in this room
12. If no relevant available agent/team is listed, do not add any @mention
13. Never invent command arguments that were not spoken

Transcription: "{transcription}"

Output the formatted message only, no explanation:"""

        # Get the AI model to process the transcription
        model = get_model_instance(config, config.voice.intelligence.model)

        # Create an agent for voice command processing
        agent = Agent(
            name="VoiceCommandProcessor",
            role="Normalize voice transcriptions while preserving command and mention intent",
            model=model,
        )

        # Process the transcription with the agent
        session_id = f"voice_process_{uuid.uuid4()}"
        response = await agent.arun(prompt, session_id=session_id)

        # Extract the content from the response
        if response and response.content:
            processed_message = _sanitize_unavailable_mentions(
                response.content.strip(),
                allowed_entities=set(agent_names) | set(team_names),
                configured_entities=set(config.agents) | set(config.teams),
            )
            if _is_speculative_command_rewrite(transcription, processed_message):
                return _sanitize_unavailable_mentions(
                    transcription.strip(),
                    allowed_entities=set(agent_names) | set(team_names),
                    configured_entities=set(config.agents) | set(config.teams),
                )
            return processed_message

    except Exception as e:
        logger.exception("Error processing transcription")
        # Return error message so user knows what happened
        from mindroom.error_handling import get_user_friendly_error_message  # noqa: PLC0415

        return get_user_friendly_error_message(e, "VoiceProcessor")
    else:
        # Return original transcription if no valid response from model
        return transcription


def _get_available_entities_for_sender(
    room: nio.MatrixRoom,
    sender_id: str,
    config: Config,
) -> tuple[list[str], list[str]]:
    """Return available agent and team names in this room for a specific sender."""
    available_agent_names: list[str] = []
    available_team_names: list[str] = []

    for matrix_id in get_available_agents_for_sender(room, sender_id, config):
        name = matrix_id.agent_name(config)
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
    configured_entities: set[str],
) -> str:
    """Strip @ from mentions that target configured but unavailable entities."""
    if not text:
        return text

    configured_by_lower = {name.lower(): name for name in configured_entities}
    allowed_lower = {name.lower() for name in allowed_entities}

    def _replace(match: re.Match[str]) -> str:
        name = match.group("name")
        configured_name = configured_by_lower.get(name.lower())
        if configured_name is None:
            return match.group(0)
        if configured_name.lower() in allowed_lower:
            return match.group(0)
        # Strip only '@', preserving exact matched token shape (mindroom_ prefix/domain suffix/case).
        return match.group(0)[1:]

    return _VOICE_MENTION_PATTERN.sub(_replace, text)


def _is_speculative_command_rewrite(transcription: str, formatted_message: str) -> bool:
    """Return True when model output invents a command not clearly requested by the user."""
    if not formatted_message:
        return False
    match = _VOICE_COMMAND_PATTERN.match(formatted_message.strip())
    if match is None:
        return False
    command_name = match.group("command").lower().replace("-", "_")
    normalized_transcription = transcription.strip().lower()
    if command_name == "skill":
        return _VOICE_SKILL_INTENT_PATTERN.search(normalized_transcription) is None
    if command_name == "help":
        return _VOICE_HELP_INTENT_PATTERN.search(normalized_transcription) is None
    return False
