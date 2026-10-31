"""Sliding request settings and room checkpoints; persistence stays shared."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..api import Api
from ..client.sliding_sync import timeline_provenance
from ..event_provenance import TimelineEventProvenance
from ..events import AccountDataEvent, InviteMemberEvent, MegolmEvent, RoomMemberEvent
from ..exceptions import LocalProtocolError
from ..responses import SlidingSyncResponse, SlidingSyncRoom, SlidingSyncStateStub
from .model import encode_json

if TYPE_CHECKING:
    from .client import DurableSync


@dataclass(frozen=True, slots=True)
class SlidingSyncConfig:
    """One simplified Sliding connection; dictionaries are copied at opening."""

    conn_id: str = "nio"
    lists: dict[str, Any] = field(
        default_factory=lambda: {
            "main": {
                "ranges": [[0, 99]],
                "timeline_limit": 100,
                "required_state": [["*", "*"]],
            }
        }
    )
    room_subscriptions: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.conn_id or len(self.conn_id) > 64:
            raise ValueError("Sliding connection ID must contain 1 to 64 characters")
        for name in ("to_device", "e2ee", "account_data"):
            if self.extensions.get(name, {}).get("enabled") is False:
                raise ValueError(f"durable Sliding requires the {name} extension")


class SlidingSource:
    def __init__(self, session: DurableSync, config: SlidingSyncConfig):
        self.session = session
        self.config = deepcopy(config)
        self.pos: str | None = None
        self.generation = 0
        self.accepted_generation: int | None = None
        self.unknown_positions = 0
        self.baselines: dict[str, dict[str, Any]] = {
            key: json.loads(body)
            for key, body in session._store.database.execute_sql(
                "SELECT key,body FROM NioDurableCrypto WHERE kind='sliding_room'"
            )
        }
        row = session._store.database.execute_sql(
            "SELECT body FROM NioDurableCrypto WHERE kind='sliding_device' AND key='current'"
        ).fetchone()
        self.device_cursor: str | None = json.loads(row[0]) if row else None
        session.client._pending_sliding_room_account_data = {
            room_id: {
                kind: AccountDataEvent.parse_event(raw)
                for kind, raw in json.loads(body).items()
            }
            for room_id, body in session._store.database.execute_sql(
                "SELECT key,body FROM NioDurableCrypto WHERE kind='sliding_account_data'"
            )
        }

    def request(self) -> tuple[str, str, str]:
        config = self.config
        lists, subscriptions = (
            deepcopy(config.lists),
            deepcopy(config.room_subscriptions),
        )
        for room in (*lists.values(), *subscriptions.values()):
            state = room.setdefault("required_state", [])
            for selector in (
                ["m.room.member", "$ME"],
                ["m.room.create", ""],
                ["m.room.power_levels", ""],
                ["m.room.encryption", ""],
            ):
                if selector not in state:
                    state.append(selector)
        extensions = deepcopy(config.extensions)
        for name in ("to_device", "e2ee", "account_data"):
            extensions.setdefault(name, {})["enabled"] = True
        # Only committed progress may acknowledge delivered keys.
        extensions["to_device"].pop("since", None)
        if self.device_cursor is not None:
            extensions["to_device"]["since"] = self.device_cursor
        return Api.sliding_sync(
            "",
            pos=self.pos,
            timeout=self.session.config.sync_timeout_ms if self.pos else 0,
            conn_id=config.conn_id,
            lists=lists,
            room_subscriptions=subscriptions,
            extensions=extensions,
        )

    def forget_room(self, room_id: str) -> None:
        """Invalidate a replaced projection before requesting its fresh state."""
        self.baselines.pop(room_id, None)
        self.session._store.database.execute_sql(
            "DELETE FROM NioDurableCrypto WHERE kind='sliding_room' AND key=?",
            (room_id,),
        )
        self.pos = None
        self.generation += 1

    def membership(
        self, room_id: str, room: SlidingSyncRoom
    ) -> tuple[str | None, bool]:
        """Return current proof and whether it continues the held joined tenure."""
        held = self.baselines.get(room_id, {})
        known = set(held.get("event_ids", []))
        live_count = room.num_live
        if live_count is None:
            live_count = (
                0 if room.initial or room.expanded_timeline else len(room.timeline)
            )
        live = (
            room.timeline[-min(max(live_count, 0), len(room.timeline)) :]
            if live_count > 0
            else []
        )
        own = [
            event
            for event in (*room.required_state, *live)
            if isinstance(event, (RoomMemberEvent, SlidingSyncStateStub))
            and not (isinstance(event, RoomMemberEvent) and event.decrypted)
            and event.state_key == self.session.client.user_id
            and (
                not isinstance(event, SlidingSyncStateStub)
                or event.type == "m.room.member"
            )
        ]
        continuous_proof = held.get("membership")
        new_join = False
        for candidate in live:
            if (
                not isinstance(candidate, RoomMemberEvent)
                or candidate.decrypted
                or candidate.state_key != self.session.client.user_id
            ):
                continue
            unsigned = candidate.source.get("unsigned", {})
            if candidate.event_id == continuous_proof:
                continue
            if candidate.event_id in known:
                continue
            if (
                candidate.membership != "join"
                or candidate.prev_membership != "join"
                or not isinstance(unsigned, dict)
                or unsigned.get("replaces_state") != continuous_proof
            ):
                new_join = True
                continuous_proof = None
            elif not new_join:
                continuous_proof = candidate.event_id
        event = own[-1] if own else None
        if room.membership in ("leave", "ban", "invite") or room.stripped_state:
            return None, False
        if event is None:
            proof = None if room.initial else held.get("membership")
            return proof, bool(proof)
        if not isinstance(event, RoomMemberEvent) or event.membership != "join":
            return None, False
        proof = event.event_id
        unsigned = event.source.get("unsigned", {})
        linked = (
            event.prev_membership == "join"
            and isinstance(unsigned, dict)
            and unsigned.get("replaces_state") == held.get("membership")
        )
        continues = not new_join and (
            proof in (held.get("membership"), continuous_proof) or linked
        )
        return proof, continues

    def boundary_membership(self, room: SlidingSyncRoom) -> str | None:
        """Return explicit current own membership from a room response."""
        if room.membership in ("join", "leave", "invite", "ban"):
            return room.membership
        for event in reversed((*room.required_state, *room.stripped_state)):
            if (
                isinstance(event, (RoomMemberEvent, InviteMemberEvent))
                and event.state_key == self.session.client.user_id
                and event.membership in ("join", "leave", "invite", "ban")
            ):
                return event.membership
            if (
                isinstance(event, SlidingSyncStateStub)
                and event.type == "m.room.member"
                and event.state_key == self.session.client.user_id
            ):
                return "leave"
        return None

    def plan(self, response: SlidingSyncResponse, state: dict[str, Any]) -> None:
        state["sliding_starts"] = {}
        state["recovered_rooms"] = []
        state["history_rooms"] = []
        state["unknown_rooms"] = []
        state["sliding_history_prefixes"] = {}
        for room_id, room in response.rooms.items():
            held = self.baselines.get(room_id, {})
            _, continues = self.membership(room_id, room)
            state["applied_ids"][room_id] = held.get("event_ids", [])
            observed = set(held.get("window_ids", []))
            state["sliding_history_prefixes"][room_id] = next(
                (
                    index
                    for index, event in enumerate(room.timeline)
                    if event.source.get("event_id") in observed
                ),
                0,
            )
            if room.initial or room.limited:
                if continues and held.get("token") and room.prev_batch:
                    state["sliding_starts"][room_id] = held["token"]
                    state["recovered_rooms"].append(room_id)
                else:
                    state["history_rooms"].append(room_id)
                    if room_id in self.session._metadata:
                        state["unknown_rooms"].append(room_id)
            elif not self.session._metadata.get(room_id, {}).get("baseline"):
                state["history_rooms"].append(room_id)

    def commit(self, response: SlidingSyncResponse, state: dict[str, Any]) -> None:
        database = self.session._store.database
        pending = self.session.client._pending_sliding_room_account_data
        encoded_pending = {
            room_id: encode_json({
                kind: {**event.source, "type": kind} for kind, event in events.items()
            })
            for room_id, events in pending.items()
        }
        if (
            sum(len(value.encode()) for value in encoded_pending.values())
            > self.session.config.max_pending_bytes
        ):
            raise LocalProtocolError(
                "Sliding account data exceeds the durable pending bound"
            )
        database.execute_sql(
            "DELETE FROM NioDurableCrypto WHERE kind='sliding_account_data'"
        )
        for room_id, encoded in encoded_pending.items():
            database.execute_sql(
                "INSERT INTO NioDurableCrypto(kind,key,body) VALUES('sliding_account_data',?,?)",
                (room_id, encoded),
            )
        for room_id, room in response.rooms.items():
            proof, _ = self.membership(room_id, room)
            prior = self.baselines.get(room_id, {})
            if proof is not None and self.session._metadata[room_id].get("baseline"):
                prefix = state["sliding_history_prefixes"][room_id]
                token = room.prev_batch
                if (
                    prefix
                    and room_id in state["recovered_rooms"]
                    and room_id not in state["history_rooms"]
                ):
                    token = prior["token"]
                elif not (room.initial or room.limited):
                    token = token or prior.get("token")
                # Only actionable ciphertext awaits promotion; history stays observed.
                ids = [
                    event.source.get("event_id")
                    for index, event in enumerate(room.timeline)
                    if not isinstance(event, MegolmEvent)
                    or timeline_provenance(
                        room,
                        index,
                        history=room_id in state["history_rooms"],
                        recovered=room_id in state["recovered_rooms"],
                        history_prefix=prefix,
                    )
                    is TimelineEventProvenance.HISTORY
                ]
                window_ids = [
                    event.source.get("event_id") for event in room.timeline[prefix:]
                ]
                if token == prior.get("token"):
                    ids = list(dict.fromkeys([*prior.get("event_ids", []), *ids]))
                    window_ids = list(
                        dict.fromkeys([*prior.get("window_ids", []), *window_ids])
                    )
                value = {
                    "token": token,
                    "membership": proof,
                    "event_ids": ids,
                    "window_ids": window_ids,
                }
                if len(encode_json(value).encode()) > 2 * 1024 * 1024:
                    raise LocalProtocolError(
                        "Sliding room checkpoint exceeds its bound"
                    )
                self.baselines[room_id] = value
                database.execute_sql(
                    "INSERT OR REPLACE INTO NioDurableCrypto(kind,key,body) VALUES('sliding_room',?,?)",
                    (room_id, encode_json(value)),
                )
            else:
                self.baselines.pop(room_id, None)
                database.execute_sql(
                    "DELETE FROM NioDurableCrypto WHERE kind='sliding_room' AND key=?",
                    (room_id,),
                )
        if response.to_device_next_batch is not None:
            self.device_cursor = response.to_device_next_batch
            database.execute_sql(
                "INSERT OR REPLACE INTO NioDurableCrypto(kind,key,body) VALUES('sliding_device','current',?)",
                (encode_json(self.device_cursor),),
            )
        if self.accepted_generation == self.generation:
            self.pos = response.pos
        self.unknown_positions = 0
