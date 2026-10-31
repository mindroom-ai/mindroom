"""Coordinate personal-room triggers, recovery, and retention across live agents."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING

import nio

from mindroom.background_tasks import create_background_task
from mindroom.config.personal_agents import read_personal_agent_roster
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import get_room_members
from mindroom.matrix.personal_room_store import (
    personal_room_cleanup_exclusions,
    personal_room_record_path,
    read_personal_room,
    retained_personal_rooms,
)
from mindroom.matrix.personal_rooms import PersonalRoomRosterMismatchError, PersonalRoomValidationError
from mindroom.matrix.state import resolve_room_aliases
from mindroom.personal_agents import is_mindroom_account_id, provision_personal_agent
from mindroom.requester_identity import is_human_requester_id, resolve_human_requester_alias

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.personal_rooms import PersonalRoomsConfig
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.personal_rooms import PersonalRoomService
    from mindroom.matrix.room_member_joins import RoomMemberJoin
    from mindroom.orchestration.config_lifecycle import ConfigReloadLifecycle
    from mindroom.runtime_protocols import SupportsClientConfig


logger = get_logger(__name__)
_RECONCILIATION_RETRY_SECONDS = 30.0
# A candidate that keeps failing, such as a room waiting for a person to fix its
# membership, is retried after doubling delays up to this cap.
_RECONCILIATION_MAX_RETRY_SECONDS = 3600.0


class _TargetNotReadyError(RuntimeError):
    """The personal-room agent is not connected yet."""


class _TargetPendingError(RuntimeError):
    """The requester's own personal agent is not in the live config or not connected yet."""


@dataclass(frozen=True)
class _CandidateBackoff:
    """One candidate's consecutive reconciliation failures and when it is next due."""

    failures: int
    delay_seconds: float
    retry_at: float
    error_type: type[Exception]


def _next_backoff(previous: _CandidateBackoff | None, error: Exception) -> _CandidateBackoff:
    if previous is None:
        failures, delay = 1, _RECONCILIATION_RETRY_SECONDS
    else:
        failures, delay = previous.failures + 1, min(2 * previous.delay_seconds, _RECONCILIATION_MAX_RETRY_SECONDS)
    return _CandidateBackoff(failures, delay, monotonic() + delay, type(error))


@dataclass(frozen=True)
class PersonalRoomTarget:
    """A connected owner's service and its separate startup readiness projection."""

    service: PersonalRoomService
    first_sync_complete: bool


