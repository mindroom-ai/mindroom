"""Zep conversational memory and knowledge graph toolkit."""

from __future__ import annotations

import contextlib
import uuid
from typing import Any, Literal

from mindroom.failure_text import failure_text
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tools.native_helpers import UNSET, UnsetType, require_non_empty, resolve_toggle

_ReadAction = Literal["context", "messages", "search"]
_SearchScope = Literal["edges", "nodes"]
_MessageRole = Literal["user", "assistant", "system", "function", "tool"]


class ZepTools(Toolkit):
    """Store conversation messages and retrieve or search Zep memory."""

    def __init__(
        self,
        session_id: str | None = None,
        user_id: str | None = None,
        api_key: str | None = None,
        ignore_assistant_messages: bool = False,
        enable_add_zep_message: bool | None | UnsetType = UNSET,
        enable_get_zep_memory: bool | None | UnsetType = UNSET,
        enable_search_zep_memory: bool | None | UnsetType = UNSET,
        instructions: str | None = None,
        all: bool | None | UnsetType = UNSET,  # noqa: A002
    ) -> None:
        self._api_key = require_non_empty(api_key, "Zep API key")
        self.session_id_provided = session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
        self.user_id_provided = user_id.strip() if isinstance(user_id, str) and user_id.strip() else None
        self.ignore_assistant_messages = ignore_assistant_messages
        self.zep_client: Any = None
        self.session_id: str | None = None
        self.user_id: str | None = None
        self._initialized = False

        enable_all = resolve_toggle(all, default=False)
        self._allow_add = resolve_toggle(enable_add_zep_message, default=True, enable_all=enable_all)
        self._allow_get = resolve_toggle(enable_get_zep_memory, default=True, enable_all=enable_all)
        self._allow_search = resolve_toggle(enable_search_zep_memory, default=True, enable_all=enable_all)

        tools: list[Any] = []
        if self._allow_get or self._allow_search:
            tools.append(self.zep_read)
        if self._allow_add:
            tools.append(self.zep_add_message)
        super().__init__(name="zep", tools=tools, instructions=instructions)

    def initialize(self) -> bool:
        """Lazily initialize the Zep client and ensure the user and thread exist."""
        if self._initialized and self.zep_client is not None:
            return True
        from zep_cloud import BadRequestError, NotFoundError
        from zep_cloud.client import Zep

        try:
            if self.zep_client is None:
                self.zep_client = Zep(api_key=self._api_key)
            self.session_id = self.session_id_provided or str(uuid.uuid4())
            self.user_id = self.user_id_provided or f"user-{uuid.uuid4()}"
            if self.user_id_provided is None:
                self.zep_client.user.add(user_id=self.user_id)
            else:
                try:
                    self.zep_client.user.get(self.user_id)
                except NotFoundError:
                    try:
                        self.zep_client.user.add(user_id=self.user_id)
                    except BadRequestError:
                        self.zep_client = None
                        return False
            with contextlib.suppress(Exception):
                self.zep_client.thread.create(thread_id=self.session_id, user_id=self.user_id)
            self._initialized = True
        except Exception:
            self.zep_client = None
            self._initialized = False
            return False
        else:
            return True

    def zep_add_message(self, role: _MessageRole, content: str) -> str:
        """Append a message with `role` and `content` to the current Zep session."""
        if not self.initialize() or self.zep_client is None or not self.session_id:
            return "Error: Zep client/session not initialized."
        try:
            from zep_cloud import Message as ZepMessage

            kwargs: dict[str, Any] = {
                "thread_id": self.session_id,
                "messages": [ZepMessage(role=role, content=require_non_empty(content, "content"))],
            }
            if self.ignore_assistant_messages:
                kwargs["ignore_roles"] = ["assistant"]
            self.zep_client.thread.add_messages(**kwargs)
        except Exception as exc:
            return f"Error adding message: {redact_sensitive_text(failure_text(exc))}"
        else:
            return f"Message from '{role}' added successfully to session {self.session_id}."

    def _read_session_memory(self, action: Literal["context", "messages"]) -> str:
        if not self._allow_get:
            return "Error: Reading Zep session memory is disabled."
        assert self.session_id is not None
        if action == "context":
            user_context = self.zep_client.thread.get_user_context(thread_id=self.session_id)
            return user_context.context or "No context available."
        messages_list = self.zep_client.thread.get(thread_id=self.session_id)
        return str(messages_list.messages) if messages_list.messages else "No messages available."

    def _search_graph(self, query: str | None, search_scope: _SearchScope) -> str:
        if not self._allow_search:
            return "Error: Searching Zep memory is disabled."
        if not self.user_id:
            return "Error: Zep user not initialized."
        clean_query = require_non_empty(query, "query")
        response = self.zep_client.graph.search(query=clean_query, user_id=self.user_id, scope=search_scope)
        if search_scope == "edges" and response.edges:
            facts = "\n".join(f"- {edge.fact}" for edge in response.edges)
            return f"Found {len(response.edges)} facts:\n{facts}"
        if search_scope == "nodes" and response.nodes:
            nodes = "\n".join(f"- {node.name}: {node.summary}" for node in response.nodes)
            return f"Found {len(response.nodes)} nodes:\n{nodes}"
        return f"No {search_scope} found for query: {clean_query}"

    def zep_read(
        self,
        action: _ReadAction,
        query: str | None = None,
        search_scope: _SearchScope = "edges",
    ) -> str:
        """Retrieve Zep session context/messages or search the user knowledge graph.

        Actions:
        - `context`: Summarized memory context for the current session.
        - `messages`: Raw messages in the current session.
        - `search`: Search user graph `edges` (facts) or `nodes` for `query` (`search_scope`).
        """
        if not self.initialize() or self.zep_client is None or not self.session_id:
            return "Error: Zep client/session not initialized."
        try:
            if action in {"context", "messages"}:
                return self._read_session_memory(action)
            return self._search_graph(query, search_scope)
        except Exception as exc:
            return f"Error reading Zep memory: {redact_sensitive_text(failure_text(exc))}"


@register_tool_with_metadata(
    name="zep",
    file_access=ToolFileAccess.NONE,
    requires_primary_runtime=True,
    display_name="Zep Memory",
    description="Memory system for storing, retrieving, and searching conversational data",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="Brain",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="session_id",
            label="Session ID",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="user_id",
            label="User ID",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="api_key",
            env=("ZEP_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
        ),
        ConfigField(
            name="ignore_assistant_messages",
            label="Ignore Assistant Messages",
            type="boolean",
            required=False,
            default=False,
        ),
        ConfigField(
            name="enable_add_zep_message",
            label="Enable Add Zep Message",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_get_zep_memory",
            label="Enable Get Zep Memory",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="enable_search_zep_memory",
            label="Enable Search Zep Memory",
            type="boolean",
            required=False,
            default=True,
        ),
        ConfigField(
            name="instructions",
            label="Instructions",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="all",
            label="All",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    dependencies=["zep-cloud"],
    docs_url="https://docs.mindroom.chat/tools/memory-and-storage/#zep",
    function_names=("zep_read", "zep_add_message"),
    action_scoped_grants=("zep_read",),
)
def zep_tools() -> type[ZepTools]:
    """Return Zep memory tools for storing and retrieving conversational data."""
    return ZepTools
