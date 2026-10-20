"""Microsoft Teams channel and chat tools that act as the requester through Microsoft Graph."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any

from agno.tools import Toolkit

from mindroom.custom_tools.microsoft_graph import (
    MicrosoftGraphError,
    graph_error_payload,
    graph_session,
    graph_tool_result,
    path_segment,
)
from mindroom.oauth.microsoft import microsoft_teams_oauth_provider

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.custom_tools.microsoft_graph import GraphSession
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

FUNCTION_NAMES = (
    "microsoft_teams_list_teams",
    "microsoft_teams_list_channels",
    "microsoft_teams_list_channel_messages",
    "microsoft_teams_list_chats",
    "microsoft_teams_list_chat_messages",
    "microsoft_teams_send_channel_message",
    "microsoft_teams_reply_to_channel_message",
    "microsoft_teams_send_chat_message",
)
_WRITE_FUNCTIONS = frozenset(
    {
        "microsoft_teams_send_channel_message",
        "microsoft_teams_reply_to_channel_message",
        "microsoft_teams_send_chat_message",
    },
)
# Graph pages channel and chat messages, and chats, at most 50 at a time.
_MAX_LIMIT = 50
# Message bodies come back into the conversation, so one message stays short.
_MAX_BODY_CHARACTERS = 4_000
# Teams refuses a post over about 28 KB, counting its content and markup.
_MAX_MESSAGE_BYTES = 28_000
_TOOL_NAME = "microsoft_teams"
_CHANNEL_SELECT = "id,displayName,description,membershipType,webUrl"
# Tags that start a new line in the plain-text rendering of an HTML message.
_LINE_TAGS = frozenset(
    {"br", "p", "div", "li", "tr", "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table"},
)


@dataclass(frozen=True)
class _Request:
    """One validated Graph request."""

    method: str
    path: str
    params: dict[str, str] | None = None
    body: dict[str, object] | None = None

    def send(self, session: GraphSession) -> dict[str, Any]:
        return session.json(self.method, self.path, params=self.params, json=self.body)


class _PlainText(HTMLParser):
    """Collect an HTML message's text, one line per block, with images shown as [image]."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:  # noqa: ARG002
        if tag in _LINE_TAGS:
            self.parts.append("\n")
        elif tag == "img":
            self.parts.append("[image]")

    def handle_endtag(self, tag: str) -> None:
        if tag in _LINE_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(html: str) -> str:
    parser = _PlainText()
    parser.feed(html)
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def _team_path(team_id: object) -> str:
    return f"/teams/{path_segment(team_id, 'team_id')}"


def _channel_path(team_id: object, channel_id: object) -> str:
    return f"{_team_path(team_id)}/channels/{path_segment(channel_id, 'channel_id')}"


def _chat_path(chat_id: object) -> str:
    return f"/chats/{path_segment(chat_id, 'chat_id')}"


def _top(limit: object) -> str:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_LIMIT:
        msg = f"limit must be an integer from 1 to {_MAX_LIMIT}."
        raise MicrosoftGraphError(code="invalid_argument", message=msg)
    return str(limit)


def _text_body(text: object) -> dict[str, object]:
    """Return a plain-text message body, so model text is never rendered as HTML."""
    if not isinstance(text, str) or not text.strip():
        raise MicrosoftGraphError(code="invalid_argument", message="text must not be empty.")
    size = len(text.encode("utf-8"))
    if size > _MAX_MESSAGE_BYTES:
        msg = f"text is {size} bytes in UTF-8; a Teams message holds at most {_MAX_MESSAGE_BYTES}."
        raise MicrosoftGraphError(code="too_large", message=msg)
    return {"body": {"contentType": "text", "content": text}}


