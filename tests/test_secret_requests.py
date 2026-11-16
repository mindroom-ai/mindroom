"""Tests for the pending secret request registry and card content."""

from __future__ import annotations

import stat
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom import secret_requests
from mindroom.secret_requests import (
    SECRET_REQUEST_TTL_SECONDS,
    TEMPORARY_SECRET_KEEP_SECONDS,
    KeyTarget,
    TemporaryTarget,
    _SecretHolder,
    _SecretRequestRegistry,
    _TemporarySecrets,
    secret_holder,
    secret_request_body,
    secret_request_content,
    secret_request_registry,
    temporary_secret_body,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.egress_broker.conftest import worker_target

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.secret_requests import SecretKeep

ROOM = "!room:example.org"


def _register(registry: _SecretRequestRegistry, *, now: float = 1000.0) -> secret_requests.PendingSecretRequest:
    return registry.register(
        room_id=ROOM,
        requester_id="@alice:example.org",
        agent_name="code",
        target=KeyTarget(service="github", worker_target=worker_target(), hosts=("api.github.com",)),
        now=now,
    )


@pytest.mark.asyncio
async def test_claim_is_single_use() -> None:
    """A second claim of the same request is refused."""
    registry = _SecretRequestRegistry()
    request = _register(registry)
    assert registry.claim(request.request_id, room_id=ROOM, now=1001.0) is request
    assert registry.claim(request.request_id, room_id=ROOM, now=1001.0) is None


@pytest.mark.asyncio
async def test_release_lets_a_failed_save_retry() -> None:
    """Releasing a claim makes the request claimable again and tells a waiting tool no save is in flight."""
    registry = _SecretRequestRegistry()
    request = _register(registry)
    assert registry.claim(request.request_id, room_id=ROOM, now=1001.0) is request
    assert not request.claim_released.is_set()
    registry.release(request.request_id)
    assert request.claim_released.is_set()
    assert registry.claim(request.request_id, room_id=ROOM, now=1002.0) is request
    assert not request.claim_released.is_set()


@pytest.mark.asyncio
async def test_claim_refuses_other_room_and_expired() -> None:
    """Unknown, other-room, expired and finished requests cannot be claimed."""
    registry = _SecretRequestRegistry()
    request = _register(registry)
    assert request.expires_at == 1000.0 + SECRET_REQUEST_TTL_SECONDS
    assert registry.claim("unknown", room_id=ROOM, now=1001.0) is None
    assert registry.claim(request.request_id, room_id="!other:example.org", now=1001.0) is None
    assert registry.claim(request.request_id, room_id=ROOM, now=request.expires_at) is None
    done = _register(registry)
    registry.resolve(done.request_id, "declined")
    assert registry.claim(done.request_id, room_id=ROOM, now=1001.0) is None


@pytest.mark.asyncio
async def test_resolve_sets_outcome_once() -> None:
    """The first outcome wins and later ones are ignored."""
    registry = _SecretRequestRegistry()
    request = _register(registry)
    registry.resolve(request.request_id, "saved")
    registry.resolve(request.request_id, "expired")
    assert request.outcome.result() == "saved"
    registry.resolve("unknown", "saved")


@pytest.mark.asyncio
async def test_discard_forgets_the_request() -> None:
    """A discarded request is gone."""
    registry = _SecretRequestRegistry()
    request = _register(registry)
    registry.discard(request.request_id)
    registry.discard(request.request_id)
    assert registry.claim(request.request_id, room_id=ROOM, now=1001.0) is None


@pytest.mark.asyncio
async def test_ttl_is_read_at_call_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkeypatching the module constant changes the next expiry."""
    monkeypatch.setattr(secret_requests, "SECRET_REQUEST_TTL_SECONDS", 5)
    assert _register(_SecretRequestRegistry()).expires_at == 1005.0


def test_registry_is_a_singleton() -> None:
    """The accessor returns one shared registry."""
    assert secret_request_registry() is secret_request_registry()


@pytest.mark.asyncio
async def test_content_carries_metadata_only() -> None:
    """The card content has exactly the documented keys and millisecond expiry."""
    request = _register(_SecretRequestRegistry())
    content = secret_request_content(
        request,
        display_name="GitHub",
        reason="to open a PR",
        status="pending",
    )
    assert set(content) == {
        "request_id",
        "requester_id",
        "service",
        "display_name",
        "hosts",
        "reason",
        "status",
        "expires_at",
    }
    assert content["expires_at"] == int(request.expires_at * 1000)
    assert content["hosts"] == ["api.github.com"]


def test_bodies() -> None:
    """Each status has its body text."""
    kwargs = {"agent_display_name": "Code", "display_name": "GitHub"}
    assert (
        secret_request_body("pending", **kwargs) == "🔑 Code asks for your GitHub key. Open MindRoom Chat to enter it."
    )
    assert secret_request_body("saved", **kwargs) == "🔑 GitHub key saved."
    assert secret_request_body("declined", **kwargs) == "🔑 GitHub key request declined."
    assert secret_request_body("expired", **kwargs) == "🔑 GitHub key request expired."


HOLDER = _SecretHolder(agent_name="code", room_id=ROOM, thread_id="$thread", requester_id="@alice:example.org")
IDENTITY = ToolExecutionIdentity(
    channel="matrix",
    agent_name="code",
    requester_id="@alice:example.org",
    room_id=ROOM,
    thread_id=None,
    resolved_thread_id="$thread",
    session_id="session-1",
)


def _files(storage_root: Path) -> list[Path]:
    directory = storage_root / "temporary_secrets"
    return sorted(directory.iterdir()) if directory.exists() else []


@pytest.mark.parametrize("keep", ["memory", "disk"])
def test_a_given_value_is_read_until_it_expires(tmp_path: Path, keep: SecretKeep) -> None:
    """Each keep has its own lifetime: 15 minutes in memory, 24 hours on disk."""
    store = _TemporarySecrets()
    store.give(HOLDER, "TOKEN", "s3cret", keep=keep, now=1000.0, storage_root=tmp_path)
    lifetime = TEMPORARY_SECRET_KEEP_SECONDS[keep]

    assert store.values(HOLDER, now=1000.0 + lifetime - 1, storage_root=tmp_path) == {"TOKEN": "s3cret"}
    assert store.values(HOLDER, now=1000.0 + lifetime, storage_root=tmp_path) == {}
    assert (TEMPORARY_SECRET_KEEP_SECONDS["memory"], TEMPORARY_SECRET_KEEP_SECONDS["disk"]) == (900, 86400)


@pytest.mark.parametrize(
    "other",
    [
        replace(HOLDER, agent_name="other"),
        replace(HOLDER, room_id="!other:example.org"),
        replace(HOLDER, thread_id=None),
        replace(HOLDER, requester_id="@bob:example.org"),
    ],
)
@pytest.mark.parametrize("keep", ["memory", "disk"])
def test_only_the_same_agent_room_thread_and_requester_reads_a_value(
    tmp_path: Path,
    other: _SecretHolder,
    keep: SecretKeep,
) -> None:
    """A value belongs to one agent's conversation with one requester."""
    store = _TemporarySecrets()
    store.give(HOLDER, "TOKEN", "s3cret", keep=keep, now=1000.0, storage_root=tmp_path)

    assert store.values(other, now=1001.0, storage_root=tmp_path) == {}
    assert _TemporarySecrets().values(other, now=1001.0, storage_root=tmp_path) == {}


def test_a_new_value_replaces_the_same_name(tmp_path: Path) -> None:
    """The latest answer for a variable is the one commands receive, beside the conversation's other variables."""
    store = _TemporarySecrets()
    store.give(HOLDER, "TOKEN", "old", keep="memory", now=1000.0, storage_root=tmp_path)
    store.give(HOLDER, "OTHER", "kept", keep="memory", now=1000.0, storage_root=tmp_path)
    store.give(HOLDER, "TOKEN", "new", keep="memory", now=1001.0, storage_root=tmp_path)

    assert store.values(HOLDER, now=1002.0, storage_root=tmp_path) == {"TOKEN": "new", "OTHER": "kept"}


def test_a_holder_needs_an_agent_room_and_requester() -> None:
    """A call outside a person's Matrix conversation holds no values."""
    assert secret_holder(IDENTITY) == HOLDER
    assert secret_holder(None) is None
    assert secret_holder(replace(IDENTITY, agent_name="")) is None
    assert secret_holder(replace(IDENTITY, room_id=None)) is None
    assert secret_holder(replace(IDENTITY, requester_id=None)) is None


def test_a_memory_value_leaves_no_file(tmp_path: Path) -> None:
    """The default keep never touches the disk."""
    _TemporarySecrets().give(HOLDER, "TOKEN", "s3cret", keep="memory", now=1000.0, storage_root=tmp_path)

    assert _files(tmp_path) == []
    assert not any("s3cret" in path.read_text() for path in tmp_path.rglob("*") if path.is_file())


def test_a_disk_value_survives_a_restart(tmp_path: Path) -> None:
    """A value kept on disk lies in one private file, which a new process reads back."""
    _TemporarySecrets().give(HOLDER, "TOKEN", "s3cret", keep="disk", now=1000.0, storage_root=tmp_path)

    (file,) = _files(tmp_path)
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert stat.S_IMODE(file.parent.stat().st_mode) == 0o700
    assert _TemporarySecrets().values(HOLDER, now=1001.0, storage_root=tmp_path) == {"TOKEN": "s3cret"}


def test_an_expired_disk_value_s_file_is_deleted(tmp_path: Path) -> None:
    """The file goes when its value expires, in the process that wrote it or a later one."""
    store = _TemporarySecrets()
    store.give(HOLDER, "TOKEN", "s3cret", keep="disk", now=1000.0, storage_root=tmp_path)
    store.give(HOLDER, "OTHER", "kept", keep="disk", now=2000.0, storage_root=tmp_path)
    after_the_first = 1000.0 + TEMPORARY_SECRET_KEEP_SECONDS["disk"]

    assert store.values(HOLDER, now=after_the_first, storage_root=tmp_path) == {"OTHER": "kept"}
    assert len(_files(tmp_path)) == 1
    assert _TemporarySecrets().values(HOLDER, now=after_the_first + 1000, storage_root=tmp_path) == {}
    assert _files(tmp_path) == []


def test_a_memory_value_replacing_a_disk_one_deletes_the_file(tmp_path: Path) -> None:
    """A later answer kept in memory leaves no older copy of the variable on disk."""
    store = _TemporarySecrets()
    store.give(HOLDER, "TOKEN", "old", keep="disk", now=1000.0, storage_root=tmp_path)
    store.give(HOLDER, "TOKEN", "new", keep="memory", now=1001.0, storage_root=tmp_path)

    assert _files(tmp_path) == []
    assert store.values(HOLDER, now=1002.0, storage_root=tmp_path) == {"TOKEN": "new"}
    assert _TemporarySecrets().values(HOLDER, now=1002.0, storage_root=tmp_path) == {}


def test_a_disk_value_replaces_its_own_file(tmp_path: Path) -> None:
    """A variable has one file however often it is given."""
    store = _TemporarySecrets()
    store.give(HOLDER, "TOKEN", "old", keep="disk", now=1000.0, storage_root=tmp_path)
    store.give(HOLDER, "TOKEN", "new", keep="disk", now=1001.0, storage_root=tmp_path)

    assert len(_files(tmp_path)) == 1
    assert _TemporarySecrets().values(HOLDER, now=1002.0, storage_root=tmp_path) == {"TOKEN": "new"}


def test_an_unreadable_file_is_deleted_not_fatal(tmp_path: Path) -> None:
    """A file that is no stored value never stops tool calls, and does not stay."""
    directory = tmp_path / "temporary_secrets"
    directory.mkdir()
    (directory / "broken.json").write_text("not json", encoding="utf-8")
    (directory / "partial.json").write_text('{"name": "TOKEN"}', encoding="utf-8")

    assert _TemporarySecrets().values(HOLDER, now=1000.0, storage_root=tmp_path) == {}
    assert _files(tmp_path) == []


@pytest.mark.asyncio
async def test_temporary_content_has_no_service_fields() -> None:
    """A temporary card names its variable and the keeps to choose from, so an older Chat reads no key card in it."""
    request = _SecretRequestRegistry().register(
        room_id=ROOM,
        requester_id="@alice:example.org",
        agent_name="code",
        target=TemporaryTarget(name="SUDO_PASSWORD", holder=HOLDER),
        now=1000.0,
    )

    pending = secret_request_content(request, display_name="", reason="To restart nginx.", status="pending")
    request.kept = "disk"
    saved = secret_request_content(request, display_name="", reason="To restart nginx.", status="saved")

    assert pending == {
        "request_id": request.request_id,
        "requester_id": "@alice:example.org",
        "env": "SUDO_PASSWORD",
        "reason": "To restart nginx.",
        "status": "pending",
        "expires_at": int(request.expires_at * 1000),
        "keep": {"memory": 900, "disk": 86400},
    }
    assert saved == {**pending, "status": "saved", "kept": "disk"}


def test_temporary_bodies() -> None:
    """Each status of a temporary card has its body text, which never says a key was saved."""
    kwargs = {"agent_display_name": "Code", "name": "SUDO_PASSWORD"}
    assert (
        temporary_secret_body("pending", **kwargs)
        == "🔑 Code asks for a secret (SUDO_PASSWORD). Open MindRoom Chat to enter it."
    )
    assert temporary_secret_body("saved", **kwargs) == "🔑 Secret received."
    assert temporary_secret_body("declined", **kwargs) == "🔑 Secret request declined."
    assert temporary_secret_body("expired", **kwargs) == "🔑 Secret request expired."


def test_a_file_that_cannot_be_deleted_never_fails_a_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tool calls read the store, so a file the process cannot remove must not stop them."""
    store = _TemporarySecrets()
    store.give(HOLDER, "TOKEN", "s3cret", keep="disk", now=1000.0, storage_root=tmp_path)

    def refuse(_path: Path, *, missing_ok: bool = False) -> None:  # noqa: ARG001
        raise PermissionError

    monkeypatch.setattr(type(tmp_path), "unlink", refuse)
    expired = 1000.0 + TEMPORARY_SECRET_KEEP_SECONDS["disk"]

    assert store.values(HOLDER, now=expired, storage_root=tmp_path) == {}
    assert _TemporarySecrets().values(HOLDER, now=expired, storage_root=tmp_path) == {}


def test_an_unreadable_directory_never_fails_a_read(tmp_path: Path) -> None:
    """A store directory that is no directory leaves the conversation without stored values, not without tools."""
    (tmp_path / "temporary_secrets").write_text("in the way", encoding="utf-8")

    assert _TemporarySecrets().values(HOLDER, now=1000.0, storage_root=tmp_path) == {}


def test_a_file_left_by_an_interrupted_write_is_deleted(tmp_path: Path) -> None:
    """A crash between writing a value and publishing its file leaves a copy, which the next start removes."""
    directory = tmp_path / "temporary_secrets"
    directory.mkdir()
    (directory / ".0123.json.abcdef.tmp").write_text('{"value": "s3cret"}', encoding="utf-8")

    assert _TemporarySecrets().values(HOLDER, now=1000.0, storage_root=tmp_path) == {}
    assert _files(tmp_path) == []
