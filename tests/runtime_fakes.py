"""The runtime a test bot belongs to: the production orchestrator, ready, with test-controlled fleet liveness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.agent_reply_membership_sync import AgentReplyMembershipSync
from mindroom.orchestration.config_updates import configured_entity_names
from mindroom.orchestrator import _MultiAgentOrchestrator

if TYPE_CHECKING:
    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import EventJournalStore


@dataclass
class ReadyRuntime(_MultiAgentOrchestrator):
    """A started, ready runtime whose fleet liveness the test decides.

    Bot tests drive bots without starting them, so liveness does not come from registered bots: every configured
    entity counts as running and first-synced unless `unavailable` or `not_ready` names it, and unknown names stay
    unavailable. Liveness never grants room access; reply membership stays the index's decision.
    """

    unavailable: set[str] = field(default_factory=set)
    not_ready: set[str] = field(default_factory=set)
    journal_store: EventJournalStore | None = None

    def __post_init__(self) -> None:
        """Mark the runtime started and ready, as it is once startup published its config."""
        super().__post_init__()
        self.running = True
        self._runtime_ready_event.set()

    def use_memberships(
        self,
        memberships: AgentReplyMembershipIndex,
        sync: AgentReplyMembershipSync | None = None,
    ) -> None:
        """Make this runtime's reply-membership index (and the router's sync coordinator) the given ones."""
        self.agent_reply_memberships = memberships
        self.agent_reply_membership_sync = sync if sync is not None else AgentReplyMembershipSync(memberships)

    def running_entity_names(self) -> frozenset[str]:
        """Return every configured entity the test did not mark unavailable."""
        return frozenset(configured_entity_names(self.config)) - self.unavailable

    def entity_first_sync_complete(self, entity_name: str) -> bool | None:
        """Return readiness by the test's marks for configured entities, and None for unknown ones."""
        if entity_name not in configured_entity_names(self.config):
            return None
        return entity_name not in self.unavailable and entity_name not in self.not_ready

    def event_journal_store(self) -> EventJournalStore:
        """Return the store the test handed in, or the runtime's own."""
        return self.journal_store if self.journal_store is not None else super().event_journal_store()


def ready_runtime(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    journal_store: EventJournalStore | None = None,
    agent_reply_memberships: AgentReplyMembershipIndex | None = None,
    agent_reply_membership_sync: AgentReplyMembershipSync | None = None,
) -> ReadyRuntime:
    """Build one ready test runtime around ``config``."""
    runtime = ReadyRuntime(runtime_paths=runtime_paths, config=config, journal_store=journal_store)
    if agent_reply_memberships is not None:
        runtime.use_memberships(agent_reply_memberships, agent_reply_membership_sync)
    return runtime
