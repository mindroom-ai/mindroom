"""Give Agno's Mem0 toolkit the user filter that Mem0 2 search and listing require."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agno.tools.mem0 import Mem0Tools

from mindroom.failure_text import failure_text
from mindroom.redaction import redact_sensitive_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from mem0.client.main import MemoryClient
    from mem0.memory.main import Memory


@dataclass(frozen=True)
class _UserFilterClient:
    """Forward the toolkit's Mem0 calls, passing the user of a search or listing as ``filters``.

    Mem0 2.1 and later raise a failed provider call with the provider's error body, which can quote a rejected key,
    and the toolkit returns a failure's text to the model, so each failure is raised again with credentials redacted.
    Agno renders a ``ValueError``'s text alone and any other failure after a prefix, so a ``ValueError`` stays one.
    """

    client: Memory | MemoryClient

    def add(self, messages: list[dict[str, str]], **kwargs: Any) -> object:  # noqa: ANN401 - Mem0's add options
        return _redacting_failures(self.client.add, messages, **kwargs)

    def search(self, query: str, *, user_id: str) -> object:
        return _redacting_failures(self.client.search, query, filters={"user_id": user_id})

    def get_all(self, *, user_id: str) -> object:
        return _redacting_failures(self.client.get_all, filters={"user_id": user_id})

    def delete_all(self, *, user_id: str) -> object:
        return _redacting_failures(self.client.delete_all, user_id=user_id)


def _redacting_failures(call: Callable[..., object], *args: object, **kwargs: object) -> object:
    try:
        return call(*args, **kwargs)
    except Exception as exc:
        error_type = ValueError if isinstance(exc, ValueError) else RuntimeError
        raise error_type(redact_sensitive_text(failure_text(exc))) from None


class UserFilterMem0Tools(Mem0Tools):
    """Mem0Tools whose search and listing reach Mem0 2."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        api_key: str | None = None,
        user_id: str | None = None,
        org_id: str | None = None,
        project_id: str | None = None,
        infer: bool = True,
        enable_add_memory: bool = True,
        enable_search_memory: bool = True,
        enable_get_all_memories: bool = True,
        enable_delete_all_memories: bool = True,
        all: bool = False,  # noqa: A002 - upstream option name
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        super().__init__(
            config=config,
            api_key=api_key,
            user_id=user_id,
            org_id=org_id,
            project_id=project_id,
            infer=infer,
            enable_add_memory=enable_add_memory,
            enable_search_memory=enable_search_memory,
            enable_get_all_memories=enable_get_all_memories,
            enable_delete_all_memories=enable_delete_all_memories,
            all=all,
            **kwargs,
        )
        # AGNO_COMPAT: Mem0Tools passes its user to Mem0 2 search and listing as a top-level user_id.
        # Reason: Agno 3.0.9 search_memory and get_all_memories call client.search(query=..., user_id=...)
        # and client.get_all(user_id=...); Mem0 2.0 and later, both the local Memory and the platform
        # MemoryClient, reject top-level entity parameters there and take them in filters, so both tool
        # functions return Mem0's ValueError text instead of memories.
        # Upstream issue: https://github.com/agno-agi/agno/issues/11340, open on October 27, 2026.
        # Upstream PR: None identified; the open async Mem0Tools PR, https://github.com/agno-agi/agno/pull/5886,
        # keeps the top-level user_id.
        # Remove when: Agno's Mem0Tools passes filters={"user_id": ...} to search and get_all; keep redacting
        # the credentials in the Mem0 failures the toolkit returns to the model.
        # Coverage: tests/test_mem0_tool.py::test_mem0_tool_search_and_listing_pass_the_user_as_a_filter,
        # tests/test_mem0_tool.py::test_mem0_tool_failures_reach_the_model_without_the_rejected_key
        self.client = _UserFilterClient(self.client)  # ty: ignore[invalid-assignment]
