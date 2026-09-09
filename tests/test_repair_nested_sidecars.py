"""One-off repairs survive a fresh import with the normal single-layer reader."""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom.matrix.message_content import resolve_event_source_content
from mindroom.matrix.sidecar_content import holds_unresolved_sidecar
from scripts.utilities.repair_nested_sidecars import repair
from tests.test_conversation_hydration import ALICE, ROOM, FakeClient, admit_all, raw
from tests.test_conversation_hydration import TestSidecarResolution as SidecarTests

if TYPE_CHECKING:
    from mindroom.event_journal import EventJournalStore


class MatrixAPI:
    """Keep Matrix event and media state behind the HTTP boundary."""

    def __init__(self) -> None:
        self.owner = ALICE
        self.encrypted = False
        self.refuse_redaction = False
        self.corrupt_readback = False
        self.newer_on_tail = False
        self.repeat_cursor = False
        self.writes: list[httpx.Request] = []
        self.events = {"$root": raw("$root", "pending", thread_id="$thread")}
        inner = {
            "msgtype": "m.file",
            "body": "preview",
            "url": "mxc://example.org/inner",
            "filename": "message.json",
            "info": {"mimetype": "application/json"},
            "io.mindroom.long_text": {"version": 2, "encoding": "matrix_event_content_json"},
        }
        outer = {**inner, "url": "mxc://example.org/outer"}
        self.events["$broken"] = {
            **raw("$broken", "* preview", ts=2_000, replaces="$root"),
            "content": {
                "msgtype": "m.file",
                "body": "* preview",
                "url": outer["url"],
                "m.new_content": outer,
                "m.relates_to": {"rel_type": "m.replace", "event_id": "$root"},
            },
        }
        self.media = {
            "outer": json.dumps({"m.new_content": inner}).encode(),
            "inner": json.dumps(
                {
                    "msgtype": "m.text",
                    "body": "* Complete original answer",
                    "m.new_content": {"msgtype": "m.text", "body": "Complete original answer"},
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$untrusted"},
                },
            ).encode(),
        }

    def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: C901, PLR0911, PLR0912
        """Implement only the Matrix endpoints this maintenance operation uses."""
        path = request.url.path
        if request.method == "GET":
            if path.endswith("/account/whoami"):
                return httpx.Response(200, json={"user_id": self.owner})
            if path.endswith("/state/m.room.encryption"):
                return httpx.Response(
                    200 if self.encrypted else 404,
                    json={"algorithm": "m.megolm.v1.aes-sha2"} if self.encrypted else {"errcode": "M_NOT_FOUND"},
                )
            if "/event/" in path:
                event = copy.deepcopy(self.events[path.rsplit("/", 1)[1]])
                if self.corrupt_readback and event["event_id"] == "$repaired":
                    event["content"]["body"] = "wrong payload"
                return httpx.Response(200, json=event)
            if "/relations/" in path:
                if request.url.params.get("from") == "tail":
                    if self.repeat_cursor:
                        return httpx.Response(200, json={"chunk": [], "next_batch": "tail"})
                    if self.newer_on_tail:
                        return httpx.Response(200, json={"chunk": [self.events["$newer"]]})
                    return httpx.Response(200, json={"chunk": []})
                edits = [
                    e
                    for e in self.events.values()
                    if e["content"].get("m.relates_to", {}).get("rel_type") == "m.replace"
                ]
                if self.newer_on_tail:
                    edits = [e for e in edits if e["event_id"] != "$newer"]
                return httpx.Response(200, json={"chunk": edits, "next_batch": "tail"})
            if "/media/download/" in path:
                payload = self.media.get(path.rsplit("/", 1)[1])
                return httpx.Response(404) if payload is None else httpx.Response(200, content=payload)
        if request.method == "PUT":
            self.writes.append(request)
            if "/send/m.room.message/" in path:
                self.events["$repaired"] = {
                    **raw("$repaired", "", ts=3_000, replaces="$root"),
                    "content": json.loads(request.content),
                }
                return httpx.Response(200, json={"event_id": "$repaired"})
            if "/redact/$broken/" in path:
                if self.refuse_redaction:
                    return httpx.Response(503)
                self.events["$broken"]["content"] = {}
                self.events["$broken"]["unsigned"] = {
                    "redacted_because": {"type": "m.room.redaction", "sender": ALICE, "content": {}},
                }
                return httpx.Response(200, json={"event_id": "$redaction"})
        message = f"Unexpected Matrix request: {request.method} {path}"
        raise AssertionError(message)

    def client(self) -> httpx.Client:
        """Use a real HTTP client with only its network transport replaced."""
        return httpx.Client(base_url="https://matrix.example.org", transport=httpx.MockTransport(self.handle))

    def history_client(self) -> FakeClient:
        """Read current server state through the existing hydration fixture."""
        return FakeClient(
            events=self.events,
            relations={"$root": [e for key, e in self.events.items() if key != "$root"]},
            sidecars={f"mxc://example.org/{key}": value.decode() for key, value in self.media.items()},
        )


