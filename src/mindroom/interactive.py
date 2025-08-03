"""Interactive Q&A system using Matrix reactions as clickable buttons."""

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import nio

from .logging_config import emoji, get_logger
from .matrix import create_mention_content_from_text, extract_domain_from_user_id

logger = get_logger(__name__)


@dataclass
class InteractiveQuestion:
    """Tracks an active interactive question with reaction-based answers."""

    event_id: str  # The message event ID containing the question
    room_id: str
    thread_id: str | None
    question: str
    options: dict[str, str]  # Maps emoji/text to value (e.g., {"🚀": "fast", "1": "fast"})
    handler: Callable[[str, str, str, str | None], Coroutine[Any, Any, None]]  # (room_id, user_id, value, thread_id)
    expires_at: datetime
    bot_reaction_ids: dict[str, str] = field(default_factory=dict)  # Maps emoji to bot's reaction event IDs
    responded_users: set[str] = field(default_factory=set)  # Track users who already responded


class InteractiveManager:
    """Manages interactive questions and handles user reactions as answers."""

    def __init__(self, client: nio.AsyncClient, agent_name: str):
        self.client = client
        self.agent_name = agent_name
        self.active_questions: dict[str, InteractiveQuestion] = {}
        self.logger = logger.bind(agent=f"{emoji(agent_name)} {agent_name}")

        # Start cleanup task
        asyncio.create_task(self._periodic_cleanup())

    async def ask_interactive(
        self,
        room_id: str,
        thread_id: str | None,
        question: str,
        options: list[tuple[str, str, str]],  # List of (emoji, label, value)
        handler: Callable[[str, str, str, str | None], Coroutine[Any, Any, None]],
        timeout_minutes: int = 30,
    ) -> str | None:
        """Send an interactive question with reaction options.

        Args:
            room_id: The room to send the question in
            thread_id: Thread ID if in a thread
            question: The question text
            options: List of (emoji, label, value) tuples for options
            handler: Async function to handle responses - receives (room_id, user_id, value, thread_id)
            timeout_minutes: How long the question remains active

        Returns:
            Event ID of the question message, or None if failed
        """
        if len(options) > 5:
            self.logger.warning("Too many options for interactive question", count=len(options))
            options = options[:5]

        # Build the message content
        message_lines = [question, ""]
        option_map = {}

        for i, (emoji_char, label, value) in enumerate(options, 1):
            message_lines.append(f"{emoji_char} {label}")
            # Support both emoji and numeric responses
            option_map[emoji_char] = value
            option_map[str(i)] = value

        message_lines.extend(["", "React with an emoji or type the number to respond."])
        message_text = "\n".join(message_lines)

        # Send the message
        sender_domain = extract_domain_from_user_id(self.client.user_id)
        content = create_mention_content_from_text(
            message_text,
            sender_domain=sender_domain,
            thread_event_id=thread_id,
        )

        response = await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )

        if not isinstance(response, nio.RoomSendResponse):
            self.logger.error("Failed to send interactive question", error=str(response))
            return None

        question_event_id = response.event_id

        # Store the question
        self.active_questions[question_event_id] = InteractiveQuestion(
            event_id=question_event_id,
            room_id=room_id,
            thread_id=thread_id,
            question=question,
            options=option_map,
            handler=handler,
            expires_at=datetime.now() + timedelta(minutes=timeout_minutes),
        )

        # Add reaction buttons
        bot_reactions = {}
        for emoji_char, _, _ in options:
            reaction_response = await self.client.room_send(
                room_id=room_id,
                message_type="m.reaction",
                content={
                    "m.relates_to": {
                        "rel_type": "m.annotation",
                        "event_id": question_event_id,
                        "key": emoji_char,
                    }
                },
            )
            if isinstance(reaction_response, nio.RoomSendResponse):
                bot_reactions[emoji_char] = reaction_response.event_id
            else:
                self.logger.warning("Failed to add reaction", emoji=emoji_char, error=str(reaction_response))

        self.active_questions[question_event_id].bot_reaction_ids = bot_reactions

        self.logger.info(
            "Sent interactive question",
            event_id=question_event_id,
            options=len(options),
        )

        return str(question_event_id)

    async def on_reaction(self, room: nio.MatrixRoom, event: nio.ReactionEvent) -> None:
        """Handle a reaction event that might be an answer to a question.

        Args:
            room: The room the reaction occurred in
            event: The reaction event
        """
        # Check if this reaction relates to an active question
        question = self.active_questions.get(event.reacts_to)
        if not question:
            return

        # Check if it's expired
        if datetime.now() > question.expires_at:
            await self._cleanup_question(question)
            return

        # Check if this user already responded
        if event.sender in question.responded_users:
            self.logger.debug("User already responded to question", user=event.sender)
            return

        # Check if the reaction is one of our options
        reaction_key = event.key
        if reaction_key not in question.options:
            return

        # Don't process our own reactions
        if event.sender == self.client.user_id:
            return

        # Mark user as having responded
        question.responded_users.add(event.sender)

        # Get the value for this reaction
        selected_value = question.options[reaction_key]

        self.logger.info(
            "Received answer via reaction",
            user=event.sender,
            reaction=reaction_key,
            value=selected_value,
        )

        # Call the handler
        try:
            await question.handler(question.room_id, event.sender, selected_value, question.thread_id)
        except Exception as e:
            self.logger.error("Error in reaction handler", error=str(e))

        # Send confirmation
        await self._send_confirmation(question, event.sender, reaction_key)

    async def on_text_response(self, room: nio.MatrixRoom, event: nio.RoomMessageText) -> None:
        """Handle text responses to interactive questions (e.g., "1", "2", "3").

        Args:
            room: The room the message occurred in
            event: The message event
        """
        # Check if this might be a response to a question
        message_text = event.body.strip()

        # Look for numeric responses
        if not message_text.isdigit() or len(message_text) > 1:
            return

        # Find recent questions in this room/thread
        # Extract thread info from the event
        is_thread = "m.relates_to" in event.source.get("content", {})
        thread_id = None
        if is_thread:
            relates_to = event.source["content"]["m.relates_to"]
            if relates_to.get("rel_type") == "m.thread":
                thread_id = relates_to.get("event_id")

        # Find matching active questions
        for question in self.active_questions.values():
            if question.room_id != room.room_id:
                continue
            if question.thread_id != thread_id:
                continue
            if datetime.now() > question.expires_at:
                continue
            if event.sender in question.responded_users:
                continue
            if message_text not in question.options:
                continue

            # Found a matching question
            question.responded_users.add(event.sender)
            selected_value = question.options[message_text]

            self.logger.info(
                "Received answer via text",
                user=event.sender,
                text=message_text,
                value=selected_value,
            )

            # Call the handler
            try:
                await question.handler(question.room_id, event.sender, selected_value, question.thread_id)
            except Exception as e:
                self.logger.error("Error in text response handler", error=str(e))

            # Send confirmation
            await self._send_confirmation(question, event.sender, message_text)
            break

    async def _send_confirmation(self, question: InteractiveQuestion, user_id: str, response: str) -> None:
        """Send a confirmation message after a user responds."""
        confirmation = f"✅ Received your response: {response}"

        sender_domain = extract_domain_from_user_id(self.client.user_id)
        content = create_mention_content_from_text(
            confirmation,
            sender_domain=sender_domain,
            thread_event_id=question.thread_id,
            reply_to_event_id=question.event_id,
        )

        await self.client.room_send(
            room_id=question.room_id,
            message_type="m.room.message",
            content=content,
        )

    async def _cleanup_question(self, question: InteractiveQuestion) -> None:
        """Clean up an expired or completed question."""
        # Remove bot's reactions
        for emoji_char, reaction_id in question.bot_reaction_ids.items():
            try:
                await self.client.room_redact(
                    room_id=question.room_id,
                    event_id=reaction_id,
                    reason="Interactive question expired",
                )
            except Exception as e:
                self.logger.warning("Failed to remove reaction", emoji=emoji_char, error=str(e))

        # Remove from active questions
        self.active_questions.pop(question.event_id, None)

        self.logger.debug("Cleaned up interactive question", event_id=question.event_id)

    async def _periodic_cleanup(self) -> None:
        """Periodically clean up expired questions."""
        while True:
            try:
                await asyncio.sleep(300)  # Check every 5 minutes

                now = datetime.now()
                expired_questions = [q for q in self.active_questions.values() if now > q.expires_at]

                for question in expired_questions:
                    await self._cleanup_question(question)

                if expired_questions:
                    self.logger.info("Cleaned up expired questions", count=len(expired_questions))

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error("Error in periodic cleanup", error=str(e))

    def cleanup(self) -> None:
        """Clean up when shutting down."""
        # Cancel any pending cleanups
        self.active_questions.clear()
