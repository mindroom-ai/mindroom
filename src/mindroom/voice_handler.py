"""Voice message handler with speech-to-text and intelligent command recognition."""

from __future__ import annotations

import os
import ssl
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import aiofiles
import aiohttp
import nio
from agno.agent import Agent
from nio import crypto

from .ai import get_model_instance
from .commands import get_command_list
from .constants import VOICE_PREFIX
from .logging_config import get_logger

if TYPE_CHECKING:
    from .config import Config

logger = get_logger(__name__)


async def handle_voice_message(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,  # noqa: ARG001
    event: nio.RoomMessageAudio | nio.RoomEncryptedAudio,
    config: Config,
) -> str | None:
    """Handle a voice message event.

    Args:
        client: Matrix client
        room: Matrix room
        event: Voice message event
        config: Application configuration

    Returns:
        The transcribed and formatted message, or None if transcription failed

    """
    if not config.voice.enabled:
        return None

    try:
        # Download the audio file
        audio_data = await _download_audio(client, event)
        if not audio_data:
            logger.error("Failed to download audio file")
            return None

        # Transcribe the audio
        transcription = await _transcribe_audio(audio_data, config)
        if not transcription:
            logger.warning("Failed to transcribe audio or empty transcription")
            return None

        logger.info(f"Raw transcription: {transcription}")

        # Process transcription with AI for command/agent recognition
        formatted_message = await _process_transcription(transcription, config)

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
) -> bytes | None:
    """Download and decrypt audio file from Matrix.

    Args:
        client: Matrix client
        event: Audio event

    Returns:
        Audio file bytes or None if failed

    """
    try:
        # Unencrypted audio
        mxc = event.url
        response = await client.download(mxc)
        if isinstance(response, nio.DownloadError):
            logger.error(f"Download failed: {response}")
            return None
        if isinstance(event, nio.RoomMessageAudio):
            return response.body  # type: ignore[no-any-return]

        assert isinstance(event, nio.RoomEncryptedAudio)
        # Decrypt the audio
        return crypto.attachments.decrypt_attachment(  # type: ignore[no-any-return]
            response.body,
            event.source["content"]["file"]["key"]["k"],
            event.source["content"]["file"]["hashes"]["sha256"],
            event.source["content"]["file"]["iv"],
        )

    except Exception:
        logger.exception("Error downloading audio")
    return None


async def _transcribe_audio(audio_data: bytes, config: Config) -> str | None:
    """Transcribe audio using OpenAI-compatible API.

    Args:
        audio_data: Audio file bytes
        config: Application configuration

    Returns:
        Transcription text or None if failed

    """
    try:
        # Save audio to temporary file (required by most STT APIs)
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp_file:
            tmp_file.write(audio_data)
            tmp_path = tmp_file.name

        try:
            # Use OpenAI-compatible API for transcription
            stt_host = config.voice.stt.host
            if stt_host:
                # Self-hosted solution
                url = f"{stt_host}/v1/audio/transcriptions"
            else:
                # OpenAI or compatible cloud service
                url = "https://api.openai.com/v1/audio/transcriptions"

            api_key = config.voice.stt.api_key or os.getenv("OPENAI_API_KEY")
            headers = {"Authorization": f"Bearer {api_key}"}

            # Prepare multipart form data
            async with aiofiles.open(tmp_path, "rb") as audio_file:
                audio_content = await audio_file.read()

            data = aiohttp.FormData()
            data.add_field("file", audio_content, filename="audio.ogg", content_type="audio/ogg")
            data.add_field("model", config.voice.stt.model)

            # Make the API request (with SSL verification disabled if needed)
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with (
                aiohttp.ClientSession(connector=connector) as session,
                session.post(url, headers=headers, data=data) as response,
            ):
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"STT API error: {response.status} - {error_text}")
                    return None

                result = await response.json()
                return result.get("text", "").strip()  # type: ignore[no-any-return]

        finally:
            # Clean up temporary file
            Path(tmp_path).unlink()

    except Exception:
        logger.exception("Error transcribing audio")
        return None


async def _process_transcription(transcription: str, config: Config) -> str:
    """Process transcription to recognize commands and agent names.

    Args:
        transcription: Raw transcription text
        config: Application configuration

    Returns:
        Formatted message with proper commands and mentions

    """
    try:
        # Get list of available agents and teams
        agent_names = list(config.agents.keys())
        agent_display_names = {name: cfg.display_name for name, cfg in config.agents.items()}

        team_names = list(config.teams.keys()) if config.teams else []
        team_display_names = {name: cfg.display_name for name, cfg in config.teams.items()} if config.teams else {}

        # Build the prompt for the AI
        prompt = f"""You are a voice command processor for a Matrix chat bot system.
Your task is to convert spoken transcriptions into properly formatted chat commands.

Available agents (use EXACT agent name after @):
{chr(10).join([f"  - @{name} or @mindroom_{name} (spoken as: {agent_display_names[name]})" for name in agent_names])}

Available teams (use EXACT team name after @):
{chr(10).join([f"  - @{name} (spoken as: {team_display_names[name]})" for name in team_names]) if team_names else "  (none)"}

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
2. If the user speaks a command, format it as !command
3. !schedule commands MUST include a time (in X minutes, at 3pm, tomorrow, etc.)
   - The time should come right after !schedule
4. When both command AND agent are mentioned, command comes FIRST
5. Agent mentions come FIRST when just addressing them (no command):
   - "research agent, find papers" → "@research find papers"
   - "ask the email agent to check mail" → "@email check mail"
6. Fix common speech recognition errors (e.g., "at research" → "@research")
7. Be smart about intent - "ask the research agent" means "@research"
8. Keep the natural language but add proper formatting
9. If unclear, prefer natural language over forcing commands

Transcription: "{transcription}"

Output the formatted message only, no explanation:"""

        # Get the AI model to process the transcription
        model = get_model_instance(config, config.voice.intelligence.model)

        # Create an agent for voice command processing
        agent = Agent(
            name="VoiceCommandProcessor",
            role="Convert voice transcriptions to properly formatted chat commands",
            model=model,
        )

        # Process the transcription with the agent
        session_id = f"voice_process_{uuid.uuid4()}"
        response = await agent.arun(prompt, session_id=session_id)

        # Extract the content from the response
        if response and response.content:
            return response.content.strip()  # type: ignore[no-any-return]

    except Exception as e:
        logger.exception("Error processing transcription")
        # Return error message so user knows what happened
        from .error_handling import get_user_friendly_error_message  # noqa: PLC0415

        return get_user_friendly_error_message(e, "VoiceProcessor")
    else:
        # Return original transcription if no valid response from model
        return transcription
