"""Skill reviews started by the completed responses that make a conversation due.

Like Hermes' post-turn review fork, a review starts right after the response that reached the review interval and
never delays a reply: a response starting in the same conversation cancels the running review, and the kept count lets
the next completed reply start another. Reviews of one skills directory run one at a time across processes that share
the storage root.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Literal

from mindroom.background_tasks import create_background_task, run_blocking_until_complete
from mindroom.constants import SKILL_REVIEW_NOTICE_CONTENT_KEY, SKIP_MENTIONS_KEY
from mindroom.file_locks import async_exclusive_file_lock
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.message_builder import build_message_content
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.skill_learning.library import archive_unused_skills
from mindroom.skill_learning.queue import settle_review
from mindroom.skill_learning.reviewer import review_conversation
from mindroom.skill_learning.tools import ReviewProgress, library_turn
from mindroom.tool_system.skills import agent_workspace_skills_root

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import nio

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.skill_learning.capture import CapturedRequest
    from mindroom.skill_learning.queue import QueueEntry
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

type _Outcome = Literal["reviewed", "failed", "interrupted"]


def _skills_root(config: Config, runtime_paths: RuntimePaths, entry: QueueEntry) -> Path:
    """Return the workspace skills directory that this conversation's reviews maintain."""
    runtime = resolve_agent_runtime(entry.agent, config, runtime_paths, execution_identity=entry.execution_identity())
    workspace_root = runtime.workspace.root if runtime.workspace is not None else None
    return agent_workspace_skills_root(runtime_paths, entry.agent, workspace_root=workspace_root)


async def _cancel(tasks: list[asyncio.Task[None]]) -> None:
    """Stop reviews and wait until each has settled its count."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@dataclass
class SkillReviewRunner:
    """Own the process's running skill reviews, at most one per conversation."""

    runtime_paths: RuntimePaths
    client_provider: Callable[[str], nio.AsyncClient | None]
    _reviews: dict[str, tuple[str, asyncio.Task[None]]] = field(default_factory=dict, init=False)
    _stopped: bool = field(default=False, init=False)

    def start(
        self,
        config: Config,
        key: str,
        entry: QueueEntry,
        captured: CapturedRequest | None,
    ) -> asyncio.Task[None] | None:
        """Review a conversation whose count reached the interval, unless a review of it already runs.

        After shutdown began, responses still finishing leave their count for the next start instead.
        """
        running = self._reviews.get(key)
        if self._stopped or (running is not None and not running[1].done()):
            return None
        task = create_background_task(
            self._review(config, key, entry, captured),
            name=f"skill_review:{entry.agent}",
            # The response's context carries its queued-message and mid-turn state, which the reused model's
            # hooks would otherwise apply to the review's requests.
            context=contextvars.Context(),
        )
        self._reviews[key] = (entry.agent, task)
        task.add_done_callback(lambda done: self._forget(key, done))
        return task

    def cancel(self, key: str) -> None:
        """Stop a conversation's review because a response starts in it; its count stays for the next reply."""
        if (running := self._reviews.get(key)) is not None:
            running[1].cancel()

    async def retire(self, config: Config) -> None:
        """Stop the reviews of agents that no longer learn skills, once each has settled its count."""
        await _cancel(
            [
                task
                for agent_name, task in self._reviews.values()
                if (agent := config.agents.get(agent_name)) is None or not agent.skill_learning.enabled
            ],
        )

    async def stop(self) -> None:
        """Stop every review; the queue is durable, so a review that changed nothing runs after the next reply."""
        self._stopped = True
        await _cancel([task for _agent_name, task in self._reviews.values()])

    def _forget(self, key: str, task: asyncio.Task[None]) -> None:
        if (running := self._reviews.get(key)) is not None and running[1] is task:
            del self._reviews[key]

    async def _review(self, config: Config, key: str, entry: QueueEntry, captured: CapturedRequest | None) -> None:
        settings = config.agents[entry.agent].skill_learning
        identity = entry.execution_identity()
        progress = ReviewProgress()
        outcome: _Outcome = "reviewed"
        stopped: asyncio.CancelledError | None = None
        try:
            skills_root = await asyncio.to_thread(_skills_root, config, self.runtime_paths, entry)
            # The lock lives in the storage root, because the primary takes no lock inside a workspace worker code shares.
            lock_name = f"{hashlib.sha256(str(skills_root).encode()).hexdigest()[:32]}.lock"
            async with async_exclusive_file_lock(self.runtime_paths.storage_root / "skill_learning_locks" / lock_name):
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
                    logger.info("Archived unused learned skills", agent=entry.agent, archived=archived)
                await asyncio.wait_for(
                    review_conversation(
                        config=config,
                        runtime_paths=self.runtime_paths,
                        agent_name=entry.agent,
                        session_id=entry.session,
                        identity=identity,
                        skills_root=skills_root,
                        captured=captured,
                        progress=progress,
                    ),
                    timeout=settings.timeout_seconds,
                )
        except asyncio.CancelledError as error:
            stopped, outcome = error, "interrupted"
        except Exception:
            outcome = "failed"
            logger.exception("Skill review failed", agent=entry.agent, session_id=entry.session)
        # Archival and writes land before a timeout or a stop goes through, so every exit, a stop included, settles the
        # count with every change the learner made.
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
            elif settings.notify and progress.changes and identity is not None and not self._stopped:
                # A new response or a config change stopped the review after its writes landed.
                await self._notify(entry.agent, identity, progress.changes)
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

    async def _finish(self, key: str, claimed: QueueEntry, progress: ReviewProgress, outcome: _Outcome) -> _Outcome:
        if progress.changes:
            # Like Hermes' best-effort review, one that already changed skills is done; rerunning the same
            # conversation would repeat its edits and notices.
            outcome = "reviewed"
        await asyncio.to_thread(settle_review, self.runtime_paths, key, claimed=claimed, outcome=outcome)
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
            # The review is already settled; a lost notice must not make it look failed.
            logger.exception("Could not post skill review notice", agent=agent_name, room_id=identity.room_id)
            return
        if delivered is None:
            logger.warning("Could not post skill review notice", agent=agent_name, room_id=identity.room_id)
