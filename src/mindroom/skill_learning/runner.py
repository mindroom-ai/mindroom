"""Reply counts and the skill reviews they start.

Like Hermes' counter, each completed response to a person adds its model replies, one per tool-calling step plus the
final answer, to its conversation's count, and a ``skill_manage`` call restarts the count. Once the count reaches the
review interval it restarts and a review starts in the background, so it never delays a reply; a response starting in
the same conversation cancels the running review. Counts live in memory, like Hermes', so a restart forgets them.
Conversations are keyed by agent, private worker scope, and session, never by requester, so a shared thread is
reviewed once however many people talk in it.
"""

from __future__ import annotations

import asyncio
import contextvars
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Protocol

from agno.run.agent import RunOutput

from mindroom.agent_storage import create_session_storage
from mindroom.background_tasks import create_background_task, run_blocking_until_complete
from mindroom.constants import SKILL_REVIEW_NOTICE_CONTENT_KEY, SKIP_MENTIONS_KEY
from mindroom.llm_request_logging import bind_llm_request_log_context
from mindroom.logging_config import bound_log_context, get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.message_builder import build_message_content
from mindroom.runtime_resolution import resolve_agent_execution, resolve_agent_runtime
from mindroom.skill_learning.library import archive_unused_skills
from mindroom.skill_learning.reviewer import review_conversation
from mindroom.skill_learning.tools import ReviewProgress, library_turn
from mindroom.skill_learning.transcript import count_model_replies
from mindroom.tool_system.skills import agent_workspace_skills_root

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.skill_learning.capture import CapturedRequest
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)


class _NoticeBot(Protocol):
    """The agent bot a review's notice is sent as."""

    running: bool
    client: nio.AsyncClient | None

    async def latest_thread_event_id_if_needed(self, room_id: str, thread_id: str) -> str | None: ...


@dataclass(frozen=True)
class _ReviewScope:
    """One conversation a review covers, with the scope of the latest person whose response counted in it."""

    agent: str
    session: str
    identity: ToolExecutionIdentity | None

    def key(self, config: Config) -> str:
        """Return the conversation's key: its agent, private worker scope, and session."""
        execution = resolve_agent_execution(self.agent, config, execution_identity=self.identity)
        worker = f"{execution.worker_key}:" if execution.is_private else ""
        return f"{self.agent}:{worker}{self.session}"


def _response_replies(
    config: Config,
    runtime_paths: RuntimePaths,
    scope: _ReviewScope,
    run_id: str,
) -> tuple[int, bool]:
    storage = create_session_storage(scope.agent, config, runtime_paths, execution_identity=scope.identity)
    try:
        run = storage.get_run(run_id)
    finally:
        storage.close()
    return count_model_replies(run if isinstance(run, RunOutput) else None)


def _review_log_context(scope: _ReviewScope, correlation_id: str) -> dict[str, str]:
    """Return the log fields of the reviewed conversation and the response that made it due."""
    identity = scope.identity
    fields = {
        "agent_id": scope.agent,
        "session_id": scope.session,
        "requester_id": identity.requester_id if identity is not None else None,
        "room_id": identity.room_id if identity is not None else None,
        "thread_id": identity.resolved_thread_id if identity is not None else None,
        "correlation_id": correlation_id,
        # Like the review's usage rows.
        "kind": "skill_learning",
    }
    return {key: value for key, value in fields.items() if value is not None}


def _skills_root(config: Config, runtime_paths: RuntimePaths, scope: _ReviewScope) -> Path:
    """Return the workspace skills directory that this conversation's reviews maintain."""
    runtime = resolve_agent_runtime(scope.agent, config, runtime_paths, execution_identity=scope.identity)
    workspace_root = runtime.workspace.root if runtime.workspace is not None else None
    return agent_workspace_skills_root(runtime_paths, scope.agent, workspace_root=workspace_root)


