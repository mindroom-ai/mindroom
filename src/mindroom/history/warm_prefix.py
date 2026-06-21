"""Warm-prefix compaction summary requests.

When the compaction summary model is the active reply model, the summary call
can reproduce the reply-path request prefix — system prompt, tool schemas, and
the history runs being compacted — and append one summary instruction as the
final user turn.
Providers that cache the reply prefix (Vertex Claude full-prefix breakpoints,
OpenAI automatic prefix caching, Anthropic system/tool caching) then serve most
of the summary input from cache instead of re-reading it at full price.

This module adapts compaction-domain inputs into the pure Agno forked-request
adapter (``mindroom.history.agno_forked_request``) and returns the
``SummaryProviderRequest`` consumed by ``mindroom.history.summary_call``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from agno.models.message import Message

from mindroom.history.agno_forked_request import build_agent_provider_request_from_runs
from mindroom.history.summary_call import SummaryProviderRequest

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.agent import Agent
    from agno.run.agent import RunOutput
    from agno.session.agent import AgentSession


@dataclass(frozen=True)
class WarmPrefixSummaryContext:
    """Live agent and resolved instruction enabling warm-prefix summary requests.

    Present only when the compaction summary model is the active reply model, so
    the request prefix can match the reply path's cached prefix.
    """

    agent: Agent
    instruction: str


async def build_warm_prefix_summary_request(
    *,
    agent: Agent,
    working_session: AgentSession,
    prefix_runs: Sequence[RunOutput],
    final_instruction: str,
) -> SummaryProviderRequest:
    """Build one warm-prefix summary request from the runs being compacted."""
    request = await build_agent_provider_request_from_runs(
        agent=agent,
        source_session=working_session,
        prefix_runs=prefix_runs,
        final_user_message=Message(role="user", content=final_instruction),
        synthetic_run_id=_synthetic_compaction_run_id(prefix_runs),
    )
    return SummaryProviderRequest(
        messages=request.messages,
        tools=request.tools,
        tool_choice=request.tool_choice,
    )


def _synthetic_compaction_run_id(prefix_runs: Sequence[RunOutput]) -> str:
    run_ids = [run.run_id for run in prefix_runs if isinstance(run.run_id, str) and run.run_id]
    if run_ids:
        joined = "+".join(run_ids)
        if len(joined) <= 200:
            return joined
        digest = hashlib.sha256(joined.encode()).hexdigest()[:16]
        return f"compaction-summary-{digest}"
    return f"compaction-summary-{uuid4()}"
