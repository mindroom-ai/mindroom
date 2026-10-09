"""Records the primary trusts leave the state roots that sandbox runners write, once."""

from __future__ import annotations

import importlib
import json
import os
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from mindroom.agent_modes import resolve_agent_mode
from mindroom.constants import resolve_runtime_paths
from mindroom.legacy_state_root_records import migrate_state_root_records
from mindroom.matrix.invited_rooms_store import (
    invited_rooms_path,
    load_invited_rooms,
    load_pending_room_invites,
    pending_room_invites_path,
)
from mindroom.matrix.personal_room_store import (
    PersonalRoomRecord,
    personal_room_digest,
    personal_room_record_path,
    read_personal_room,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_ALICE = "@alice:example.org"
_MODE_KEY = json.dumps(["helper", "session"], separators=(",", ":"))


class _MigrationRanError(Exception):
    pass


def _runtime(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _mode_choices() -> str:
    record = {"agent": "helper", "session": "session", "mode": "minimal", "set_by": _ALICE, "set_at": "2026-10-01"}
    return json.dumps({_MODE_KEY: record})


@pytest.mark.asyncio
async def test_startup_moves_valid_records_once(tmp_path: Path) -> None:
    """Every kind of record reaches its primary-only path, and later starts never read the old paths again."""
    paths = _runtime(tmp_path)
    storage = paths.storage_root
    record = PersonalRoomRecord(user_id=_ALICE, alias="#personal:example.org", source_room_id="!lobby:example.org")
    old_personal_room = storage / "agents" / "helper" / "personal_rooms" / f"{personal_room_digest(_ALICE)}.json"
    _write(storage / "agents" / "router" / "invited_rooms.json", '["!invited:example.org"]')
    _write(storage / "agents" / "helper" / "pending_room_invites.json", json.dumps({"!pending:example.org": _ALICE}))
    _write(storage / "agents" / "helper" / "agent_modes.json", _mode_choices())
    _write(storage / "private_instances" / "scope" / "helper" / "agent_modes.json", _mode_choices())
    _write(old_personal_room, record.model_dump_json())

    await migrate_state_root_records(paths)

    assert load_invited_rooms(invited_rooms_path(paths, "router")) == {"!invited:example.org"}
    assert load_pending_room_invites(pending_room_invites_path(paths, "helper")) == {"!pending:example.org": _ALICE}
    for state_root in (storage / "agents" / "helper", storage / "private_instances" / "scope" / "helper"):
        assert resolve_agent_mode(paths, state_root, "helper", "session") == "minimal"
    assert read_personal_room(personal_room_record_path(paths, "helper", _ALICE)) == record
    assert not [path for path in (storage / "agents").rglob("*") if path.is_file()]
    assert not [path for path in (storage / "private_instances").rglob("*") if path.is_file()]

    _write(storage / "agents" / "router" / "invited_rooms.json", '["!planted:example.org"]')
    await migrate_state_root_records(paths)

    assert load_invited_rooms(invited_rooms_path(paths, "router")) == {"!invited:example.org"}


@pytest.mark.asyncio
async def test_planted_entries_stay_behind_without_stopping_startup(tmp_path: Path) -> None:
    """Junk, links, FIFOs, misbound records, and records their reader cannot decode stay where workers put them."""
    paths = _runtime(tmp_path)
    agent = paths.storage_root / "agents" / "helper"
    _write(agent / "invited_rooms.json", "{}")
    # The invited-room reader decodes strictly as UTF-8, so it would reject a BOM that json.loads on bytes accepts.
    bom_ledger = paths.storage_root / "agents" / "planner" / "invited_rooms.json"
    bom_ledger.parent.mkdir(parents=True)
    bom_ledger.write_bytes(b'\xef\xbb\xbf["!a:example.org"]')
    os.mkfifo(agent / "pending_room_invites.json")
    _write(tmp_path / "outside.json", _mode_choices())
    (agent / "agent_modes.json").symlink_to(tmp_path / "outside.json")
    misbound = PersonalRoomRecord(user_id="@bob:example.org", alias="#bob:example.org", source_room_id="!lobby:x")
    _write(agent / "personal_rooms" / f"{personal_room_digest(_ALICE)}.json", misbound.model_dump_json())
    _write(agent / "personal_rooms" / "junk.json", "{}")

    with capture_logs() as logs:
        await migrate_state_root_records(paths)

    assert len([log for log in logs if log["log_level"] == "warning"]) == 6
    assert load_invited_rooms(invited_rooms_path(paths, "helper")) == set()
    assert load_invited_rooms(invited_rooms_path(paths, "planner")) == set()
    assert bom_ledger.exists()
    assert load_pending_room_invites(pending_room_invites_path(paths, "helper")) == {}
    assert resolve_agent_mode(paths, agent, "helper", "session") == "standard"
    assert read_personal_room(personal_room_record_path(paths, "helper", _ALICE)) is None
    assert len(list(agent.rglob("*.json"))) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["api", "orchestrator"])
async def test_both_entry_points_move_records_before_serving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    """Standalone API and orchestrator startup both move the records before anything reads them."""
    paths = _runtime(tmp_path)
    module = importlib.import_module(f"mindroom.{'api.main' if entrypoint == 'api' else 'orchestrator'}")

    async def migration(runtime_paths: RuntimePaths) -> None:
        assert runtime_paths == paths
        raise _MigrationRanError

    monkeypatch.setattr(module, "migrate_state_root_records", migration)
    if entrypoint == "api":
        monkeypatch.setattr(module, "_app_runtime_paths", lambda _app: paths)
        with pytest.raises(_MigrationRanError):
            async with module._lifespan(module.app):
                pytest.fail("API admitted runtime work")
    else:
        with pytest.raises(_MigrationRanError):
            await module.main("ERROR", paths, api=False)
