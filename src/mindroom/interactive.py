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

    @staticmethod
    def should_create_interactive_question(response_text: str) -> bool:
        """Determine if the response warrants an interactive question.

        Args:
            response_text: The AI's response text

        Returns:
            True if an interactive question should be created
        """
        # Patterns that suggest interactive questions could be helpful
        interactive_patterns = [
            "would you prefer",
            "would you like me to",
            "should i",
            "which approach",
            "choose one",
            "select from",
            "option 1",
            "option a)",
            "alternatively",
            "do you want",
            "shall i continue",
        ]

        response_lower = response_text.lower()
        return any(pattern in response_lower for pattern in interactive_patterns)

    async def handle_interactive_response(self, room_id: str, thread_id: str | None, response_text: str) -> None:
        """Create an interactive question based on the AI response.

        Args:
            room_id: The room ID
            thread_id: Thread ID if in a thread
            response_text: The AI's response containing options
        """
        # Parse common patterns and create appropriate interactive questions
        response_lower = response_text.lower()

        # Send the original response first
        sender_domain = extract_domain_from_user_id(self.client.user_id)
        content = create_mention_content_from_text(
            response_text,
            sender_domain=sender_domain,
            thread_event_id=thread_id,
        )

        response = await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content=content,
        )

        if not isinstance(response, nio.RoomSendResponse):
            return

        # Now create interactive follow-up based on detected patterns
        if "shall i continue" in response_lower or "should i continue" in response_lower:
            await self.ask_interactive(
                room_id=room_id,
                thread_id=thread_id,
                question="Would you like me to continue?",
                options=[
                    ("✅", "Yes, continue", "continue"),
                    ("❌", "No, stop here", "stop"),
                    ("🤔", "Explain more", "explain"),
                ],
                handler=self._create_continue_handler(),
            )
        elif "would you prefer" in response_lower or "which approach" in response_lower:
            # For more complex choices, look for numbered options or bullets
            if "option 1" in response_lower or "1)" in response_lower or "1." in response_lower:
                await self._create_numbered_options_question(room_id, thread_id, response_text)
        elif "should i include" in response_lower:
            await self._create_yes_no_question(room_id, thread_id, response_text)

    async def _create_numbered_options_question(self, room_id: str, thread_id: str | None, response_text: str) -> None:
        """Create an interactive question for numbered options."""
        # Simple pattern matching for common formats
        options = []

        if "fast" in response_text.lower() and "readable" in response_text.lower():
            options = [
                ("🚀", "Fast/Performance", "fast"),
                ("📚", "Readable/Simple", "readable"),
                ("⚖️", "Balanced", "balanced"),
            ]
        else:
            # Generic numbered options
            options = [
                ("1️⃣", "Option 1", "option1"),
                ("2️⃣", "Option 2", "option2"),
                ("3️⃣", "Option 3", "option3"),
            ]

        await self.ask_interactive(
            room_id=room_id,
            thread_id=thread_id,
            question="Which option would you prefer?",
            options=options,
            handler=self._create_option_handler(),
        )

    async def _create_yes_no_question(self, room_id: str, thread_id: str | None, response_text: str) -> None:
        """Create a yes/no interactive question."""
        # Extract what we're asking about
        question = "Should I proceed with this?"
        if "error handling" in response_text.lower():
            question = "Should I include error handling?"
        elif "tests" in response_text.lower():
            question = "Should I include tests?"

        await self.ask_interactive(
            room_id=room_id,
            thread_id=thread_id,
            question=question,
            options=[
                ("✅", "Yes", "yes"),
                ("❌", "No", "no"),
                ("🤔", "Tell me more", "explain"),
            ],
            handler=self._create_yes_no_handler(),
        )

    def _create_continue_handler(
        self,
    ) -> Callable[[str, str, str, str | None], Coroutine[Any, Any, None]]:
        """Create a handler for continue/stop choices."""

        async def handler(room_id: str, user_id: str, choice: str, thread_id: str | None) -> None:
            if choice == "continue":
                response = "Continuing with the implementation..."
            elif choice == "stop":
                response = "Understood, I'll stop here."
            else:  # explain
                response = "Let me explain what I would do next..."

            sender_domain = extract_domain_from_user_id(self.client.user_id)
            content = create_mention_content_from_text(
                response,
                sender_domain=sender_domain,
                thread_event_id=thread_id,
            )

            await self.client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content,
            )

        return handler

    def _create_option_handler(
        self,
    ) -> Callable[[str, str, str, str | None], Coroutine[Any, Any, None]]:
        """Create a handler for option selection."""

        async def handler(room_id: str, user_id: str, choice: str, thread_id: str | None) -> None:
            responses = {
                "fast": "Great! I'll implement the performance-optimized solution.",
                "readable": "Perfect! I'll focus on clarity and simplicity.",
                "balanced": "Good choice! I'll balance performance with readability.",
                "option1": "Proceeding with option 1...",
                "option2": "Proceeding with option 2...",
                "option3": "Proceeding with option 3...",
            }

            response = responses.get(choice, f"Proceeding with your selection: {choice}")

            sender_domain = extract_domain_from_user_id(self.client.user_id)
            content = create_mention_content_from_text(
                response,
                sender_domain=sender_domain,
                thread_event_id=thread_id,
            )

            await self.client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content,
            )

        return handler

    def _create_yes_no_handler(
        self,
    ) -> Callable[[str, str, str, str | None], Coroutine[Any, Any, None]]:
        """Create a handler for yes/no choices."""

        async def handler(room_id: str, user_id: str, choice: str, thread_id: str | None) -> None:
            if choice == "yes":
                response = "Great! I'll include that in the implementation."
            elif choice == "no":
                response = "Understood, I'll skip that part."
            else:  # explain
                response = "Let me explain the trade-offs..."

            sender_domain = extract_domain_from_user_id(self.client.user_id)
            content = create_mention_content_from_text(
                response,
                sender_domain=sender_domain,
                thread_event_id=thread_id,
            )

            await self.client.room_send(
                room_id=room_id,
                message_type="m.room.message",
                content=content,
            )

        return handler
