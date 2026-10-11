"""Capture typed GitHub failures across Agno's serialized-error boundary."""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agno.tools import github as agno_github_module
from agno.utils import log as agno_log_module

if TYPE_CHECKING:
    import logging
    from collections.abc import Callable

# AGNO_COMPAT: GithubTools serializes provider failures without typed access.
# Reason: GithubTools catches provider exceptions and returns JSON error strings;
# callers cannot distinguish provider failures from local tool validation errors.
# Upstream issue: No matching structured GithubTools error-handler issue identified.
# Upstream PR: None identified for this extension point.
# Remove when: Agno exposes typed provider failures before serialization; preserve
# owner OAuth recovery, safe error messages, and successful partial results.
# Coverage: tests/test_github_oauth_tool.py.


@dataclass(frozen=True, slots=True)
class _GithubProviderFailure:
    """Typed provider failure captured before Agno serializes the exception."""

    status_code: int | None


_github_provider_failure: ContextVar[_GithubProviderFailure | None] = ContextVar(
    "github_provider_failure",
    default=None,
)


def record_provider_failure(status_code: int | None) -> None:
    """Capture owner-classified status before Agno catches the provider failure."""
    _github_provider_failure.set(_GithubProviderFailure(status_code))


def _is_serialized_github_error_result(result: object) -> bool:
    if not isinstance(result, str):
        return False
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and set(payload) == {"error"} and isinstance(payload["error"], str)


def call_with_provider_failure_capture(
    entrypoint: Callable[..., object],
    *args: object,
    **kwargs: object,
) -> tuple[object, _GithubProviderFailure | None]:
    """Return typed failure only when Agno replaced the tool result with an error."""
    failure_token = _github_provider_failure.set(None)
    try:
        result = entrypoint(*args, **kwargs)
        provider_failure = _github_provider_failure.get()
    finally:
        _github_provider_failure.reset(failure_token)
    if provider_failure is None or not _is_serialized_github_error_result(result):
        return result, None
    return result, provider_failure


# AGNO_COMPAT: GithubTools lacks structured errors and log-redaction hooks.
# Reason: GithubTools logs some caught failures as strings with fixed prefixes,
# and routes messages through multiple Agno loggers with no redaction hook.
# Upstream issue: No matching GithubTools structured logging/redaction issue identified.
# Upstream PR: None identified; typed-result handling alone cannot replace this filter.
# Remove when: All affected Agno log paths expose structured errors or a redaction
# hook before rendering; owner sanitization and PyGithub retry logging remain.
# Coverage: tests/test_github_oauth_tool.py::test_agno_github_log_redaction_prefixes_match_pinned_upstream;
# tests/test_github_oauth_tool.py::test_github_partial_results_do_not_log_nested_provider_details.

_PROVIDER_DETAIL_LOG_PREFIXES = (
    "Error getting actual open issues:",
    "Error getting open PRs count:",
    "Error processing individual PR:",
    "Error getting recent open PRs:",
    "Error calculating PR metrics:",
    "Error getting contributors:",
    "Error decoding file content:",
)


def is_provider_detail_log(record: logging.LogRecord) -> bool:
    """Recognize the pinned Agno messages that inline provider-controlled text."""
    return isinstance(record.msg, str) and record.msg.startswith(_PROVIDER_DETAIL_LOG_PREFIXES)


def install_provider_log_filter(filter_type: type[logging.Filter], *, retry_logger: logging.Logger) -> None:
    """Install the owner's filter once on all Agno routes and its retry logger."""
    upstream_loggers = {
        agno_github_module.logger,
        agno_log_module.logger,
        agno_log_module.agent_logger,
        agno_log_module.team_logger,
        agno_log_module.workflow_logger,
        retry_logger,
    }
    for upstream_logger in upstream_loggers:
        if not any(isinstance(log_filter, filter_type) for log_filter in upstream_logger.filters):
            upstream_logger.addFilter(filter_type())
