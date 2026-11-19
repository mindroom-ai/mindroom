"""Where one inbound event came from, classified once at admission, and the one command rule."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.commands.parsing import command_parser
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.dispatch_source import (
    MESSAGE_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_SOURCE_KIND,
    is_visible_router_voice_echo_content,
)
from mindroom.turn_origin import TurnOrigin, classify_turn_origin

if TYPE_CHECKING:
    from mindroom.commands.parsing import Command

_USER_RELAY_SOURCE_KINDS: frozenset[str] = frozenset({TRUSTED_INTERNAL_RELAY_SOURCE_KIND, VOICE_SOURCE_KIND})

# Who a turn runs as, plus the sender that stays its speaker when it wrote for that requester.
type RunIdentity = tuple[str, str | None]


@dataclass(frozen=True, slots=True)
class IngressFacts:
    """The provenance one inbound event carries, read from the event and the entity registry before precheck.

    Each field is independent evidence; who the turn runs as and how far its relay
    metadata is trusted derive from them. The physical sender stays the event's
    transport sender for every sender-based mechanic, the lane requester orders
    lanes and coalescing, and the run requester is who the turn runs as.
    """

    physical_sender: str
    source_kind: str
    # The requester-promotion result, alias-resolved, or the owner of an agent party in its contact room: lanes,
    # coalescing keys, and the self-echo drop use it.
    lane_requester_id: str
    # Trusted relay metadata: a human or configured bot account a managed sender relayed.
    original_sender: str | None = None
    original_sender_is_human: bool = False
    # Whom another sender's message runs for: the human or bot account a managed non-router sender wrote this
    # reply for, or the owner of an agent party in its contact room when someone else wrote it.
    acting_requester_id: str | None = None
    hook_source: str | None = None
    message_received_depth: int = 0
    # The person's events a steward's hidden route names: its responder answers the last one.
    routed_event_ids: tuple[str, ...] = ()
    # The earlier event this event's source answers for: the last routed message or a finalized reply.
    discovery_event_id: str | None = None
    sender_entity_name: str | None = None
    # The entity behind the run requester, if one is.
    requester_entity_name: str | None = None
    # The names the agents and teams this event's pills mention can read as in its text.
    mention_names: tuple[str, ...] = ()
    # Whether this event's pills name the owner account and no agent or team: it is aimed at nobody who answers.
    aimed_at_owner: bool = False
    # Whether this is a contact room of the responder that refuses the sender: only the counterparty and the
    # responder's owner start an active contact's turns, and a removed contact's room starts none.
    contact_refused: bool = False

    @property
    def routed_event_id(self) -> str | None:
        """Return the person's event a route's responder answers, or None for an event no steward routed."""
        return self.routed_event_ids[-1] if self.routed_event_ids else None

    @property
    def discovery_event_ids(self) -> tuple[str, ...]:
        """Return every earlier event a turn for this event also owns, so their edits and deletions find it."""
        return self.routed_event_ids or tuple(filter(None, (self.discovery_event_id,)))

    @property
    def run_requester_id(self) -> str:
        """Return who the turn runs as: the acting requester when an entity wrote it for one, else the lane requester."""
        return self.acting_requester_id or self.lane_requester_id

    @property
    def acts_for_requester(self) -> bool:
        """Return whether the turn runs for a requester other than the sender, so the sender stays its speaker."""
        return self.acting_requester_id is not None

    @property
    def run_identity(self) -> RunIdentity:
        """Return who the turn runs as, plus its speaker when the sender wrote it for that requester.

        A coalesced batch runs as one requester and takes its origin from its latest event, so comparing run identities
        keeps a reply an entity wrote for a human out of a batch with that human's own messages.
        """
        return self.run_requester_id, self.physical_sender if self.acts_for_requester else None

    @property
    def sender_is_managed(self) -> bool:
        """Return whether the entity registry manages the sender, which is what trusts its metadata."""
        return self.sender_entity_name is not None

    @property
    def trusted_user_relay(self) -> bool:
        """Return whether a managed sender relayed a human's message."""
        return self.original_sender_is_human and self.source_kind in _USER_RELAY_SOURCE_KINDS

    # LEGACY_COMPAT: visible router relay messages ("@agent could you help with this?") already in room history.
    # Legacy format: an `m.room.message` the router account sent with the trusted-relay source kind and
    # `com.mindroom.original_sender`, which mentions the agent the router chose and replies to the person's message.
    # Last legacy release: v2026.11.396, the newest release when visible relays stopped; the next, still unreleased,
    # release sends a hidden `io.mindroom.route` event from the room's steward instead.
    # Handling: every message of the router account still counts as routed, so the mentioned agent answers such a
    # relay for its original sender, in reply to the relay, exactly as before.
    # Coverage: tests/test_turns_focused.py::test_old_router_relay_event_still_answers_from_relay_context
    @property
    def routed(self) -> bool:
        """Return whether the room's steward routed this event to its responder."""
        return bool(self.routed_event_ids) or self.sender_entity_name == ROUTER_AGENT_NAME

    @property
    def router_relay(self) -> bool:
        """Return whether a route relays a human's or a configured bot account's message."""
        return (
            self.routed
            and self.source_kind == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
            and (self.original_sender is not None)
        )

    @property
    def origin(self) -> TurnOrigin:
        """Return the canonical turn origin these facts describe."""
        return classify_turn_origin(
            transport_sender_id=self.physical_sender,
            requester_id=self.run_requester_id,
            sender_entity_name=self.sender_entity_name,
            requester_entity_name=self.requester_entity_name,
            source_kind=self.source_kind,
            original_sender=self.original_sender,
            trusted_user_relay=self.trusted_user_relay,
            acts_for_requester=self.acts_for_requester,
            routed=self.routed,
        )


def is_voice_echo(facts: IngressFacts, content: object) -> bool:
    """Return whether content is a managed account's display-only transcript of a human's or bot account's audio."""
    # LEGACY_COMPAT: voice transcript echoes the router account posted, already in room history.
    # Legacy format: an `m.room.message` of the router account with `com.mindroom.visible_router_voice_echo` true,
    # the trusted-relay source kind and `com.mindroom.original_sender`; first a "transcribing" placeholder, then
    # edited to the transcript or the fallback text.
    # Last legacy release: v2026.11.396, the newest release when the router stopped posting echoes; the next, still
    # unreleased, release has the answering agent post the transcript under the same content key.
    # Handling: no router branch. Old and new echoes are read by this one rule, as display-only and never a turn.
    # Coverage: tests/test_turns_focused.py::test_old_router_echo_is_not_a_turn.
    return facts.original_sender is not None and is_visible_router_voice_echo_content(content)


def command_for(facts: IngressFacts, command_text: str, source_count: int = 1) -> Command | None:
    """Return the command one turn's text runs as, or None when the text is conversation.

    Only an ordinary message runs as a command, under its run requester's
    authority. Text a schedule, a hook, a trigger, or a relay produced is
    conversation: an agent or a plugin can write it, yet it would run with a
    person's authority. ``command_text`` is ``dispatch_prompt_for_event`` of the
    event's logical content, so a finished reply's tool markers never hide a
    command. A turn of several sources carries no command, and neither does a
    message aimed at the owner account alone: no agent runs it as its own.
    """
    if source_count != 1 or facts.source_kind != MESSAGE_SOURCE_KIND or facts.aimed_at_owner:
        return None
    return command_parser.parse(command_text, facts.mention_names)
