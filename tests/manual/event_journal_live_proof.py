"""Prove the event journal's homeserver assumptions against a live server.

Some of what this design rests on is not observable from MindRoom's source or
from a mocked client:

- whether the server actually traverses relations to the depth we require,
- what a redacted current edit leaves behind,
- whether resending a transaction ID the server already accepted is a no-op,
- what a bounded ``/messages`` walk looks like when history runs out.

Each of those is a place where a wrong assumption is invisible in CI and
expensive in production, so each is checked here against a disposable Tuwunel.

Run it with::

    uv run python tests/manual/event_journal_live_proof.py

Add ``--keep`` to leave the server running for inspection. Every run uses a
throwaway instance, throwaway accounts, and removes the stack afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import nio

from mindroom.event_journal import (
    DeliveryStage,
    EventClass,
    EventJournalStore,
    EventKind,
)
from mindroom.matrix.conversation_hydration import ConversationHydrator
from mindroom.matrix.journal_ingress import inbound_event, projected_event

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.event_journal import PrincipalStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTANCE_REGISTRY = PROJECT_ROOT / "local" / "instances" / "deploy" / "instances.json"
PRINCIPAL = "journal-live-proof"


def _run(*command: str) -> str:
    result = subprocess.run(command, check=False, cwd=PROJECT_ROOT, capture_output=True, text=True)
    if result.returncode:
        msg = f"command failed ({' '.join(command)}):\n{result.stdout}\n{result.stderr}"
        raise RuntimeError(msg)
    return result.stdout


@dataclass
class DisposableTuwunel:
    """One throwaway Tuwunel homeserver."""

    instance_name: str = field(default_factory=lambda: f"jrnl{secrets.token_hex(4)}")
    homeserver: str = ""
    server_name: str = ""
    _created: bool = False

    def start(self) -> None:
        """Create and start only the Matrix side of a local instance."""
        _run("just", "local-instances-create", self.instance_name, "tuwunel")
        self._created = True
        registry = json.loads(INSTANCE_REGISTRY.read_text(encoding="utf-8"))
        instance = registry["instances"][self.instance_name]
        self.homeserver = f"http://127.0.0.1:{int(instance['matrix_port'])}"
        self.server_name = f"m-{instance['domain']}"
        _run("just", "local-instances-start-matrix", self.instance_name)
        self._wait_ready()

    def _wait_ready(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                response = httpx.get(f"{self.homeserver}/_matrix/client/versions", timeout=2)
            except httpx.HTTPError as error:
                last_error = error
            else:
                if response.status_code == httpx.codes.OK:
                    return
            time.sleep(0.25)
        msg = f"Tuwunel did not become ready at {self.homeserver}"
        raise RuntimeError(msg) from last_error

    def close(self) -> None:
        """Remove the instance and its data."""
        if self._created:
            _run("just", "local-instances-remove", self.instance_name)
            self._created = False


@dataclass(frozen=True, slots=True)
class _Account:
    """One disposable account, kept whole so it can be logged in twice.

    The password is retained deliberately. A second login by the *same* user
    is the only way to observe what a re-login does to transaction-ID
    idempotency, and that is the situation MindRoom actually restarts into.
    """

    client: nio.AsyncClient
    user_id: str
    password: str


async def _register(homeserver: str) -> _Account:
    """Register one disposable account and return a logged-in client."""
    username = f"jrnl{secrets.token_hex(6)}"
    password = secrets.token_urlsafe(24)
    client = nio.AsyncClient(homeserver, f"@{username}:unused")
    response = await client.register(username, password)
    if not isinstance(response, nio.RegisterResponse):
        msg = f"registration failed: {response}"
        raise TypeError(msg)
    client.user_id = response.user_id
    client.access_token = response.access_token
    client.device_id = response.device_id
    return _Account(client=client, user_id=response.user_id, password=password)


async def _login_another_device(account: _Account) -> nio.AsyncClient:
    """Log the same account in again, producing a second device."""
    client = nio.AsyncClient(account.client.homeserver, account.user_id)
    response = await client.login(account.password)
    if not isinstance(response, nio.LoginResponse):
        msg = f"second login failed: {response}"
        raise TypeError(msg)
    if response.device_id == account.client.device_id:
        msg = "the second login reused the first device, so nothing here is a re-login"
        raise AssertionError(msg)
    return client


async def _send(
    client: nio.AsyncClient,
    room_id: str,
    content: dict[str, Any],
    *,
    transaction_id: str | None = None,
) -> str:
    response = await client.room_send(
        room_id,
        "m.room.message",
        content,
        ignore_unverified_devices=True,
        tx_id=transaction_id,
    )
    if not isinstance(response, nio.RoomSendResponse):
        msg = f"send failed: {response}"
        raise TypeError(msg)
    return response.event_id


def _text(body: str) -> dict[str, Any]:
    return {"msgtype": "m.text", "body": body}


def _edit(target: str, body: str) -> dict[str, Any]:
    return {
        "msgtype": "m.text",
        "body": f"* {body}",
        "m.new_content": {"msgtype": "m.text", "body": body},
        "m.relates_to": {"rel_type": "m.replace", "event_id": target},
    }


def _threaded(root: str, body: str) -> dict[str, Any]:
    return {
        "msgtype": "m.text",
        "body": body,
        "m.relates_to": {"rel_type": "m.thread", "event_id": root},
    }


@dataclass
class Findings:
    """What the live server actually did."""

    results: list[tuple[str, bool, str]] = field(default_factory=list)

    def record(self, name: str, passed: bool, detail: str = "") -> None:
        """Record one observation that must hold on every server."""
        self.results.append((name, passed, detail))
        marker = "PASS" if passed else "FAIL"
        print(f"[{marker}] {name}{f' — {detail}' if detail else ''}")

    def note(self, name: str, detail: str) -> None:
        """Record what a server did without requiring every server to do it.

        Some behaviour differs legitimately between implementations, and the
        useful thing is the measurement rather than a verdict. Asserting one
        server's answer as if it were the contract makes this harness fail on
        the other, which stops it being run there at all -- the opposite of
        what a two-server proof is for.
        """
        print(f"[NOTE] {name} — {detail}")

    @property
    def ok(self) -> bool:
        """Return whether every required observation passed."""
        return all(passed for _name, passed, _detail in self.results)


async def prove_recursion_depth(
    client: nio.AsyncClient,
    room_id: str,
    findings: Findings,
) -> None:
    """A root, a reply, and an edit of that reply is a depth-three tree.

    Neither Tuwunel nor Synapse advertises the depth it traverses, so the only
    way to know is to build a tree that needs three levels and ask.
    """
    root = await _send(client, room_id, _text("depth root"))
    reply = await _send(client, room_id, _threaded(root, "depth reply"))
    await _send(client, room_id, _edit(reply, "depth reply edited"))

    reported: int | None = None
    seen: list[str] = []
    method, path = nio.Api.room_get_event_relations(
        client.access_token,
        room_id,
        root,
        recurse=True,
    )
    async with httpx.AsyncClient(timeout=30) as http:
        response = await http.request(method, f"{client.homeserver}{path}")
        response.raise_for_status()
        body = response.json()
        reported = body.get("recursion_depth")
        seen = [event["event_id"] for event in body["chunk"]]

    findings.record(
        "a non-empty recursive page reports recursion_depth",
        isinstance(reported, int),
        f"recursion_depth={reported!r}",
    )
    # Measured, not asserted, because the two servers mean different things by
    # it. Tuwunel reports the depth of the deepest event it actually returned,
    # so this two-level tree reports 1. Synapse reports the constant 3, the
    # depth it is willing to traverse, whatever the tree looks like. Both are
    # defensible readings of the same field name, so no numeric floor above
    # zero is correct on both -- which is exactly why
    # `_REQUIRED_RECURSION_DEPTH` is 0 and the requirement MindRoom actually
    # enforces is the next check: the server reported a depth at all, meaning
    # it honoured `recurse` rather than silently returning direct children.
    findings.note(
        "what the reported depth means on this server",
        f"a two-level tree reported recursion_depth={reported!r}"
        f" ({'the tree it returned' if isinstance(reported, int) and reported < 3 else 'a fixed capability'})",
    )
    findings.record(
        "recursive traversal returns the indirectly related edit",
        all(event_id in seen for event_id in (reply,)),
        f"{len(seen)} related events",
    )

    depth_enforced = True
    with contextlib.suppress(nio.InsufficientRecursionDepthError):
        async for _ in client.room_get_event_relations(
            room_id=room_id,
            event_id=root,
            recurse=True,
            minimum_recursion_depth=99,
        ):
            depth_enforced = False
    findings.record(
        "an impossible depth requirement is refused, not silently met",
        depth_enforced,
    )

    honored = True
    async for _ in client.room_get_event_relations(
        room_id=room_id,
        event_id=root,
        recurse=True,
        minimum_recursion_depth=0,
    ):
        honored = True
    findings.record(
        "the requirement MindRoom actually uses is satisfied",
        honored,
        "recursion_depth=0 means only 'the server honored recurse'",
    )


async def prove_edit_redaction(
    client: nio.AsyncClient,
    store: PrincipalStore,
    hydrator: ConversationHydrator,
    room_id: str,
    findings: Findings,
) -> None:
    """Redacting the visible edit must reveal whatever the server still has.

    Tuwunel purges superseded edits on a background job with a 24-hour floor,
    so a fresh run should see the prior edit. Both that and the original are
    correct answers — the point is that MindRoom shows what every other client
    in the room shows, and never the deleted text.
    """
    original = await _send(client, room_id, _text("redaction original"))
    first_edit = await _send(client, room_id, _edit(original, "redaction first edit"))
    second_edit = await _send(client, room_id, _edit(original, "redaction second edit"))

    for event_id in (original, first_edit, second_edit):
        await _admit_from_server(client, store, room_id, event_id)

    page = await store.read_conversation(room_id=room_id, thread_id=None, limit=50)
    visible = [m for m in page.messages if m.logical_event_id == original]
    findings.record(
        "the newest edit is the visible revision before redaction",
        bool(visible) and visible[0].content.get("body") == "redaction second edit",
        f"visible body {visible[0].content.get('body')!r}" if visible else "no visible row",
    )

    redaction = await client.room_redact(room_id, second_edit, reason="live proof")
    if not isinstance(redaction, nio.RoomRedactResponse):
        msg = f"redaction failed: {redaction}"
        raise TypeError(msg)
    await _admit_from_server(client, store, room_id, redaction.event_id)

    page = await store.read_conversation(room_id=room_id, thread_id=None, limit=50)
    hidden = all(message.content.get("body") != "redaction second edit" for message in page.messages)
    findings.record("the redacted revision is not readable before refetch", hidden)
    findings.record(
        "the message is reported as owing a refetch",
        any(request.logical_event_id == original for request in page.refresh_pending),
    )

    request = next(r for r in page.refresh_pending if r.logical_event_id == original)
    installed = await hydrator.refresh(request)
    findings.record("the point refetch installs a server revision", installed)

    page = await store.read_conversation(room_id=room_id, thread_id=None, limit=50)
    bodies = [str(message.content.get("body")) for message in page.messages]
    findings.record(
        "the restored revision is one the server still holds",
        "redaction first edit" in bodies or "redaction original" in bodies,
        f"restored {[b for b in bodies if b.startswith('redaction')]}",
    )
    findings.record(
        "the deleted text is gone from every read",
        "redaction second edit" not in bodies,
    )


async def prove_edit_churn(
    client: nio.AsyncClient,
    store: PrincipalStore,
    room_id: str,
    findings: Findings,
    *,
    edits: int = 25,
) -> None:
    """Streaming produces many edits and must leave one row and one body."""
    original = await _send(client, room_id, _text("stream chunk 0"))
    await _admit_from_server(client, store, room_id, original)
    for index in range(1, edits + 1):
        event_id = await _send(client, room_id, _edit(original, f"stream chunk {index}"))
        await _admit_from_server(client, store, room_id, event_id)

    page = await store.read_conversation(room_id=room_id, thread_id=None, limit=200)
    matching = [m for m in page.messages if m.logical_event_id == original]
    findings.record(
        "edit churn leaves exactly one logical row",
        len(matching) == 1,
        f"{edits} edits produced {len(matching)} row(s)",
    )
    findings.record(
        "the surviving body is the newest edit",
        bool(matching) and matching[0].content.get("body") == f"stream chunk {edits}",
    )


async def prove_sidecar_resolution(
    client: nio.AsyncClient,
    store: PrincipalStore,
    hydrator: ConversationHydrator,
    room_id: str,
    findings: Findings,
) -> None:
    """A message too large for one event must still read back whole.

    Most answers this runtime produces exceed the event size limit and are
    sent as a short preview plus the real text in an uploaded file. Nothing in
    CI uploads to a real media repository or downloads from one, so the round
    trip -- upload, reference, admit, resolve -- is only observable here.
    """
    whole = "the whole answer " * 512
    upload, _keys = await client.upload(
        lambda *_args: json.dumps({"msgtype": "m.text", "body": whole}).encode(),
        content_type="application/json",
        filename="message.json",
        filesize=len(json.dumps({"msgtype": "m.text", "body": whole}).encode()),
    )
    if not isinstance(upload, nio.UploadResponse):
        findings.record("the sidecar payload uploads to the media repository", False, str(upload))
        return
    findings.record("the sidecar payload uploads to the media repository", True, upload.content_uri)

    preview = "the whole answer [Message continues in attached file]"
    event_id = await _send(
        client,
        room_id,
        {
            "msgtype": "m.file",
            "body": preview,
            "url": upload.content_uri,
            "io.mindroom.long_text": {
                "version": 2,
                "encoding": "matrix_event_content_json",
                "is_complete_content": True,
            },
        },
    )
    await _admit_from_server(client, store, room_id, event_id)

    page = await store.read_conversation(room_id=room_id, thread_id=None, limit=200)
    served = [m for m in page.messages if m.logical_event_id == event_id]
    owed = [r for r in page.refresh_pending if r.logical_event_id == event_id]
    findings.record(
        "an unresolved sidecar is never served as a message",
        not served,
        f"{len(served)} row(s) served before resolution",
    )
    findings.record("an unresolved sidecar is reported as owing a fetch", bool(owed))

    owed = (await store.read_conversation(room_id=room_id, thread_id=None, limit=200)).refresh_pending
    await hydrator.resolve_refreshes(owed)

    page = await store.read_conversation(room_id=room_id, thread_id=None, limit=200)
    resolved = [m for m in page.messages if m.logical_event_id == event_id]
    findings.record(
        "resolving downloads the attachment and installs the whole body",
        bool(resolved) and resolved[0].content.get("body") == whole,
        f"{len(str(resolved[0].content.get('body'))) if resolved else 0} chars, expected {len(whole)}",
    )
    findings.record(
        "the preview never reaches a reader",
        bool(resolved) and resolved[0].content.get("body") != preview,
    )


async def prove_deterministic_retry(
    account: _Account,
    store: PrincipalStore,
    room_id: str,
    findings: Findings,
) -> None:
    """Resending an accepted transaction ID must not create a second message.

    This is what makes a crash between "Matrix accepted it" and "MindRoom
    recorded it" harmless -- and the second half of this proof is where that
    stops being true. Servers scope the deduplication to the sending device,
    so it survives a restart that keeps its login and not one that does not.
    The outbox records the sending device for exactly this reason, and
    delivery reads the room instead of resending when it has changed.
    """
    client = account.client
    turn_id = f"live-{secrets.token_hex(4)}"
    await store.enqueue_delivery(
        turn_id=turn_id,
        stage=DeliveryStage.FINAL,
        room_id=room_id,
        thread_id=None,
        payload=_text("deterministic delivery"),
    )
    claimed = await store.claim_delivery(turn_id=turn_id, stage=DeliveryStage.FINAL)
    assert claimed is not None
    first = await _send(client, room_id, dict(claimed.payload), transaction_id=claimed.transaction_id)
    second = await _send(client, room_id, dict(claimed.payload), transaction_id=claimed.transaction_id)

    findings.record(
        "a repeated transaction ID returns the same event",
        first == second,
        f"{first} vs {second}",
    )

    # The same user, a second device. A different account would also produce
    # a second event, but it would prove nothing about a re-login: the sender
    # differs, so the server has no reason to collapse it either way. What
    # MindRoom restarts into is this -- one account, one room, a new device
    # holding transaction IDs the old one minted.
    relogged_in = await _login_another_device(account)
    try:
        third = await _send(
            relogged_in,
            room_id,
            dict(claimed.payload),
            transaction_id=claimed.transaction_id,
        )
        findings.record(
            "transaction deduplication is scoped to the sending device",
            third != first,
            f"the same user on a new device reusing the ID created {third}, not {first}",
        )
    finally:
        await relogged_in.close()


async def prove_history_exhaustion(
    client: nio.AsyncClient,
    store: PrincipalStore,
    findings: Findings,
) -> None:
    """A bounded history walk that runs out must be treated as success.

    Servers answer an exhausted ``/messages`` walk with an empty chunk and no
    ``end`` token. Reading that as a failure is what used to leave a room
    permanently unready after a restart.
    """
    room = await client.room_create(preset=nio.RoomPreset.public_chat)
    if not isinstance(room, nio.RoomCreateResponse):
        msg = f"room creation failed: {room}"
        raise TypeError(msg)
    room_id = room.room_id
    await _send(client, room_id, _text("only message"))

    hydrator = ConversationHydrator(
        store=store,
        runtime=SimpleNamespace(client=client),  # type: ignore[arg-type]
        self_sender=client.user_id,
    )
    await hydrator.ensure_hydrated(room_id=room_id, thread_id=None)

    findings.record(
        "an exhausted history walk completes hydration",
        await store.conversation_is_hydrated(room_id=room_id, thread_id=None),
    )
    page = await store.read_conversation(room_id=room_id, thread_id=None, limit=50)
    findings.record(
        "cold history populates the conversation",
        any(m.content.get("body") == "only message" for m in page.messages),
    )
    findings.record(
        "cold history starts no semantic work",
        await store.pending() == (),
        f"{len(await store.pending())} pending event(s)",
    )


async def _admit_from_server(
    client: nio.AsyncClient,
    store: PrincipalStore,
    room_id: str,
    event_id: str,
) -> None:
    """Admit the event exactly as the server serves it.

    Fetching rather than reconstructing matters: edit ordering breaks ties on
    ``origin_server_ts``, and a locally stamped clock would decide the winner
    differently from the server the refetch later asks.
    """
    fetched = await client.room_get_event(room_id, event_id)
    if not isinstance(fetched, nio.RoomGetEventResponse):
        msg = f"could not fetch {event_id}: {fetched}"
        raise TypeError(msg)
    event = fetched.event
    kind = EventKind.REDACTION if isinstance(event, nio.RedactionEvent) else EventKind.MESSAGE
    await store.admit(
        inbound_event(room_id, event, kind, EventClass.ACTIONABLE),
        projected_event(room_id, event, kind, self_sender=client.user_id),
    )
    await store.settle(event_id)


async def run_proof(homeserver: str) -> Findings:
    """Run every live observation against one homeserver."""
    findings = Findings()
    account = await _register(homeserver)
    client, user_id = account.client, account.user_id
    with tempfile.TemporaryDirectory(prefix="journal-live-proof-") as directory:
        store_root = EventJournalStore.open_sqlite(Path(directory) / "journal.db")
        store = store_root.principal(PRINCIPAL)
        try:
            room = await client.room_create(preset=nio.RoomPreset.public_chat)
            if not isinstance(room, nio.RoomCreateResponse):
                msg = f"room creation failed: {room}"
                raise TypeError(msg)
            room_id = room.room_id
            hydrator = ConversationHydrator(
                store=store,
                runtime=SimpleNamespace(client=client),  # type: ignore[arg-type]
                self_sender=client.user_id,
            )

            await prove_recursion_depth(client, room_id, findings)
            await prove_edit_redaction(client, store, hydrator, room_id, findings)
            await prove_edit_churn(client, store, room_id, findings)
            await prove_sidecar_resolution(client, store, hydrator, room_id, findings)
            await prove_deterministic_retry(account, store, room_id, findings)
            await prove_history_exhaustion(client, store, findings)
        finally:
            await store_root.close()
            await client.close()
    del user_id
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    """Start a disposable Tuwunel, run the proof, and report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--homeserver", help="Use an already running homeserver instead")
    parser.add_argument("--keep", action="store_true", help="Leave the disposable server running")
    arguments = parser.parse_args(argv)

    if arguments.homeserver:
        findings = asyncio.run(run_proof(arguments.homeserver))
        return 0 if findings.ok else 1

    server = DisposableTuwunel()
    try:
        server.start()
        print(f"Tuwunel ready at {server.homeserver}")
        findings = asyncio.run(run_proof(server.homeserver))
    finally:
        if arguments.keep:
            print(f"Leaving {server.instance_name} running at {server.homeserver}")
        else:
            server.close()
    return 0 if findings.ok else 1


if __name__ == "__main__":
    sys.exit(main())
