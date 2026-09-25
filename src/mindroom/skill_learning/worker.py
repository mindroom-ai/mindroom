"""Background worker that reviews conversations once their reply count reaches the interval."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from mindroom.agent_storage import load_agent_session
from mindroom.background_loop import run_until_stopped
from mindroom.constants import SKILL_REVIEW_NOTICE_CONTENT_KEY, SKIP_MENTIONS_KEY
from mindroom.file_locks import async_exclusive_file_lock
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.message_builder import build_message_content
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.skill_learning.library import archive_unused_skills
from mindroom.skill_learning.queue import SKILL_LEARNING_WAKE, QueueEntry, claim_due_reviews, settle_review
from mindroom.skill_learning.reviewer import ReviewProgress, review_conversation
from mindroom.skill_learning.transcript import conversation_messages
from mindroom.tool_system.skills import agent_workspace_skills_root

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_POLL_SECONDS = 30
_MAX_REVIEWS_PER_CYCLE = 4


def _skills_root(config: Config, runtime_paths: RuntimePaths, entry: QueueEntry) -> Path:
    """Return the workspace skills directory that this conversation's reviews maintain."""
    runtime = resolve_agent_runtime(entry.agent, config, runtime_paths, execution_identity=entry.execution_identity())
    workspace_root = runtime.workspace.root if runtime.workspace is not None else None
    return agent_workspace_skills_root(runtime_paths, entry.agent, workspace_root=workspace_root)


@dataclass
class SkillLearningWorker:
    """Serialize reviews across processes sharing one storage root; queued conversations survive restarts."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    client_provider: Callable[[str], nio.AsyncClient | None]
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)

    def stop(self) -> None:
        """Stop at once: the queue is durable, so an interrupted review runs again after restart."""
        self._stop_event.set()
        self._wake_event.set()
        if self._task is not None:
            self._task.cancel()

    async def run(self) -> None:
        """Run review cycles until stopped, waking early whenever a conversation reaches its interval."""
        self._task = asyncio.current_task()
        await run_until_stopped(
            stop=self._stop_event,
            wake=self._wake_event,
            signal=SKILL_LEARNING_WAKE,
            cycle=self._cycle,
        )

    async def _cycle(self) -> float:
        config = self.config_provider()
        if config is not None:
            try:
                await self._run_cycle(config)
            except Exception:
                # A broken queue file must not end learning for every agent until the next restart.
                logger.exception("Skill learning cycle failed")
        return _POLL_SECONDS

    async def _run_cycle(self, config: Config) -> None:
        async with async_exclusive_file_lock(self.runtime_paths.storage_root / "skill_learning.lock"):
            due = await asyncio.to_thread(claim_due_reviews, config, self.runtime_paths, now=time.time())
            # Later conversations stay due for the next cycle.
            for key, entry in due[:_MAX_REVIEWS_PER_CYCLE]:
                try:
                    skills_root = await asyncio.to_thread(_skills_root, config, self.runtime_paths, entry)
                    await self._review(config, key, entry, skills_root)
                except Exception:
                    logger.exception("Skill learning failed", agent=entry.agent, session_id=entry.session)
                    await asyncio.to_thread(
                        settle_review,
                        self.runtime_paths,
                        key,
                        claimed=entry,
                        outcome="failed",
                        now=time.time(),
                    )

    async def _review(
        self,
        config: Config,
        key: str,
        entry: QueueEntry,
        skills_root: Path,
    ) -> None:
        settings = config.agents[entry.agent].skill_learning
        identity = entry.execution_identity()
        progress = ReviewProgress()
        outcome: Literal["reviewed", "failed", "interrupted"] = "reviewed"
        stopped: asyncio.CancelledError | None = None
        try:
            archived = await progress.track(
                asyncio.to_thread(
                    archive_unused_skills,
                    skills_root,
                    archive_after_days=settings.archive_after_days,
                    now=datetime.now(UTC),
                ),
            )
            if archived:
                logger.info("Archived unused learned skills", agent=entry.agent, archived=archived)
            session = await asyncio.to_thread(
                load_agent_session,
                entry.agent,
                config,
                self.runtime_paths,
                entry.session,
                execution_identity=identity,
            )
            if session is not None:
                await asyncio.wait_for(
                    review_conversation(
                        config=config,
                        runtime_paths=self.runtime_paths,
                        agent_name=entry.agent,
                        session_id=entry.session,
                        identity=identity,
                        skills_root=skills_root,
                        messages=conversation_messages(session),
                        summary=session.summary.summary if session.summary is not None else None,
                        progress=progress,
                    ),
                    timeout=settings.timeout_seconds,
                )
        except asyncio.CancelledError as error:
            stopped, outcome = error, "interrupted"
        except Exception:
            outcome = "failed"
            logger.exception("Skill review failed", agent=entry.agent, session_id=entry.session)
        # Every exit, a stop included, waits for the archival and writes it started, which land even after a timeout
        # or a stop cancels the review, so they are recorded as the learner's before the state is settled.
        finish = asyncio.ensure_future(self._finish(key, entry, progress, outcome))
        while not finish.done():
            try:
                # Waiting never cancels the bookkeeping, so a stop arriving now still lets it finish.
                await asyncio.wait([finish])
            except asyncio.CancelledError as error:
                stopped = error
        if stopped is not None:
            # A bookkeeping error must not replace the cancellation.
            if (error := finish.exception()) is not None:
                logger.error("Could not record an interrupted skill review", agent=entry.agent, exc_info=error)
            raise stopped
        outcome = finish.result()
        changes = progress.changes
        logger.info(
            "Skill review finished",
            agent=entry.agent,
            session_id=entry.session,
            outcome=outcome,
            changed=sorted(changes),
        )
        if settings.notify and changes and identity is not None:
            await self._notify(entry.agent, identity, changes)

    async def _finish(
        self,
        key: str,
        claimed: QueueEntry,
        progress: ReviewProgress,
        outcome: Literal["reviewed", "failed", "interrupted"],
    ) -> Literal["reviewed", "failed", "interrupted"]:
        await progress.settled()
        if progress.changes:
            # Like Hermes' best-effort review, one that already changed skills is done; rerunning the same
            # conversation would repeat its edits and notices.
            outcome = "reviewed"
        await asyncio.to_thread(
            settle_review,
            self.runtime_paths,
            key,
            claimed=claimed,
            outcome=outcome,
            now=time.time(),
        )
        return outcome

    async def _notify(self, agent_name: str, identity: ToolExecutionIdentity, changes: dict[str, str]) -> None:
        """Tell the conversation which skills its review changed, like Hermes' self-improvement summary."""
        client = self.client_provider(agent_name)
        if client is None or identity.channel != "matrix" or identity.room_id is None:
            return
        body = "💾 Skill review: " + " · ".join(f"{action} `{name}`" for name, action in sorted(changes.items()))
        thread_id = identity.resolved_thread_id
        content = build_message_content(
            body,
            thread_event_id=thread_id,
            latest_thread_event_id=thread_id,
            # The marker keeps the notice out of later model context, like compaction notices.
            extra_content={
                "msgtype": "m.notice",
                SKILL_REVIEW_NOTICE_CONTENT_KEY: {"changes": changes},
                SKIP_MENTIONS_KEY: True,
            },
        )
        try:
            delivered = await send_message_result(client, identity.room_id, content)
        except Exception:
            # The review is already settled; a lost notice must not make it look failed and run again.
            logger.exception("Could not post skill review notice", agent=agent_name, room_id=identity.room_id)
            return
        if delivered is None:
            logger.warning("Could not post skill review notice", agent=agent_name, room_id=identity.room_id)