@pytest.mark.asyncio
async def test_repair_survives_a_fresh_database_without_nested_reader_support(journal_store: EventJournalStore) -> None:
    """Repair source history rather than an existing projection or resolver cache."""
    api = MatrixAPI()
    unresolved = await resolve_event_source_content(api.events["$broken"], api.history_client())
    assert holds_unresolved_sidecar(unresolved["content"])
    with api.client() as client:
        assert repair(client, ROOM, "$broken")["status"] == "would_repair"
        assert api.writes == []
        assert repair(client, ROOM, "$broken", apply=True)["status"] == "repaired"
    assert api.events["$broken"]["content"] == {}
    replacement = api.events["$repaired"]["content"]
    assert replacement["m.new_content"]["url"] == "mxc://example.org/inner"
    assert replacement["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$root"}
    assert replacement["m.mentions"] == {}
    fresh_store = journal_store.principal("after-repair")
    await admit_all(fresh_store, list(api.events.values()))
    fresh_client = api.history_client()
    await fresh_store.install_hydrated_conversation(
        room_id=ROOM,
        thread_id="$thread",
        events=(),
        complete=True,
        expected_membership_epoch=await fresh_store.membership_epoch(ROOM),
    )
    reader = await SidecarTests._reader(fresh_store, fresh_client)
    page = await reader.read_strict(room_id=ROOM, thread_id="$thread", limit=10)
    assert [message.content["m.new_content"]["body"] for message in page.messages] == ["Complete original answer"]
    assert page.refresh_pending == ()
    assert fresh_client.downloads == ["mxc://example.org/inner"]


def test_retry_finishes_cleanup_without_sending_another_edit() -> None:
    """Resume a failed redaction after the replacement is already visible."""
    api = MatrixAPI()
    api.refuse_redaction = True
    with api.client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            repair(client, ROOM, "$broken", apply=True)
        assert api.events["$broken"]["content"]
        api.refuse_redaction = False
        assert repair(client, ROOM, "$broken", apply=True)["status"] == "repaired"
        assert repair(client, ROOM, "$broken", apply=True)["status"] == "already_redacted"
    assert sum("/send/" in request.url.path for request in api.writes) == 1


@pytest.mark.parametrize(
    "problem",
    [
        "wrong_owner",
        "encrypted_room",
        "newer_edit",
        "newer_edit_extra_relation",
        "newer_edit_later_page",
        "repeated_cursor",
        "missing_media",
        "missing_preview",
        "invalid_preview",
        "oversized_media",
        "third_layer",
    ],
)
def test_unsafe_or_unreadable_targets_cause_no_writes(problem: str) -> None:  # noqa: C901
    """Refuse unsafe source mutations before publishing or redacting anything."""
    api = MatrixAPI()
    if problem == "wrong_owner":
        api.owner = "@someone_else:example.org"
    elif problem == "encrypted_room":
        api.encrypted = True
    elif problem in {"newer_edit", "newer_edit_extra_relation", "newer_edit_later_page"}:
        api.events["$newer"] = raw("$newer", "A later answer", ts=4_000, replaces="$root")
        if problem == "newer_edit_extra_relation":
            api.events["$newer"]["content"]["m.relates_to"]["extension"] = "allowed"
        api.newer_on_tail = problem == "newer_edit_later_page"
    elif problem == "repeated_cursor":
        api.repeat_cursor = True
    elif problem == "missing_media":
        del api.media["inner"]
    elif problem in {"missing_preview", "invalid_preview"}:
        intermediate = json.loads(api.media["outer"])
        if problem == "missing_preview":
            del intermediate["m.new_content"]["body"]
        else:
            intermediate["m.new_content"]["body"] = 42
        api.media["outer"] = json.dumps(intermediate).encode()
    elif problem == "oversized_media":
        api.media["inner"] = b"x" * (2 * 1024 * 1024 + 1)
    elif problem == "third_layer":
        api.media["inner"] = api.media["outer"]
    with api.client() as client, pytest.raises((TypeError, ValueError, httpx.HTTPStatusError)):
        repair(client, ROOM, "$broken", apply=True)
    assert api.writes == []


def test_failed_replacement_verification_never_redacts_the_source() -> None:
    """An acknowledged but unverified replacement cannot justify deleting the old edit."""
    api = MatrixAPI()
    api.corrupt_readback = True
    with api.client() as client, pytest.raises(ValueError, match="verification"):
        repair(client, ROOM, "$broken", apply=True)
    assert api.events["$broken"]["content"]
    assert all("/redact/" not in request.url.path for request in api.writes)