@dataclass
class SkillReviewRunner:
    """Own the process's reply counts and running skill reviews, at most one per conversation."""

    runtime_paths: RuntimePaths
    bot_provider: Callable[[str], _NoticeBot | None]
    _replies: dict[str, int] = field(default_factory=dict, init=False)
    _reviews: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False)
    _stopped: bool = field(default=False, init=False)

    def cancel(
        self,
        config: Config,
        *,
        agent_name: str,
        session_id: str,
        identity: ToolExecutionIdentity | None,
    ) -> None:
        """Stop a conversation's review because a response starts in it."""
        if (task := self._reviews.get(_ReviewScope(agent_name, session_id, identity).key(config))) is not None:
            task.cancel()

    async def count(
        self,
        config: Config,
        *,
        agent_name: str,
        session_id: str,
        identity: ToolExecutionIdentity | None,
        run_id: str,
        captured: CapturedRequest | None,
        correlation_id: str,
    ) -> None:
        """Add a person's completed response to its conversation's count, and start a review once the count is due.

        ``captured`` is the response's final model request, which the review forks when it belongs to ``run_id``, and
        ``correlation_id`` is the response's, which the review's logs carry.
        """
        scope = _ReviewScope(agent_name, session_id, identity)
        replies, restarted = await asyncio.to_thread(_response_replies, config, self.runtime_paths, scope, run_id)
        key = scope.key(config)
        replies += 0 if restarted else self._replies.get(key, 0)
        running = self._reviews.get(key)
        if (
            replies < config.agents[scope.agent].skill_learning.review_interval
            or self._stopped
            or (running is not None and not running.done())
        ):
            self._replies[key] = replies
            return
        # Like Hermes, the count restarts when a review starts.
        self._replies[key] = 0
        task = create_background_task(
            self._review(
                config,
                scope,
                captured if captured is not None and captured.run_id == run_id else None,
                correlation_id,
            ),
            name=f"skill_review:{scope.agent}",
            # The response's context carries its queued-message and mid-turn state, which the reused model's hooks
            # would otherwise apply to the review's requests.
            context=contextvars.Context(),
        )
        self._reviews[key] = task
        task.add_done_callback(lambda done: self._reviews.pop(key) if self._reviews.get(key) is done else None)

    async def stop(self) -> None:
        """Stop every running review."""
        self._stopped = True
        tasks = list(self._reviews.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _review(
        self,
        config: Config,
        scope: _ReviewScope,
        captured: CapturedRequest | None,
        correlation_id: str,
    ) -> None:
        # The review runs in a fresh context, so it binds its conversation's fields, as the response's turn did, for
        # its LLM usage, request, and notice logs.
        log_context = _review_log_context(scope, correlation_id)
        with bound_log_context(**log_context), bind_llm_request_log_context(**log_context):
            await self._run_review(config, scope, captured)

    async def _run_review(self, config: Config, scope: _ReviewScope, captured: CapturedRequest | None) -> None:
        settings = config.agents[scope.agent].skill_learning
        progress = ReviewProgress()
        try:
            skills_root = await asyncio.to_thread(_skills_root, config, self.runtime_paths, scope)
            # Archival moves whole skill directories, so a chat skill_manage call waits instead of writing into one.
            async with library_turn(skills_root):
                archived = await run_blocking_until_complete(
                    partial(
                        archive_unused_skills,
                        skills_root,
                        archive_after_days=settings.archive_after_days,
                        now=datetime.now(UTC),
                    ),
                )
            if archived:
                logger.info("Archived unused learned skills", archived=archived)
            await asyncio.wait_for(
                review_conversation(
                    config=config,
                    runtime_paths=self.runtime_paths,
                    agent_name=scope.agent,
                    session_id=scope.session,
                    identity=scope.identity,
                    skills_root=skills_root,
                    captured=captured,
                    progress=progress,
                ),
                timeout=settings.timeout_seconds,
            )
        except TimeoutError:
            logger.info("Skill review reached its timeout")
        except Exception:
            logger.exception("Skill review failed")
        finally:
            # The notice runs on its own, so a new response that stops the review after its writes landed cannot stop
            # the notice too; shutdown sends none.
            if settings.notify and progress.changes and not self._stopped:
                create_background_task(self._notify(scope, progress.changes), name=f"skill_notice:{scope.agent}")
        logger.info("Skill review finished", changed=sorted(progress.changes))

    async def _notify(self, scope: _ReviewScope, changes: dict[str, str]) -> None:
        """Tell the conversation which skills its review changed, like Hermes' self-improvement summary."""
        identity = scope.identity
        bot = self.bot_provider(scope.agent)
        client = bot.client if bot is not None and bot.running else None
        if bot is None or client is None or identity is None or identity.channel != "matrix" or not identity.room_id:
            return
        body = "💾 Skill review: " + " · ".join(f"{action} `{name}`" for name, action in sorted(changes.items()))
        thread_id = identity.resolved_thread_id
        # Like approval events, the reply fallback names the thread's newest event for clients without threads.
        latest = await bot.latest_thread_event_id_if_needed(identity.room_id, thread_id) if thread_id else None
        content = build_message_content(
            body,
            thread_event_id=thread_id,
            latest_thread_event_id=latest or thread_id,
            # The marker keeps the notice out of later model context, like compaction notices.
            extra_content={
                "msgtype": "m.notice",
                SKILL_REVIEW_NOTICE_CONTENT_KEY: {"changes": changes},
                SKIP_MENTIONS_KEY: True,
            },
        )
        if await send_message_result(client, identity.room_id, content) is None:
            logger.warning("Could not post skill review notice")
