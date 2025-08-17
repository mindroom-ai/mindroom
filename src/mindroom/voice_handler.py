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
from .logging_config import get_logger
from .matrix.client import send_message
from .matrix.mentions import create_mention_content_from_text

if TYPE_CHECKING:
    from .config import Config

logger = get_logger(__name__)


async def handle_voice_message(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,
    event: nio.RoomMessageAudio | nio.RoomEncryptedAudio,
    config: Config,
) -> None:
    """Handle a voice message event.

    Args:
        client: Matrix client
        room: Matrix room
        event: Voice message event
        config: Application configuration

    """
    if not config.voice.enabled:
        return

    try:
        # Download the audio file
        audio_data = await _download_audio(client, event)
        if not audio_data:
            logger.error("Failed to download audio file")
            return

        # Transcribe the audio
        transcription = await _transcribe_audio(audio_data, config)
        if not transcription:
            logger.warning("Failed to transcribe audio or empty transcription")
            return

        logger.info(f"Raw transcription: {transcription}")

        # Process transcription with AI for command/agent recognition
        formatted_message = await _process_transcription(transcription, config)

        logger.info(f"Formatted message: {formatted_message}")

        # Send the formatted message as a text message from the bot
        # This will trigger the normal message processing flow
        if formatted_message:
            # Add a note that this was transcribed from voice
            final_message = f"🎤 {formatted_message}"

            # Create mention content if there are mentions
            content = create_mention_content_from_text(
                config,
                final_message,
                reply_to_event_id=event.event_id,
            )

            # Send as a reply to the original voice message (reply already set in content)
            await send_message(client, room.room_id, content)

    except Exception:
        logger.exception("Error handling voice message")


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
        if isinstance(event, nio.RoomMessageAudio):
            # Unencrypted audio
            mxc = event.url
            response = await client.download(mxc)
            if isinstance(response, nio.DownloadError):
                logger.error(f"Download failed: {response}")
                return None
            return response.body

        if isinstance(event, nio.RoomEncryptedAudio):
            # Encrypted audio
            mxc = event.url
            response = await client.download(mxc)
            if isinstance(response, nio.DownloadError):
                logger.error(f"Download failed: {response}")
                return None

            # Decrypt the audio
            return crypto.attachments.decrypt_attachment(
                response.body,
                event.source["content"]["file"]["key"]["k"],
                event.source["content"]["file"]["hashes"]["sha256"],
                event.source["content"]["file"]["iv"],
            )

    except Exception:
        logger.exception("Error downloading audio")
        return None
    else:
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
            headers = {
                "Authorization": f"Bearer {api_key}",
            }

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
                return result.get("text", "").strip()

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

Available agents: {", ".join([f"@{name} ({agent_display_names[name]})" for name in agent_names])}
Available teams: {", ".join([f"@{name} ({team_display_names[name]})" for name in team_names])}

Available commands:
- !invite <agent> - Invite an agent to the current thread
- !uninvite <agent> - Remove an agent from the thread
- !list_invites - Show all invited agents
- !schedule <task> - Schedule a task
- !list_schedules - List scheduled tasks
- !cancel_schedule <id> - Cancel a scheduled task
- !help [topic] - Get help
- !widget [url] - Add configuration widget

Rules:
1. If the user mentions an agent by name or role, format it as @agent_name
2. If the user speaks a command, format it as !command
3. Fix common speech recognition errors (e.g., "at research" -> "@research")
4. Be smart about intent - "ask the research agent" means "@research"
5. "Schedule a meeting" should become "!schedule meeting"
6. Keep the natural language but add proper formatting
7. If unclear, prefer natural language over forcing commands

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
            return response.content.strip()

    except Exception:
        logger.exception("Error processing transcription")
        # Return original transcription as fallback
        return transcription
    else:
        # Return original transcription if no valid response from model
        return transcription
