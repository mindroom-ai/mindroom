"""Mem0 persistent memory toolkit."""

from __future__ import annotations

import inspect
from typing import Any, Literal

from mindroom.failure_text import failure_text
from mindroom.redaction import redact_sensitive_text
from mindroom.tool_system.authoring import Toolkit, current_call
from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolStatus,
    bool_config_field,
)
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.runtime_context import current_execution_identity
from mindroom.tools.native_helpers import UNSET, UnsetType, json_dumps, require_non_empty, resolve_toggle

_ReadAction = Literal["search", "get_all"]


class _Mem0ScopeMismatchError(ValueError):
    """Raised when configured org_id/project_id disagrees with the Mem0 API key scope."""


def _validate_client_scope(
    client: Any,  # noqa: ANN401
    *,
    org_id: str | None,
    project_id: str | None,
) -> None:
    if org_id is not None:
        discovered_org = getattr(client, "org_id", None)
        if discovered_org != org_id:
            msg = (
                f"Configured Mem0 org_id ({org_id!r}) does not match the organization "
                f"resolved from the API key ({discovered_org!r})."
            )
            raise _Mem0ScopeMismatchError(msg)
    if project_id is not None:
        discovered_project = getattr(client, "project_id", None)
        if discovered_project != project_id:
            msg = (
                f"Configured Mem0 project_id ({project_id!r}) does not match the project "
                f"resolved from the API key ({discovered_project!r})."
            )
            raise _Mem0ScopeMismatchError(msg)


def _build_mem0_client(
    *,
    api_key: str | None,
    org_id: str | None,
    project_id: str | None,
    config: dict[str, Any] | None,
) -> Any:  # noqa: ANN401
    from mem0.client.main import MemoryClient
    from mem0.memory.main import Memory

    if api_key:
        sig = inspect.signature(MemoryClient.__init__)
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if org_id and "org_id" in sig.parameters:
            client_kwargs["org_id"] = org_id
        if project_id and "project_id" in sig.parameters:
            client_kwargs["project_id"] = project_id
        client = MemoryClient(**client_kwargs)
        _validate_client_scope(client, org_id=org_id, project_id=project_id)
        return client
    if config is not None:
        return Memory.from_config(config)
    return Memory()


def _extract_memory_list(results: object) -> list[Any]:
    if isinstance(results, dict) and isinstance(results.get("results"), list):
        return results["results"]
    if isinstance(results, list):
        return results
    return []


