"""AI domain facade."""

import sys
from types import ModuleType

from mindroom.media_fallback import append_inline_media_fallback_prompt, should_retry_without_inline_media

# ruff: noqa: F401,F403,F405
from . import core as _core
from .core import *
from .core import (
    QUEUED_MESSAGE_NOTICE_TEXT,
    PreparedAgentRun,
    _agent_tools_schema,
    _append_inline_media_fallback_to_run_input,
    _attach_media_to_run_input,
    _copy_run_input,
    _prepare_agent_and_prompt,
    _render_system_enrichment_context,
    build_llm_request_log_context,
    build_memory_prompt_parts,
    close_agent_runtime_sqlite_dbs,
    create_agent,
    get_user_friendly_error_message,
    open_resolved_scope_session_context,
    prepare_agent_execution_context,
)


class _FacadeModule(ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if hasattr(_core, name):
            setattr(_core, name, value)


sys.modules[__name__].__class__ = _FacadeModule

__all__ = [
    "AIStreamChunk",
    "ai_response",
    "build_matrix_run_metadata",
    "cached_agent_run",
    "cleanup_queued_notice_state",
    "get_model_instance",
    "install_queued_message_notice_hook",
    "queued_message_signal_context",
    "scrub_queued_notice_session_context",
    "stream_agent_response",
]