@dataclass
class PersonalRoomLifecycle:
    """Own feature policy while consuming narrow runtime and live-service projections."""

    agent_name: str
    runtime: SupportsClientConfig
    runtime_paths: RuntimePaths
    service: PersonalRoomService
    lookup_target: Callable[[str], PersonalRoomTarget | None]
    # Starts a user's agent once provisioning added it to the roster.
    config_reload: ConfigReloadLifecycle
    # Tells the router that one room's membership changed, which may unblock a personal room's imported roster.
    report_membership_change: Callable[[str], None]
    # A live join and a reconciliation pass can both reach one user; only one may register the account.
    _provisioning: set[tuple[str, str]] = field(default_factory=set, init=False)
    _reconciled: bool = field(default=False, init=False)
    _config_revision: int = field(default=0, init=False)
    _completed_candidates: set[tuple[str, str]] = field(default_factory=set, init=False)
    _candidate_backoff: dict[tuple[str, str], _CandidateBackoff] = field(default_factory=dict, init=False)
    # Live triggers that failed, with whether they asked to re-invite a departed owner.
    _retry_candidates: dict[tuple[str, str], bool] = field(default_factory=dict, init=False)
    # The personal room and unattested members each candidate last reported, warned about only when they change.
    # Kept across reloads, which each user's provisioning from a template requests.
    _roster_mismatches: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = field(default_factory=dict, init=False)
    # Candidates whose personal room's membership changed since their last attempt, retried without their delay.
    _membership_changed: set[tuple[str, str]] = field(default_factory=set, init=False)
    # Rooms whose membership changed during the reconciliation attempt in flight, if one is.
    _changes_during_attempt: set[str] | None = field(default=None, init=False)
    _reconciliation_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _next_reconciliation_at: float = field(default=0.0, init=False)
    _rerun_after_pass: bool = field(default=False, init=False)

    @property
    def observes_onboarding_joins(self) -> bool:
        """Whether router baseline joins need durable admission for this feature."""
        return self.agent_name == ROUTER_AGENT_NAME and self.runtime.config.personal_rooms is not None

    def config_changed(self) -> None:
        """Revisit existing records and optional backfill after a configuration reload."""
        self._config_revision += 1
        self._reconciled = False
        self._completed_candidates.clear()
        self._candidate_backoff.clear()
        self._next_reconciliation_at = 0.0

    def target_ready(self) -> None:
        """Reconcile at once because a user's own agent has just finished its first sync."""
        self._reconcile_now()

    def membership_changed(self, room_id: str) -> None:
        """Retry at once each requester whose personal room is this room and failed its imported-roster check.

        Passes this starts have no minimum gap, but at most one runs and one more waits.
        """
        if self._changes_during_attempt is not None:
            self._changes_during_attempt.add(room_id)
        self._retry_at_once({candidate for candidate, (room, _) in self._roster_mismatches.items() if room == room_id})

    def _retry_at_once(self, candidates: set[tuple[str, str]]) -> None:
        """Mark candidates to retry without their delay; every bot in a room reports its events, so mark each once."""
        new = candidates - self._membership_changed
        if new:
            self._membership_changed |= new
            self._reconcile_now()

    def _reconcile_now(self) -> None:
        self._reconciled = False
        self._next_reconciliation_at = 0.0
        # A running pass may have read the old state, so another pass follows it without the cooldown.
        self._rerun_after_pass = self._reconciliation_task is not None
        self.schedule_reconciliation()

    def _uses_template(self, settings: PersonalRoomsConfig) -> bool:
        return settings.agent in self.runtime.config.personal_agents

    def _target_name(self, settings: PersonalRoomsConfig, user_id: str) -> str | None:
        """Return the agent that serves one user's personal room, or None before their own agent exists."""
        if not self._uses_template(settings):
            return settings.agent
        return self.runtime.config.personal_agent_name(settings.agent, user_id)

    def _reconciliation_ready(self, settings: PersonalRoomsConfig) -> bool:
        """Whether a pass can start; each user's own agent is checked per candidate instead."""
        if self.runtime.client is None:
            return False
        if self._uses_template(settings):
            return True
        target = self.lookup_target(settings.agent)
        return target is not None and target.first_sync_complete

    def schedule_reconciliation(self) -> None:
        """Start at most one maintenance pass without delaying durable sync admission."""
        if (
            self._reconciled
            or not self.observes_onboarding_joins
            or self._reconciliation_task is not None
            or monotonic() < self._next_reconciliation_at
        ):
            return
        settings = self.runtime.config.personal_rooms
        assert settings is not None
        if not self._reconciliation_ready(settings):
            return
        self._reconciliation_task = create_background_task(
            self._run_reconciliation(),
            name=f"personal_room_reconciliation_{self.agent_name}",
            owner=self.runtime,
        )

    async def _run_reconciliation(self) -> None:
        revision = self._config_revision
        cancelled = False
        try:
            await self._reconcile()
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            nudged, self._rerun_after_pass = self._rerun_after_pass, False
            if not cancelled and not nudged and revision == self._config_revision:
                self._next_reconciliation_at = monotonic() + _RECONCILIATION_RETRY_SECONDS
            self._reconciliation_task = None
            if nudged and not cancelled:
                self._reconcile_now()

    async def cancel_reconciliation(self, *, timeout_seconds: float) -> None:
        """Cancel within the shutdown budget, retaining ownership of unfinished work."""
        task = self._reconciliation_task
        if task is not None:
            task.cancel()
            done, _ = await asyncio.wait((task,), timeout=timeout_seconds)
            if task in done:
                await asyncio.gather(task, return_exceptions=True)
                self._reconciliation_task = None
        self._next_reconciliation_at = 0.0
        await self.service.cancel_guest_removal_retries(timeout_seconds=timeout_seconds)

    async def _onboard(
        self,
        user_id: str,
        source_room_id: str,
        *,
        reinvite_departed_owner: bool = False,
    ) -> None:
        """Forward a trusted router observation; unavailable owners leave it retryable.

        When personal_rooms.agent names a template, the requester's own agent
        serves the room: a missing one is provisioned, and until it is in the
        live config, connected, and past its first sync the trigger is pending.
        An unsynced agent's membership changes wait for its sync to catch up,
        which would stall the pass for every other user.
        """
        settings = self.runtime.config.personal_rooms
        if (
            not self.observes_onboarding_joins
            or settings is None
            or source_room_id not in resolve_room_aliases(settings.onboarding_rooms, self.runtime_paths)
            or not is_human_requester_id(user_id, self.runtime.config, self.runtime_paths)
        ):
            return
        # A bridge alias shares its person's agent; the personal room stays keyed by the joining account.
        owner_id = resolve_human_requester_alias(user_id, self.runtime.config, self.runtime_paths)
        target_name = self._target_name(settings, owner_id)
        if target_name is None:
            # A new agent's account can join the lobby before the reload that makes it known.
            if is_mindroom_account_id(user_id, self.runtime_paths):
                logger.debug("MindRoom account gets no personal agent", user_id=user_id, room_id=source_room_id)
                return
            await self._provision_personal_agent(
                settings.agent,
                user_id=owner_id,
                source_user_id=user_id,
                source_room_id=source_room_id,
            )
            msg = "Personal agent is not in the live config yet"
            raise _TargetPendingError(msg)
        target = self.lookup_target(target_name)
        if self._uses_template(settings):
            if target is None or not target.first_sync_complete or self.runtime.client is None:
                msg = "Personal agent is not connected and synced yet"
                raise _TargetPendingError(msg)
        elif target is None or self.runtime.client is None:
            msg = "Personal-room target is not ready"
            raise _TargetNotReadyError(msg)
        await target.service.ensure(
            user_id,
            source_room_id,
            self.runtime.client,
            reinvite_departed_owner=reinvite_departed_owner,
        )

    async def _provision_personal_agent(
        self,
        template: str,
        *,
        user_id: str,
        source_user_id: str,
        source_room_id: str,
    ) -> None:
        """Create one user's agent from a template and request a reload so the runtime starts it.

        A second call for a user already being provisioned returns at once, so the caller treats the user as pending
        and retries without waiting.
        """
        client = self.runtime.client
        assert client is not None
        key = (template, user_id)
        if key in self._provisioning:
            return
        self._provisioning.add(key)
        try:
            profile = await client.get_displayname(user_id)
            _, changed = await provision_personal_agent(
                self.runtime.config,
                self.runtime_paths,
                template=template,
                user_id=user_id,
                user_display_name=profile.displayname
                if isinstance(profile, nio.ProfileGetDisplayNameResponse)
                else None,
                source_user_id=source_user_id,
                source_room_id=source_room_id,
            )
        finally:
            self._provisioning.discard(key)
        # This runs only while the live config lacks the agent, so an existing entry still needs a reload,
        # but re-requesting a queued one would restart its debounce and response drain on every retry.
        if changed or self.config_reload.status.status != "pending":
            self.config_reload.request_reload()

    async def _onboard_live(
        self,
        user_id: str,
        source_room_id: str,
        *,
        reinvite_departed_owner: bool = False,
    ) -> None:
        """Serve one onboarding-room trigger without letting a broken personal room hold that room's lane.

        A room no retry can fix is logged and the event settles without queuing
        a retry: a room whose reconciliation is still failing keeps retrying
        after doubling delays up to hourly; otherwise the room is checked again
        on restart or configuration reload. Any other failure, such as a
        requester whose server refuses the invite, also settles the event and
        hands the trigger to reconciliation, which retries it after doubling
        delays up to hourly. A personal-room agent that is not connected yet
        still raises, so the journal keeps the trigger across restarts until
        that agent is ready. A requester's own agent that is still starting
        settles the event and hands the trigger to reconciliation without a
        delay, and that agent's first sync asks for a pass at once.
        """
        candidate = (user_id, source_room_id)
        try:
            await self._onboard(user_id, source_room_id, reinvite_departed_owner=reinvite_departed_owner)
        except _TargetNotReadyError:
            raise
        except _TargetPendingError:
            logger.info("Personal agent is starting", user_id=user_id, room_id=source_room_id)
            self._queue_retry(candidate, reinvite_departed_owner=reinvite_departed_owner)
        except PersonalRoomValidationError as error:
            logger.warning(
                "Personal-room validation failed for an onboarding trigger",
                user_id=user_id,
                room_id=source_room_id,
                error=str(error),
            )
        except Exception as error:
            logger.warning(
                "Personal-room onboarding trigger failed",
                user_id=user_id,
                room_id=source_room_id,
                error_type=type(error).__name__,
                error=str(error),
            )
            self._queue_retry(candidate, reinvite_departed_owner=reinvite_departed_owner)
        else:
            self._roster_mismatches.pop(candidate, None)
            self._settle_retry(candidate, reinvite_departed_owner=reinvite_departed_owner)

    def _queue_retry(self, candidate: tuple[str, str], *, reinvite_departed_owner: bool) -> None:
        """Hand a live trigger that did not finish to the next reconciliation pass."""
        self._retry_candidates[candidate] = reinvite_departed_owner or self._retry_candidates.get(candidate, False)
        self._completed_candidates.discard(candidate)
        self._reconciled = False

    def _settle_retry(self, candidate: tuple[str, str], *, reinvite_departed_owner: bool) -> bool:
        """Forget a failed trigger's retry once a successful attempt covered it, and say whether it did.

        An attempt without re-invite authority does not cover a stored request
        to re-invite a departed owner, so that request stays pending.
        """
        if self._retry_candidates.get(candidate, False) and not reinvite_departed_owner:
            return False
        self._retry_candidates.pop(candidate, None)
        return True

    async def handle_command(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        requester_user_id: str,
    ) -> bool:
        """Recognize exact self-onboarding commands; ``requester_user_id`` is the event's trusted requester."""
        settings = self.runtime.config.personal_rooms
        if (
            not self.observes_onboarding_joins
            or settings is None
            or event.body.strip() not in settings.commands
            or room.room_id not in resolve_room_aliases(settings.onboarding_rooms, self.runtime_paths)
        ):
            return False
        if is_human_requester_id(
            event.sender,
            self.runtime.config,
            self.runtime_paths,
        ) and requester_user_id == resolve_human_requester_alias(
            event.sender,
            self.runtime.config,
            self.runtime_paths,
        ):
            await self._onboard_live(event.sender, room.room_id)
        return True

    async def member_event(self, room: nio.MatrixRoom, event: nio.RoomMemberEvent) -> None:
        """Finish local welcomes and route definite joins without bypassing baseline admission.

        An owner's event in a room no retry can fix, such as an imported room holding someone outside its attested
        roster, is logged and settles: raising would hold the room's event lane, so this agent would answer nothing
        there until someone repaired the room. A room whose reconciliation is still failing keeps retrying after
        doubling delays up to hourly; otherwise the room is checked again on restart or configuration reload.
        Any change of membership, unlike a profile update, also tells the router, which retries at once onboarding
        that this room's imported roster blocked.
        """
        if event.membership != event.prev_membership:
            self.report_membership_change(room.room_id)
        if event.membership in {"join", "leave", "ban"}:
            try:
                await self.service.owner_membership_event(room.room_id, event.state_key, event.membership)
            except PersonalRoomValidationError as error:
                logger.warning(
                    "Personal-room validation failed for an owner membership event",
                    user_id=event.state_key,
                    room_id=room.room_id,
                    error_type=type(error).__name__,
                    error=str(error),
                )
        await self.service.guest_membership_event(room, event.state_key, event.membership)
        if self.runtime.config.personal_rooms is None or event.membership != "join" or event.prev_membership == "join":
            return
        if self.observes_onboarding_joins and event.prev_membership is not None:
            await self._onboard_live(
                event.state_key,
                room.room_id,
                reinvite_departed_owner=event.prev_membership == "leave",
            )

    async def baseline_join(self, join: RoomMemberJoin) -> None:
        """Onboard unknown prior membership only after the existing durable baseline gate."""
        if join.prev_membership is None:
            await self._onboard_live(join.user_id, join.room_id)

    def _recorded_candidates(self, agent_name: str) -> tuple[set[tuple[str, str]], bool]:
        """Read each retained intent independently; keep damaged files retryable."""
        candidates: set[tuple[str, str]] = set()
        failed = False
        directory = personal_room_record_path(self.runtime_paths, agent_name, "").parent
        try:
            paths = list(directory.glob("*.json"))
        except OSError:
            logger.exception("Personal-room records unavailable", agent=agent_name)
            return candidates, True
        for path in paths:
            try:
                record = read_personal_room(path)
            except Exception:
                logger.exception("Personal-room record invalid", record=path.name)
                failed = True
                continue
            if record is not None:
                candidates.add((record.user_id, record.resume_source_room_id or record.source_room_id))
        return candidates, failed

    def _recorded_intent(self, settings: PersonalRoomsConfig) -> tuple[set[tuple[str, str]], bool]:
        """Collect retained intent of the target, or of every user's own agent from a template."""
        if not self._uses_template(settings):
            return self._recorded_candidates(settings.agent)
        try:
            roster = read_personal_agent_roster(self.runtime_paths)
        except Exception:
            logger.exception("Personal agent roster unavailable")
            return set(), True
        candidates: set[tuple[str, str]] = set()
        failed = False
        for entry in roster:
            if entry.template != settings.agent:
                continue
            # A bridge alias's join is retried for the alias, whose membership is the one that onboarded.
            candidates.add((entry.source_user_id or entry.user_id, entry.source_room_id))
            recorded, recorded_failed = self._recorded_candidates(entry.agent_name)
            candidates.update(recorded)
            failed |= recorded_failed
        return candidates, failed

    async def _backfill_candidates(self, onboarding_rooms: list[str]) -> tuple[set[tuple[str, str]], bool]:
        """Collect lobby members without losing recorded intent when a lobby is unavailable."""
        assert self.runtime.client is not None
        candidates: set[tuple[str, str]] = set()
        failed = False
        for room_id in resolve_room_aliases(onboarding_rooms, self.runtime_paths):
            try:
                members = await get_room_members(self.runtime.client, room_id)
            except Exception:
                logger.exception("Personal-room backfill failed", room_id=room_id)
                failed = True
                continue
            if members is None:
                logger.error("Personal-room backfill membership unavailable", room_id=room_id)
                failed = True
                continue
            candidates.update((user_id, room_id) for user_id in members)
        return candidates, failed

    async def _reconcile(self) -> None:
        """Retry recorded intent and optional lobby backfill once the serving agents have synced.

        A static target must have synced before the pass starts; each user's
        own agent from a template is checked per candidate, which stays
        pending until that agent has synced.
        """
        revision = self._config_revision
        settings = self.runtime.config.personal_rooms
        if self._reconciled or not self.observes_onboarding_joins or settings is None:
            return
        if not self._reconciliation_ready(settings):
            return
        candidates, failed = await asyncio.to_thread(self._recorded_intent, settings)
        if settings.backfill:
            backfill, backfill_failed = await self._backfill_candidates(settings.onboarding_rooms)
            candidates.update(backfill)
            failed |= backfill_failed
        candidates.update(self._retry_candidates)
        if not failed:
            # Forget what requesters whose intent is gone last reported.
            self._roster_mismatches = {key: seen for key, seen in self._roster_mismatches.items() if key in candidates}
            self._membership_changed &= candidates
        for candidate in sorted(candidates - self._completed_candidates):
            if revision != self._config_revision:
                return
            if not await self._reconcile_candidate(candidate, revision):
                failed = True
        # A live trigger that failed during this pass is still owed a retry.
        if not failed and not self._retry_candidates and revision == self._config_revision:
            self._reconciled = True

    async def _reconcile_candidate(self, candidate: tuple[str, str], revision: int) -> bool:
        """Retry one candidate when due or its personal room's membership changed; False leaves it pending."""
        previous = self._candidate_backoff.get(candidate)
        if previous is not None and monotonic() < previous.retry_at and candidate not in self._membership_changed:
            return False
        # A change during this attempt may come after the state it reads, so it marks the candidate again.
        self._membership_changed.discard(candidate)
        user_id, room_id = candidate
        reinvite_departed_owner = self._retry_candidates.get(candidate, False)
        changed: set[str] = set()
        self._changes_during_attempt = changed
        try:
            await self._onboard(user_id, room_id, reinvite_departed_owner=reinvite_departed_owner)
        except _TargetPendingError:
            # The user's own agent is starting; its first sync asks for the next pass.
            return False
        except Exception as error:
            backoff = _next_backoff(previous, error)
            if revision == self._config_revision:
                self._candidate_backoff[candidate] = backoff
            retry = {"attempt": backoff.failures, "retry_in_seconds": backoff.delay_seconds}
            if isinstance(error, PersonalRoomRosterMismatchError):
                # Expected until a person changes the room, so no traceback, and a warning only when the members change.
                mismatch = (error.room_id, error.unexpected_user_ids)
                unchanged = self._roster_mismatches.get(candidate) == mismatch
                self._roster_mismatches[candidate] = mismatch
                (logger.debug if unchanged else logger.warning)(
                    "Personal-room imported roster has unattested members",
                    user_id=user_id,
                    room_id=room_id,
                    personal_room_id=error.room_id,
                    unexpected_user_ids=error.unexpected_user_ids,
                    **retry,
                )
                if error.room_id in changed:
                    # The room changed while this attempt read it, possibly before the router knew it was this room.
                    self._retry_at_once({candidate})
            elif previous is None or previous.error_type is not type(error):
                logger.exception("Personal-room reconciliation failed", user_id=user_id, room_id=room_id, **retry)
            else:
                logger.warning(
                    "Personal-room reconciliation failed",
                    user_id=user_id,
                    room_id=room_id,
                    error_type=type(error).__name__,
                    error=str(error),
                    **retry,
                )
            return False
        finally:
            self._changes_during_attempt = None
        self._roster_mismatches.pop(candidate, None)
        if revision == self._config_revision:
            self._candidate_backoff.pop(candidate, None)
            # A live trigger can fail during this attempt and ask for a re-invite it did not carry,
            # and a membership change during it may come after the roster it checked.
            if (
                self._settle_retry(candidate, reinvite_departed_owner=reinvite_departed_owner)
                and candidate not in self._membership_changed
            ):
                self._completed_candidates.add(candidate)
        return True

    def retained_room_ids(self) -> set[str]:
        """Return recorded rejoin authority, including when provisioning is disabled."""
        client = self.runtime.client
        return retained_personal_rooms(
            self.runtime_paths,
            self.agent_name,
            user_id=client.user_id if client is not None else None,
        )

    async def cleanup_exclusions(self) -> set[str]:
        """Protect interrupted creates conservatively without granting rejoin authority."""
        client = self.runtime.client
        if client is None:
            msg = "Matrix client is not ready for personal-room retention"
            raise RuntimeError(msg)
        return await personal_room_cleanup_exclusions(client, self.runtime_paths, self.agent_name)
