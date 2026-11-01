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
    # The earlier event a turn for this event also owns: a routed human message or a finalized reply.
    discovery_event_id: str | None = None
    sender_entity_name: str | None = None
    # The entity behind the run requester, if one is.
    requester_entity_name: str | None = None
    # The active contact whose room this is, when the responder is one of its agent parties.
    contact_id: str | None = None
    # Whether that contact refuses the sender: only the counterparty and the responder's owner start its turns.
    contact_refused: bool = False

    @property
    def run_requester_id(self) -> str:
        """Return who the turn runs as: the acting requester when an entity wrote it for one, else the lane requester."""
        return self.acting_requester_id or self.lane_requester_id

    @property
    def acts_for_requester(self) -> bool:
        """Return whether the turn runs for a requester other than the sender, so the sender stays its speaker."""
        return self.acting_requester_id is not None

    @property
    def sender_is_managed(self) -> bool:
        """Return whether the entity registry manages the sender, which is what trusts its metadata."""
        return self.sender_entity_name is not None

    @property
    def trusted_user_relay(self) -> bool:
        """Return whether a managed sender relayed a human's message."""
        return self.original_sender_is_human and self.source_kind in _USER_RELAY_SOURCE_KINDS

    @property
    def router_relay(self) -> bool:
        """Return whether the router relayed a human's or a configured bot account's message."""
        return (
            self.sender_entity_name == ROUTER_AGENT_NAME
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
        )


def is_router_voice_echo(facts: IngressFacts, content: object) -> bool:
    """Return whether content is the router's display-only transcript of a human's or bot account's audio."""
    return (
        facts.sender_entity_name == ROUTER_AGENT_NAME
        and facts.original_sender is not None
        and is_visible_router_voice_echo_content(content)
    )


def command_for(facts: IngressFacts, command_text: str, source_count: int = 1) -> Command | None:
    """Return the command one turn's text runs as, or None when the text is conversation.

    Only an ordinary message runs as a command, under its run requester's
    authority. Text a schedule, a hook, a trigger, or a relay produced is
    conversation: an agent or a plugin can write it, yet it would run with a
    person's authority. ``command_text`` is ``dispatch_prompt_for_event`` of the
    event's logical content, so a finished reply's tool markers never hide a
    command. A turn of several sources carries no command.
    """
    if source_count != 1 or facts.source_kind != MESSAGE_SOURCE_KIND:
        return None
    return command_parser.parse(command_text)