def _entries(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [entry for entry in payload.get("value") or [] if isinstance(entry, dict)]


def _sender(message: Mapping[str, Any]) -> str | None:
    """Return the display name of the person or app that sent a message."""
    sender = message.get("from")
    if not isinstance(sender, dict):
        return None
    for kind in ("user", "application"):
        identity = sender.get(kind)
        if isinstance(identity, dict) and isinstance(identity.get("displayName"), str):
            return identity["displayName"]
    return None


def _body_text(message: Mapping[str, Any]) -> str:
    """Return a message's body as plain text; Graph sends most bodies as HTML."""
    body = message.get("body")
    if not isinstance(body, dict) or not isinstance(content := body.get("content"), str):
        return ""
    return _plain_text(content) if str(body.get("contentType", "")).casefold() == "html" else content


def _message(message: Mapping[str, Any]) -> dict[str, Any]:
    text = _body_text(message)
    attachments = message.get("attachments")
    return {
        "id": message.get("id"),
        "created": message.get("createdDateTime"),
        "from": _sender(message),
        "body": text[:_MAX_BODY_CHARACTERS],
        "truncated": len(text) > _MAX_BODY_CHARACTERS,
        "reply_to_id": message.get("replyToId"),
        "web_url": message.get("webUrl"),
        "attachment_count": len(attachments) if isinstance(attachments, list) else 0,
    }


def _message_page(payload: Mapping[str, Any]) -> dict[str, object]:
    return {
        "messages": [_message(message) for message in _entries(payload)],
        "more_available": "@odata.nextLink" in payload,
    }


def _chat(chat: Mapping[str, Any]) -> dict[str, Any]:
    members = chat.get("members")
    return {
        "id": chat.get("id"),
        "topic": chat.get("topic"),
        "chat_type": chat.get("chatType"),
        "members": [
            member["displayName"]
            for member in (members if isinstance(members, list) else [])
            if isinstance(member, dict) and isinstance(member.get("displayName"), str) and member["displayName"]
        ],
        "last_updated": chat.get("lastUpdatedDateTime"),
    }


class MicrosoftTeamsTools(Toolkit):
    """Read and send Teams channel and chat messages as the requester."""

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        runtime_config: Config | None = None,
        write: bool = True,
    ) -> None:
        if credentials_manager is None:
            msg = "MicrosoftTeamsTools requires an explicit credentials_manager"
            raise RuntimeError(msg)
        self._provider = microsoft_teams_oauth_provider()
        self._runtime_paths = runtime_paths
        self._credentials_manager = credentials_manager
        self._worker_target = worker_target
        self._config = runtime_config
        functions: dict[str, Callable[..., str]] = {
            "microsoft_teams_list_teams": self.microsoft_teams_list_teams,
            "microsoft_teams_list_channels": self.microsoft_teams_list_channels,
            "microsoft_teams_list_channel_messages": self.microsoft_teams_list_channel_messages,
            "microsoft_teams_list_chats": self.microsoft_teams_list_chats,
            "microsoft_teams_list_chat_messages": self.microsoft_teams_list_chat_messages,
            "microsoft_teams_send_channel_message": self.microsoft_teams_send_channel_message,
            "microsoft_teams_reply_to_channel_message": self.microsoft_teams_reply_to_channel_message,
            "microsoft_teams_send_chat_message": self.microsoft_teams_send_chat_message,
        }
        super().__init__(
            name=_TOOL_NAME,
            tools=[function for name, function in functions.items() if write or name not in _WRITE_FUNCTIONS],
        )

    def _call(
        self,
        request: Callable[[], _Request],
        shape: Callable[[dict[str, Any]], Mapping[str, object]],
    ) -> str:
        """Validate the arguments into one request before any network call, then send it and shape the answer."""
        try:
            prepared = request()
        except MicrosoftGraphError as exc:
            return graph_error_payload(_TOOL_NAME, exc)

        def run() -> Mapping[str, object]:
            with graph_session(
                self._provider,
                self._runtime_paths,
                self._credentials_manager,
                self._worker_target,
                self._config,
            ) as session:
                return shape(prepared.send(session))

        return graph_tool_result(_TOOL_NAME, run)

    def microsoft_teams_list_teams(self) -> str:
        """List the Teams teams the requester is a member of, with the team_id the channel functions take."""
        return self._call(
            lambda: _Request("GET", "/me/joinedTeams"),
            lambda payload: {
                "teams": [
                    {"id": team.get("id"), "name": team.get("displayName"), "description": team.get("description")}
                    for team in _entries(payload)
                ],
            },
        )

    def microsoft_teams_list_channels(self, team_id: str) -> str:
        """List the channels the requester can see in one team, with the channel_id the message functions take.

        Args:
            team_id: The team's id from microsoft_teams_list_teams.

        """
        return self._call(
            lambda: _Request("GET", f"{_team_path(team_id)}/channels", {"$select": _CHANNEL_SELECT}),
            lambda payload: {
                "channels": [
                    {
                        "id": channel.get("id"),
                        "name": channel.get("displayName"),
                        "description": channel.get("description"),
                        "membership_type": channel.get("membershipType"),
                        "web_url": channel.get("webUrl"),
                    }
                    for channel in _entries(payload)
                ],
                "more_available": "@odata.nextLink" in payload,
            },
        )

    def microsoft_teams_list_channel_messages(self, team_id: str, channel_id: str, limit: int = 20) -> str:
        """List the latest top-level posts in one Teams channel as plain text, without their replies.

        Posts come newest activity first: a post with a new reply counts as recent. Each body holds at most
        4,000 characters and reports truncated when it was cut.

        Args:
            team_id: The team's id from microsoft_teams_list_teams.
            channel_id: The channel's id from microsoft_teams_list_channels.
            limit: Maximum number of posts to return, from 1 to 50.

        """
        return self._call(
            lambda: _Request("GET", f"{_channel_path(team_id, channel_id)}/messages", {"$top": _top(limit)}),
            _message_page,
        )

    def microsoft_teams_list_chats(self, limit: int = 20) -> str:
        """List the requester's Teams chats, one-on-one, group, and meeting, with their members' names.

        Each chat names at most 25 members.

        Args:
            limit: Maximum number of chats to return, from 1 to 50.

        """
        return self._call(
            lambda: _Request("GET", "/me/chats", {"$expand": "members", "$top": _top(limit)}),
            lambda payload: {
                "chats": [_chat(chat) for chat in _entries(payload)],
                "more_available": "@odata.nextLink" in payload,
            },
        )

    def microsoft_teams_list_chat_messages(self, chat_id: str, limit: int = 20) -> str:
        """List the latest messages in one Teams chat as plain text, newest first.

        Each body holds at most 4,000 characters and reports truncated when it was cut.

        Args:
            chat_id: The chat's id from microsoft_teams_list_chats.
            limit: Maximum number of messages to return, from 1 to 50.

        """
        return self._call(
            lambda: _Request(
                "GET",
                f"{_chat_path(chat_id)}/messages",
                {"$top": _top(limit), "$orderby": "createdDateTime desc"},
            ),
            _message_page,
        )

    def microsoft_teams_send_channel_message(self, team_id: str, channel_id: str, text: str) -> str:
        """Post a new message in one Teams channel, as the requester and visible to everyone in the channel.

        Before calling, tell the requester the team and channel names and the exact text, because an approval
        prompt may show only IDs. The text is sent as plain text, up to 28,000 bytes.

        Args:
            team_id: The team's id from microsoft_teams_list_teams.
            channel_id: The channel's id from microsoft_teams_list_channels.
            text: The message text.

        """
        return self._call(
            lambda: _Request("POST", f"{_channel_path(team_id, channel_id)}/messages", body=_text_body(text)),
            lambda payload: {"message": _message(payload)},
        )

    def microsoft_teams_reply_to_channel_message(
        self,
        team_id: str,
        channel_id: str,
        message_id: str,
        text: str,
    ) -> str:
        """Reply in the thread of one Teams channel post, as the requester and visible to everyone in the channel.

        Before calling, tell the requester the team and channel names, which post the reply answers, and the exact
        text, because an approval prompt may show only IDs. The text is sent as plain text, up to 28,000 bytes.

        Args:
            team_id: The team's id from microsoft_teams_list_teams.
            channel_id: The channel's id from microsoft_teams_list_channels.
            message_id: The top-level post's id from microsoft_teams_list_channel_messages.
            text: The reply text.

        """
        return self._call(
            lambda: _Request(
                "POST",
                f"{_channel_path(team_id, channel_id)}/messages/{path_segment(message_id, 'message_id')}/replies",
                body=_text_body(text),
            ),
            lambda payload: {"message": _message(payload)},
        )

    def microsoft_teams_send_chat_message(self, chat_id: str, text: str) -> str:
        """Send a message in one existing Teams chat, as the requester and visible to every chat member.

        Before calling, tell the requester the chat's topic or members and the exact text, because an approval
        prompt may show only IDs. The text is sent as plain text, up to 28,000 bytes.

        Args:
            chat_id: The chat's id from microsoft_teams_list_chats.
            text: The message text.

        """
        return self._call(
            lambda: _Request("POST", f"{_chat_path(chat_id)}/messages", body=_text_body(text)),
            lambda payload: {"message": _message(payload)},
        )