class _Mem0Tools(Toolkit):
    """Store, search, list, and delete persistent user memories via Mem0 2."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        api_key: str | None = None,
        user_id: str | None = None,
        org_id: str | None = None,
        project_id: str | None = None,
        infer: bool = True,
        enable_add_memory: bool | None | UnsetType = UNSET,
        enable_search_memory: bool | None | UnsetType = UNSET,
        enable_get_all_memories: bool | None | UnsetType = UNSET,
        enable_delete_all_memories: bool | None | UnsetType = UNSET,
        all: bool | None | UnsetType = UNSET,  # noqa: A002
    ) -> None:
        self._config = config
        self.api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
        self.user_id = user_id.strip() if isinstance(user_id, str) and user_id.strip() else None
        self.org_id = org_id.strip() if isinstance(org_id, str) and org_id.strip() else None
        self.project_id = project_id.strip() if isinstance(project_id, str) and project_id.strip() else None
        self.infer = infer
        self.client: Any = None

        enable_all = resolve_toggle(all, default=False)
        self._allow_add = resolve_toggle(enable_add_memory, default=True, enable_all=enable_all)
        self._allow_search = resolve_toggle(enable_search_memory, default=True, enable_all=enable_all)
        self._allow_get_all = resolve_toggle(enable_get_all_memories, default=True, enable_all=enable_all)
        self._allow_delete_all = resolve_toggle(enable_delete_all_memories, default=True, enable_all=enable_all)

        tools: list[Any] = []
        if self._allow_search or self._allow_get_all:
            tools.append(self.mem0_read)
        if self._allow_add:
            tools.append(self.mem0_add)
        if self._allow_delete_all:
            tools.append(self.mem0_delete_all)
        super().__init__(name="mem0", tools=tools)

    def _get_client(self) -> Any:  # noqa: ANN401
        if self.client is None:
            try:
                self.client = _build_mem0_client(
                    api_key=self.api_key,
                    org_id=self.org_id,
                    project_id=self.project_id,
                    config=self._config,
                )
            except _Mem0ScopeMismatchError:
                raise
            except Exception as exc:
                msg = f"Failed to initialize Mem0 client: {redact_sensitive_text(failure_text(exc))}"
                raise ConnectionError(msg) from None
        return self.client

    def _require_user_id(self, method_name: str) -> str:
        if self.user_id:
            return self.user_id
        call = current_call()
        if call is not None and call.runtime is not None and call.runtime.requester_id:
            return call.runtime.requester_id
        identity = current_execution_identity()
        if identity is not None and identity.requester_id:
            return identity.requester_id
        msg = f"Error in {method_name}: A user_id must be provided or resolvable from the active request."
        raise ValueError(msg)

    @staticmethod
    def _redacted_error(prefix: str, exc: Exception) -> str:
        text = redact_sensitive_text(failure_text(exc))
        if isinstance(exc, ValueError):
            return text
        return f"{prefix}: {text}"

    def mem0_read(self, action: _ReadAction, query: str | None = None) -> str:
        """Search or list stored Mem0 memories for the configured user.

        Actions:
        - `search`: Semantic search across memories (`query`).
        - `get_all`: Return all memories for the user.
        """
        try:
            user_id = self._require_user_id("mem0_read")
            if action == "search":
                if not self._allow_search:
                    return "Error searching memory: Searching Mem0 memories is disabled."
                clean_query = require_non_empty(query, "query")
                results = self._get_client().search(clean_query, filters={"user_id": user_id})
                return json_dumps(_extract_memory_list(results))
            if not self._allow_get_all:
                return "Error getting all memories: Listing Mem0 memories is disabled."
            results = self._get_client().get_all(filters={"user_id": user_id})
            return json_dumps(_extract_memory_list(results))
        except _Mem0ScopeMismatchError:
            raise
        except Exception as exc:
            prefix = "Error searching memory" if action == "search" else "Error getting all memories"
            return self._redacted_error(prefix, exc)

    def mem0_add(self, content: str | dict[str, str]) -> str:
        """Store a fact or key-value dictionary in the user's Mem0 memory."""
        try:
            user_id = self._require_user_id("mem0_add")
            text = json_dumps(content) if isinstance(content, dict) else str(content)
            result = self._get_client().add([{"role": "user", "content": text}], user_id=user_id, infer=self.infer)
            return json_dumps(result)
        except _Mem0ScopeMismatchError:
            raise
        except Exception as exc:
            return self._redacted_error("Error adding memory", exc)

    def mem0_delete_all(self) -> str:
        """Permanently delete all Mem0 memories for the configured user."""
        try:
            user_id = self._require_user_id("mem0_delete_all")
            self._get_client().delete_all(user_id=user_id)
        except _Mem0ScopeMismatchError:
            raise
        except Exception as exc:
            return self._redacted_error("Error deleting all memories", exc)
        else:
            return f"Successfully deleted all memories for user_id: {user_id}."


@register_tool_with_metadata(
    name="mem0",
    file_access=ToolFileAccess.NONE,
    requires_primary_runtime=True,
    display_name="Mem0 Memory",
    description="Persistent memory system that stores, retrieves, searches, and manages user memories and context",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="Brain",
    icon_color="text-purple-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("MEM0_API_KEY",),
            label="API Key",
            type="password",
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
            name="org_id",
            env=("MEM0_ORG_ID",),
            label="Org ID",
            type="text",
            required=False,
            default=None,
        ),
        ConfigField(
            name="project_id",
            env=("MEM0_PROJECT_ID",),
            label="Project ID",
            type="text",
            required=False,
            default=None,
        ),
        bool_config_field("infer", "Infer"),
        bool_config_field("enable_add_memory", "Enable Add Memory"),
        bool_config_field("enable_search_memory", "Enable Search Memory"),
        bool_config_field("enable_get_all_memories", "Enable Get All Memories"),
        bool_config_field("enable_delete_all_memories", "Enable Delete All Memories"),
        bool_config_field("all", "All", default=False),
    ],
    dependencies=["mem0ai"],
    docs_url="https://docs.mindroom.chat/tools/memory-and-storage/#mem0",
    function_names=("mem0_read", "mem0_add", "mem0_delete_all"),
    action_scoped_grants=("mem0_read",),
)
def mem0_tools() -> type[Toolkit]:
    """Return Mem0 memory tools for persistent memory management."""
    return _Mem0Tools
