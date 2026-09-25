"""Background worker that counts completed runs and reviews conversations once they reach the interval."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.agent_storage import load_agent_session
from mindroom.file_locks import async_exclusive_file_lock
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.message_builder import build_message_content
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.skill_learning.library import archive_unused_skills, skills_fingerprint
from mindroom.skill_learning.queue import (
    QueueEntry,
    claim_due_reviews,
    register_wake_event,
    settle_review,
    unregister_wake_event,
)
from mindroom.skill_learning.reviewer import review_conversation
from mindroom.skill_learning.transcript import conversation_messages, count_model_replies
from mindroom.tool_system.skills import agent_workspace_skills_root

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import nio
    from agno.session.agent import AgentSession

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_POLL_SECONDS = 30
_MAX_REVIEWS_PER_CYCLE = 4


@dataclass(frozen=True)
class _DueReview:
    """A conversation whose counter reached the review interval, with the state it was counted against."""

    key: str
    entry: QueueEntry
    counted: tuple[str, ...]
    skills_root: Path
    fingerprint: str
    iterations: int
    session: AgentSession


@dataclass
class SkillLearningWorker:
    """Serialize reviews across processes sharing one storage root; queued runs survive restarts."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    client_provider: Callable[[str], nio.AsyncClient | None]
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def stop(self) -> None:
        """Request graceful shutdown of the worker loop."""
        self._stop_event.set()
        self._wake_event.set()

    async def run(self) -> None:
        """Run review cycles until stopped, waking early whenever a run is queued."""
        register_wake_event(self._wake_event)
        try:
            while not self._stop_event.is_set():
                config = self.config_provider()
                if config is not None:
                    try:
                        await self._run_cycle(config)
                    except Exception:
                        # One broken cycle must not end learning for every agent until the next restart.
                        logger.exception("Skill learning cycle failed")
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=_POLL_SECONDS)
                except TimeoutError:
                    continue
        finally:
            unregister_wake_event(self._wake_event)

    async def _run_cycle(self, config: Config) -> None:
        async with async_exclusive_file_lock(self.runtime_paths.storage_root / "skill_learning.lock"):
            due = await asyncio.to_thread(claim_due_reviews, config, self.runtime_paths, now=time.time())
            reviews_left = _MAX_REVIEWS_PER_CYCLE
            for key, entry in due:
                if self._stop_event.is_set():
                    return
                try:
                    review = await self._count(config, key, entry, may_review=reviews_left > 0)
                except Exception:
                    logger.exception("Skill learning could not count a conversation", agent=entry.agent)
                    await asyncio.to_thread(
                        settle_review,
                        self.runtime_paths,
                        key,
                        (),
                        iterations=entry.iterations,
                        failed_at=time.time(),
                    )
                    continue
                if review is not None:
                    reviews_left -= 1
                    await self._review(config, review)

    async def _count(self, config: Config, key: str, entry: QueueEntry, *, may_review: bool) -> _DueReview | None:
        """Count the entry's new runs, returning a review once the conversation reached its interval."""
        counted = tuple(entry.pending_run_ids)
        identity = entry.execution_identity()
        runtime = await asyncio.to_thread(resolve_agent_runtime, entry.agent, config, self.runtime_paths, identity)
        workspace_root = runtime.workspace.root if runtime.workspace is not None else None
        skills_root = agent_workspace_skills_root(self.runtime_paths, entry.agent, workspace_root=workspace_root)
        session = await asyncio.to_thread(
            load_agent_session,
            entry.agent,
            config,
            self.runtime_paths,
            entry.session,
            execution_identity=identity,
        )
        fingerprint = await asyncio.to_thread(skills_fingerprint, skills_root)
        iterations = entry.iterations + (count_model_replies(session, counted) if session is not None else 0)
        if entry.seen_fingerprint is not None and fingerprint != entry.seen_fingerprint:
            # Hermes resets its counter when the agent saves a skill itself; here someone other than the learner
            # changed the workspace skills since this conversation was last checked.
            iterations = 0
        if (
            session is not None
            and may_review
            and iterations >= config.agents[entry.agent].skill_learning.review_interval
        ):
            return _DueReview(key, entry, counted, skills_root, fingerprint, iterations, session)
        await asyncio.to_thread(
            settle_review,
            self.runtime_paths,
            key,
            counted,
            iterations=iterations,
            skills_root=str(skills_root),
            fingerprint=fingerprint,
        )
        return None

    async def _review(self, config: Config, review: _DueReview) -> None:
        entry = review.entry
        settings = config.agents[entry.agent].skill_learning
        identity = entry.execution_identity()
        failed_at: float | None = None
        changes: dict[str, str] = {}
        archived: list[str] = []
        try:
            archived = await asyncio.to_thread(
                archive_unused_skills,
                review.skills_root,
                archive_after_days=settings.archive_after_days,
                now=datetime.now(UTC),
            )
            await asyncio.wait_for(
                review_conversation(
                    config=config,
                    runtime_paths=self.runtime_paths,
                    agent_name=entry.agent,
                    session_id=entry.session,
                    identity=identity,
                    skills_root=review.skills_root,
                    messages=conversation_messages(review.session),
                    changes=changes,
                ),
                timeout=settings.timeout_seconds,
            )
        except Exception:
            failed_at = time.time()
            logger.exception("Skill review failed", agent=entry.agent, session_id=entry.session)
        # The learner's own writes and archival, even from a failed review, must not later read as someone
        # else's skill edits.
        await asyncio.to_thread(
            settle_review,
            self.runtime_paths,
            review.key,
            review.counted,
            iterations=review.iterations if failed_at is not None else 0,
            skills_root=str(review.skills_root),
            fingerprint=await asyncio.to_thread(skills_fingerprint, review.skills_root),
            previous_fingerprint=review.fingerprint,
            failed_at=failed_at,
        )
        logger.info(
            "Skill review finished",
            agent=entry.agent,
            session_id=entry.session,
            failed=failed_at is not None,
            changed=sorted(changes),
            archived=archived,
        )
        if settings.notify and (changes or archived) and identity is not None:
            await self._notify(entry.agent, identity, changes, archived)

    async def _notify(
        self,
        agent_name: str,
        identity: ToolExecutionIdentity,
        changes: dict[str, str],
        archived: list[str],
    ) -> None:
        """Tell the conversation what changed, like Hermes' self-improvement summary."""
        client = self.client_provider(agent_name)
        if client is None or identity.channel != "matrix" or identity.room_id is None:
            return
        parts = [f"{action} `{name}`" for name, action in sorted(changes.items())]
        parts.extend(f"archived unused `{name}`" for name in archived)
        thread_id = identity.resolved_thread_id
        content = build_message_content(
            f"💾 Skill review: {' · '.join(parts)}",
            thread_event_id=thread_id,
            latest_thread_event_id=thread_id,
            extra_content={"msgtype": "m.notice"},
        )
        if await send_message_result(client, identity.room_id, content) is None:
            logger.warning("Could not post skill review notice", agent=agent_name, room_id=identity.room_id)
