"""Background worker that counts completed runs and reviews conversations once they reach the interval."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from agno.db.utils import deserialize_run
from agno.run.agent import RunOutput

from mindroom.agent_storage import create_session_storage, load_agent_session
from mindroom.background_loop import run_until_stopped
from mindroom.constants import SKILL_REVIEW_NOTICE_CONTENT_KEY, SKIP_MENTIONS_KEY
from mindroom.file_locks import async_exclusive_file_lock
from mindroom.history_run_visibility import is_model_history_visible_run
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.message_builder import build_message_content
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.skill_learning.library import archive_unused_skills, skills_fingerprint
from mindroom.skill_learning.queue import (
    SKILL_LEARNING_WAKE,
    QueueEntry,
    RunPosition,
    claim_due_reviews,
    record_count,
    settle_review,
)
from mindroom.skill_learning.reviewer import ReviewProgress, review_conversation
from mindroom.skill_learning.transcript import conversation_messages, count_model_replies
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


_NO_RUN: RunPosition = (-1, -1)


def _conversation_runs(
    config: Config,
    runtime_paths: RuntimePaths,
    entry: QueueEntry,
) -> list[tuple[RunPosition, RunOutput]]:
    """Return this conversation's model-visible runs with their creation positions, oldest first."""
    storage = create_session_storage(entry.agent, config, runtime_paths, execution_identity=entry.execution_identity())
    try:
        rows, _total = cast(
            "tuple[list[dict[str, Any]], int]",
            storage.get_runs(session_id=entry.session, deserialize=False),
        )
    finally:
        storage.close()
    runs: list[tuple[RunPosition, RunOutput]] = []
    for row in rows:
        run = deserialize_run(row.get("run_type"), row["run_data"])
        if is_model_history_visible_run(run) and isinstance(run, RunOutput):
            runs.append(((int(row["created_at"]), int(row["run_index"])), run))
    return sorted(runs, key=lambda item: item[0])


@dataclass
class SkillLearningWorker:
    """Serialize reviews across processes sharing one storage root; queued runs survive restarts."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    client_provider: Callable[[str], nio.AsyncClient | None]
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)

    def stop(self) -> None:
        """Stop at once: the queue is durable, so an interrupted count or review runs again after restart."""
        self._stop_event.set()
        self._wake_event.set()
        if self._task is not None:
            self._task.cancel()

    async def run(self) -> None:
        """Run review cycles until stopped, waking early whenever a run is queued."""
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
            reviews_left = _MAX_REVIEWS_PER_CYCLE
            for key, entry in due:
                try:
                    replies, newest, skills_root = await self._count(config, key, entry)
                    if reviews_left and replies >= config.agents[entry.agent].skill_learning.review_interval:
                        reviews_left -= 1
                        await self._review(config, key, entry, skills_root, newest)
                except Exception:
                    logger.exception("Skill learning failed", agent=entry.agent, session_id=entry.session)
                    await asyncio.to_thread(
                        settle_review,
                        self.runtime_paths,
                        key,
                        outcome="failed",
                        now=time.time(),
                    )

    async def _count(self, config: Config, key: str, entry: QueueEntry) -> tuple[int, RunPosition, Path]:
        """Return the model replies since the conversation's marker, its newest run position, and its skills root."""
        runtime = await asyncio.to_thread(
            resolve_agent_runtime,
            entry.agent,
            config,
            self.runtime_paths,
            entry.execution_identity(),
        )
        workspace_root = runtime.workspace.root if runtime.workspace is not None else None
        skills_root = agent_workspace_skills_root(self.runtime_paths, entry.agent, workspace_root=workspace_root)
        runs = await asyncio.to_thread(_conversation_runs, config, self.runtime_paths, entry)
        fingerprint = await asyncio.to_thread(skills_fingerprint, skills_root)
        positions = [_NO_RUN, *(position for position, _run in runs)]
        newest = positions[-1]
        # The count starts just before the response that created the entry; without it, at the newest run.
        start = next(
            (positions[number] for number, (_position, run) in enumerate(runs) if run.run_id == entry.first_run_id),
            newest,
        )
        reviewed_through = await asyncio.to_thread(
            record_count,
            self.runtime_paths,
            key,
            claimed=entry,
            start=start,
            newest=newest,
            skills_root=str(skills_root),
            fingerprint=fingerprint,
        )
        replies = count_model_replies(run for position, run in runs if position > reviewed_through)
        return replies, newest, skills_root

    async def _review(
        self,
        config: Config,
        key: str,
        entry: QueueEntry,
        skills_root: Path,
        through: RunPosition,
    ) -> None:
        settings = config.agents[entry.agent].skill_learning
        identity = entry.execution_identity()
        before = await asyncio.to_thread(skills_fingerprint, skills_root)
        progress = ReviewProgress()
        outcome: Literal["reviewed", "failed"] = "reviewed"
        try:
            archived = await asyncio.to_thread(
                archive_unused_skills,
                skills_root,
                archive_after_days=settings.archive_after_days,
                now=datetime.now(UTC),
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
        except asyncio.CancelledError:
            # Shutdown keeps the counter for a retry but still records the learner's partial writes as its own;
            # a bookkeeping error here must not replace the cancellation.
            try:
                self._settle(key, skills_root, before, outcome="interrupted", through=through)
            except Exception:
                logger.exception("Could not record an interrupted skill review", agent=entry.agent)
            raise
        except Exception:
            outcome = "failed"
            logger.exception("Skill review failed", agent=entry.agent, session_id=entry.session)
        # A timeout cancels the review, not the file writes it started; they land before the state is recorded.
        await progress.settled()
        if progress.changes:
            # Like Hermes' best-effort review, one that already changed skills is done; rerunning the same
            # conversation would repeat its edits and notices.
            outcome = "reviewed"
        await asyncio.to_thread(self._settle, key, skills_root, before, outcome=outcome, through=through)
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

    def _settle(
        self,
        key: str,
        skills_root: Path,
        before: str,
        *,
        outcome: Literal["reviewed", "failed", "interrupted"],
        through: RunPosition,
    ) -> None:
        after = skills_fingerprint(skills_root)
        settle_review(
            self.runtime_paths,
            key,
            outcome=outcome,
            now=time.time(),
            through=through,
            learner_change=(before, after) if after != before else None,
        )

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
