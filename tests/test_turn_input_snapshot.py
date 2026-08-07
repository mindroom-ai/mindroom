"""The durable input snapshot one adopted turn needs in order to execute.

`TurnRecord` persists a turn's identity, its per-source prompts and revisions,
its requester, its conversation target, and its history scope. It persists
nothing about media, so a coalesced batch of one caption and three images
recovers from `TurnStore` as text alone.

That is what blocks contract 2 of the event-journal cutover: ownership of an
actionable source is meant to leave the journal once the turn is durably
adopted, and at that point the journal payload -- today the only durable copy
of a turn's media -- is compacted away. These tests hold the snapshot to the
bar the journal side is already held to in `TestReplayFidelity`: what replays
must be the media that was recorded, not a description of it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.conversation_resolver import MessageContext
from mindroom.event_journal import EventClass, EventKind
from mindroom.handled_turns import (
    TurnInputSnapshot,
    TurnMediaSource,
    TurnRecord,
    TurnRecordCodec,
    _reset_handled_turn_ledger_runtime,
)
from mindroom.hooks import MessageEnvelope
from mindroom.inbound_turn_normalizer import DispatchPayload
from mindroom.matrix.journal_ingress import inbound_event
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.response_payload_preparation import (
    DispatchPayloadInputs,
    ResponsePayloadPreparer,
    _dispatch_payload_inputs_from_snapshot,
    _TurnInputSnapshotCorruptionError,
    turn_input_snapshot,
)
from mindroom.turn_policy import PreparedDispatch, _DispatchPlan
from mindroom.turn_record import canonicalize_turn_record
from mindroom.turn_store import TurnStore, TurnStoreDeps
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_generate_response_mock,
    install_send_response_mock,
    make_matrix_client_mock,
    message_origin,
    prepared_dispatch_result,
    replace_turn_controller_deps,
    runtime_paths_for,
    test_runtime_paths,
    wrap_extracted_collaborators,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.event_journal import PrincipalStore
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

ROOM = "!room:localhost"
ALICE = "@user:localhost"


def image_event(
    event_id: str,
    body: str = "photo.png",
    *,
    ts: int = 1_000,
    encrypted: bool = False,
) -> nio.RoomMessageMedia | nio.RoomEncryptedMedia:
    """Return a parsed image message, optionally with its decryption keys."""
    content: dict[str, Any] = {
        "msgtype": "m.image",
        "body": body,
        "filename": body,
        "info": {"mimetype": "image/png", "size": 4_096, "w": 64, "h": 64},
    }
    slug = event_id.lstrip("$")
    if encrypted:
        content["file"] = {
            "url": f"mxc://example.org/{slug}",
            "key": {
                "k": "cipher-key-material",
                "alg": "A256CTR",
                "ext": True,
                "key_ops": ["encrypt", "decrypt"],
                "kty": "oct",
            },
            "iv": "initialization-vector",
            "hashes": {"sha256": "content-hash"},
            "v": "v2",
        }
    else:
        content["url"] = f"mxc://example.org/{slug}"
    source = {
        "event_id": event_id,
        "sender": ALICE,
        "origin_server_ts": ts,
        "room_id": ROOM,
        "type": "m.room.message",
        "content": content,
    }
    parsed = nio.RoomMessage.parse_decrypted_event(source) if encrypted else nio.RoomMessage.parse_event(source)
    assert isinstance(parsed, nio.RoomMessageMedia | nio.RoomEncryptedMedia)
    return parsed


def text_event(event_id: str, body: str = "what do these have in common?", *, ts: int = 1_003) -> nio.RoomMessageText:
    """Return a parsed text message event."""
    parsed = nio.Event.parse_event(
        {
            "event_id": event_id,
            "sender": ALICE,
            "origin_server_ts": ts,
            "room_id": ROOM,
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": body},
        },
    )
    assert isinstance(parsed, nio.RoomMessageText)
    return parsed


def payload_inputs(
    *media_events: nio.RoomMessageMedia | nio.RoomEncryptedMedia,
    message_attachment_ids: tuple[str, ...] = (),
    trusted_attachment_ids: tuple[str, ...] = (),
    raw_audio_fallback: bool = False,
    voice_transcript: bool = False,
) -> DispatchPayloadInputs:
    """Return the ingress-side media and attachment inputs for one turn."""
    return DispatchPayloadInputs(
        message_attachment_ids=message_attachment_ids,
        trusted_attachment_ids=trusted_attachment_ids,
        media_events=cast("tuple[Any, ...]", media_events),
        raw_audio_fallback=raw_audio_fallback,
        voice_transcript=voice_transcript,
    )


def _turn_store(tmp_path: Path) -> TurnStore:
    """Return a store backed by a real on-disk handled-turn ledger."""
    return TurnStore(
        TurnStoreDeps(
            agent_name="agent",
            tracking_base_path=tmp_path,
            state_writer=MagicMock(),
            resolver=MagicMock(),
            tool_runtime=MagicMock(),
        ),
    )


def _reopened_after_restart(tmp_path: Path) -> TurnStore:
    """Return a store that has read the ledger back from disk.

    Dropping the process-wide ledger state is what makes this a restart rather
    than a second reference to the same in-memory dictionary, which would
    prove nothing about durability.
    """
    _reset_handled_turn_ledger_runtime()
    store = _turn_store(tmp_path)
    store.warm()
    return store


def _record_adopted_turn(
    store: TurnStore,
    source_event_ids: list[str],
    inputs: DispatchPayloadInputs,
) -> None:
    """Durably adopt one turn carrying its recorded input snapshot."""
    store.record_pending_turn(
        canonicalize_turn_record(
            TurnRecord.create(source_event_ids, completed=False),
            input_snapshot=turn_input_snapshot(inputs),
            response_owner="agent",
            requester_id=ALICE,
            conversation_target=MessageTarget.resolve(room_id=ROOM, thread_id=None, reply_to_event_id="$caption"),
        ),
    )


def _replayed_inputs(store: TurnStore, source_event_id: str) -> DispatchPayloadInputs:
    """Return the execution inputs one recorded turn replays with."""
    record = store.get_turn_record(source_event_id)
    assert record is not None
    assert record.input_snapshot is not None
    return _dispatch_payload_inputs_from_snapshot(record.input_snapshot)


class TestSnapshotReplayFidelity:
    """A replayed turn must run on the media that was recorded."""

    def test_a_coalesced_batch_replays_with_its_exact_media_in_receipt_order(self, tmp_path: Path) -> None:
        """Three images and a caption are one turn to the model.

        Recovering the caption alone, or recovering the images in the wrong
        order, both change the input the turn runs on.
        """
        store = _turn_store(tmp_path)
        _record_adopted_turn(
            store,
            ["$one", "$two", "$three", "$caption"],
            payload_inputs(
                image_event("$one", "first.png", ts=1_000),
                image_event("$two", "second.png", ts=1_001),
                image_event("$three", "third.png", ts=1_002),
            ),
        )

        replayed = _replayed_inputs(_reopened_after_restart(tmp_path), "$caption")

        assert [event.event_id for event in replayed.media_events] == ["$one", "$two", "$three"]
        assert [event.url for event in replayed.media_events] == [
            "mxc://example.org/one",
            "mxc://example.org/two",
            "mxc://example.org/three",
        ]
        assert [event.body for event in replayed.media_events] == ["first.png", "second.png", "third.png"]

    def test_an_encrypted_image_replays_with_its_decryption_keys(self, tmp_path: Path) -> None:
        """Without the key material the reference is a file nobody can open.

        A snapshot that kept the MXC reference and dropped the keys would pass
        any assertion that only asks whether media survived, and would produce
        an unopenable image in production.
        """
        store = _turn_store(tmp_path)
        _record_adopted_turn(store, ["$sealed"], payload_inputs(image_event("$sealed", "sealed.png", encrypted=True)))

        replayed = _replayed_inputs(_reopened_after_restart(tmp_path), "$sealed")

        sealed = replayed.media_events[0]
        assert isinstance(sealed, nio.RoomEncryptedImage)
        assert sealed.url == "mxc://example.org/sealed"
        assert sealed.key["k"] == "cipher-key-material"
        assert sealed.iv == "initialization-vector"
        assert sealed.hashes["sha256"] == "content-hash"

    def test_no_media_bytes_are_written_to_the_ledger(self, tmp_path: Path) -> None:
        """References plus key material only; the payload stays on the server."""
        store = _turn_store(tmp_path)
        _record_adopted_turn(store, ["$sealed"], payload_inputs(image_event("$sealed", encrypted=True)))
        store.deps  # noqa: B018 - the ledger path is derived from these deps
        _reset_handled_turn_ledger_runtime()

        persisted = json.loads((tmp_path / "agent_responded.json").read_text(encoding="utf-8"))

        snapshot = persisted["records"]["$sealed"]["input_snapshot"]
        content = snapshot["media_sources"][0]["source"]["content"]
        assert content["file"]["url"] == "mxc://example.org/sealed"
        assert set(content) == {"msgtype", "body", "filename", "info", "file"}

    def test_attachment_and_voice_inputs_replay_with_the_turn(self, tmp_path: Path) -> None:
        """Media is not the only ingress input the prompt does not carry."""
        store = _turn_store(tmp_path)
        _record_adopted_turn(
            store,
            ["$voice"],
            payload_inputs(
                message_attachment_ids=("att_first", "att_second"),
                trusted_attachment_ids=("att_second",),
                raw_audio_fallback=True,
                voice_transcript=True,
            ),
        )

        replayed = _replayed_inputs(_reopened_after_restart(tmp_path), "$voice")

        assert replayed.message_attachment_ids == ("att_first", "att_second")
        assert replayed.trusted_attachment_ids == ("att_second",)
        assert replayed.raw_audio_fallback
        assert replayed.voice_transcript

    def test_a_turn_with_no_media_records_an_empty_snapshot_not_a_missing_one(self, tmp_path: Path) -> None:
        """A turn that carried no media says so, rather than saying nothing.

        "This turn had no media" and "nothing was recorded" are different
        answers, and only the first lets a later owner decide an adopted turn
        is complete and may run.
        """
        store = _turn_store(tmp_path)
        _record_adopted_turn(store, ["$text"], payload_inputs())

        record = _reopened_after_restart(tmp_path).get_turn_record("$text")

        assert record is not None
        assert record.input_snapshot == TurnInputSnapshot()

    def test_a_ledger_row_written_before_snapshots_existed_still_loads(self) -> None:
        """The ledger gains an optional key rather than a new schema version.

        Bumping the version would quarantine every existing ledger file, which
        discards live turn identity for a purely additive field.
        """
        legacy_row = TurnRecordCodec._to_ledger_record(TurnRecord.create(["$old"]))
        assert "input_snapshot" not in legacy_row

        record = TurnRecordCodec._from_ledger_record("$old", legacy_row)

        assert record is not None
        assert record.input_snapshot is None

    def test_a_corrupt_media_source_is_refused_not_guessed(self, tmp_path: Path) -> None:
        """A payload that is not the event it was recorded as replays as nothing."""
        store = _turn_store(tmp_path)
        _record_adopted_turn(store, ["$one"], payload_inputs(image_event("$one")))
        record = _reopened_after_restart(tmp_path).get_turn_record("$one")
        assert record is not None
        assert record.input_snapshot is not None
        corrupted = TurnInputSnapshot(
            media_sources=(
                TurnMediaSource(
                    event_id="$one",
                    source={**record.input_snapshot.media_sources[0].source, "event_id": "$different"},
                ),
            ),
        )

        with pytest.raises(_TurnInputSnapshotCorruptionError):
            _dispatch_payload_inputs_from_snapshot(corrupted)

    def test_a_redacted_source_takes_its_media_out_of_the_snapshot(self, tmp_path: Path) -> None:
        """A redacted source owns no reply, and its media must not outlive it.

        Prompts and source metadata already leave the record when a source is
        tombstoned; media that stayed would let a replay run the turn on
        content the sender deleted.
        """
        store = _turn_store(tmp_path)
        _record_adopted_turn(
            store,
            ["$one", "$two", "$caption"],
            payload_inputs(image_event("$one", ts=1_000), image_event("$two", ts=1_001)),
        )

        store.mark_source_redacted("$two")
        record = _reopened_after_restart(tmp_path).get_turn_record("$one")

        assert record is not None
        assert record.input_snapshot is not None
        assert [media.event_id for media in record.input_snapshot.media_sources] == ["$one"]

    def test_media_whose_source_leaves_the_turn_leaves_with_it(self, tmp_path: Path) -> None:
        """Media follows the record's sources, as prompts and metadata already do.

        A coalesced source can be projected off a turn when another completed
        turn owns it. Its media staying behind would let this turn replay on an
        input it no longer owns.
        """
        store = _turn_store(tmp_path)
        _record_adopted_turn(
            store,
            ["$one", "$two", "$caption"],
            payload_inputs(image_event("$one", ts=1_000), image_event("$two", ts=1_001)),
        )
        record = _reopened_after_restart(tmp_path).get_turn_record("$caption")
        assert record is not None

        detached = canonicalize_turn_record(record, source_event_ids=("$one", "$caption"))

        assert detached.input_snapshot is not None
        assert [media.event_id for media in detached.input_snapshot.media_sources] == ["$one"]


def _make_bot(tmp_path: Path) -> AgentBot:
    """Create a bot wired to a temporary runtime root and a real journal."""
    config = bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="TestAgent", rooms=[ROOM])},
            teams={},
            models={"default": ModelConfig(provider="test", id="test-model")},
            defaults=DefaultsConfig(coalescing={"debounce_ms": 10}),
            authorization=AuthorizationConfig(default_room_access=True),
        ),
        test_runtime_paths(tmp_path),
    )
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        password=TEST_PASSWORD,
        display_name="TestAgent",
        user_id="@mindroom_test_agent:localhost",
    )
    bot = AgentBot(agent_user, tmp_path, config, runtime_paths_for(config), rooms=[ROOM])
    bot.client = make_matrix_client_mock(user_id=agent_user.user_id)
    wrap_extracted_collaborators(bot)
    replace_turn_controller_deps(
        bot,
        turn_policy=bot._turn_policy,
        delivery_gateway=bot._delivery_gateway,
        response_runner=bot._response_runner,
        resolver=bot._conversation_resolver,
        normalizer=bot._inbound_turn_normalizer,
        state_writer=bot._conversation_state_writer,
    )
    return bot


def _prepared_dispatch(event_id: str) -> PreparedDispatch:
    """Return a minimal prepared dispatch for the caption of a media batch."""
    history: list[ResolvedVisibleMessage] = []
    target = MessageTarget.resolve(room_id=ROOM, thread_id=None, reply_to_event_id=event_id)
    return PreparedDispatch(
        requester_user_id=ALICE,
        context=MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=history,
            replay_guard_history=history,
            mentioned_agents=[],
            has_non_agent_mentions=False,
        ),
        target=target,
        correlation_id=event_id,
        envelope=MessageEnvelope(
            source_event_id=event_id,
            target=target,
            body="what do these have in common?",
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="test_agent",
            origin=message_origin(sender_id=ALICE, requester_id=ALICE, source_kind="image"),
        ),
    )


def _room() -> MagicMock:
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = ROOM
    room.canonical_alias = None
    return room


async def _admit_actionable(store: PrincipalStore, *events: nio.Event) -> None:
    """Admit each event as pending semantic work, as live ingress would."""
    for event in events:
        kind = EventKind.MESSAGE if isinstance(event, nio.RoomMessageText) else EventKind.MEDIA
        await store.admit(inbound_event(ROOM, event, kind, EventClass.ACTIONABLE), None)


@pytest.mark.asyncio
class TestDurableAdoptionRecordsTheSnapshot:
    """Adoption is where the snapshot has to be written, or it is never written."""

    async def test_a_dispatched_media_batch_records_its_media_with_the_turn(self, tmp_path: Path) -> None:
        """The turn the runner executes and the turn the ledger holds agree."""
        bot = _make_bot(tmp_path)
        images = [image_event("$one", "first.png", ts=1_000), image_event("$two", "second.png", ts=1_001)]
        caption = text_event("$caption")
        install_send_response_mock(bot, AsyncMock(return_value="$placeholder"))
        install_generate_response_mock(bot, AsyncMock(return_value="$response"))

        with (
            patch.object(
                bot._turn_controller,
                "_prepare_dispatch",
                new=AsyncMock(return_value=prepared_dispatch_result(_prepared_dispatch("$caption"))),
            ),
            patch.object(
                bot._turn_policy,
                "plan_turn",
                new=AsyncMock(
                    return_value=_DispatchPlan(kind="respond", response_action=MagicMock(kind="individual")),
                ),
            ),
            patch.object(
                bot._inbound_turn_normalizer,
                "register_batch_media_attachments",
                new=AsyncMock(return_value=MagicMock(attachment_ids=[], fallback_images=None)),
            ),
            patch.object(
                bot._inbound_turn_normalizer,
                "build_dispatch_payload_with_attachments",
                new=AsyncMock(return_value=DispatchPayload(prompt="combined")),
            ),
            patch.object(ResponsePayloadPreparer, "_log_dispatch_latency"),
        ):
            await bot._turn_controller._dispatch_text_message(
                _room(),
                caption,
                ALICE,
                media_events=cast("list[Any]", images),
                handled_turn=TurnRecord.create(["$one", "$two", "$caption"], completed=False),
            )

        record = bot._turn_store.get_turn_record("$caption")
        assert record is not None
        assert record.input_snapshot is not None
        assert [media.event_id for media in record.input_snapshot.media_sources] == ["$one", "$two"]

    async def test_a_durable_turn_persistence_failure_leaves_every_journal_source_pending(
        self,
        tmp_path: Path,
    ) -> None:
        """Adoption is the handoff, so a failed adoption hands nothing over.

        If the durable turn write fails, the journal is still the only owner of
        this work and every source has to stay pending for the next process to
        replay. Settling any of them would drop an answer the bot owes.
        """
        bot = _make_bot(tmp_path)
        journal = bot._journal_store.principal(bot._journal_principal_id)
        images = [image_event("$one", "first.png", ts=1_000), image_event("$two", "second.png", ts=1_001)]
        caption = text_event("$caption")
        await _admit_actionable(journal, *images, caption)
        generate_response = AsyncMock(return_value="$response")
        install_send_response_mock(bot, AsyncMock(return_value="$placeholder"))
        install_generate_response_mock(bot, generate_response)

        adoption_error: BaseException | None = None
        with (
            patch("mindroom.handled_turns.write_json_file_durable", side_effect=OSError("no space left on device")),
            patch.object(
                bot._turn_controller,
                "_prepare_dispatch",
                new=AsyncMock(return_value=prepared_dispatch_result(_prepared_dispatch("$caption"))),
            ),
            patch.object(
                bot._turn_policy,
                "plan_turn",
                new=AsyncMock(
                    return_value=_DispatchPlan(kind="respond", response_action=MagicMock(kind="individual")),
                ),
            ),
        ):
            # Captured rather than asserted through `pytest.raises`, so a
            # variant that swallows the failure is still measured against the
            # journal instead of aborting before the assertions that matter.
            try:
                await bot._turn_controller._dispatch_text_message(
                    _room(),
                    caption,
                    ALICE,
                    media_events=cast("list[Any]", images),
                    handled_turn=TurnRecord.create(["$one", "$two", "$caption"], completed=False),
                )
            except OSError as error:
                adoption_error = error

        assert [event.event_id for event in await journal.pending()] == ["$one", "$two", "$caption"]
        generate_response.assert_not_awaited()
        assert isinstance(adoption_error, OSError)
