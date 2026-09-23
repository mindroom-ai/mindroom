"""Released job snapshots retain evidence without replaying historical execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mindroom.config.main import Config
from mindroom.tool_jobs.authorization import locally_allowed
from mindroom.tool_jobs.runtime import ToolJobRuntime, read_job_snapshot
from mindroom.tool_system.construction import tool_config_signature

_FIXTURES = Path(__file__).parent / "fixtures" / "tool_jobs" / "v2026.9.165"


def _install_snapshot(root: Path, name: str) -> Path:
    directory = root / "tool_jobs"
    directory.mkdir()
    target = directory / f"{name}.json"
    target.write_bytes((_FIXTURES / target.name).read_bytes())
    return target


@pytest.mark.asyncio
@pytest.mark.parametrize("notified_generation", [1, 2])
@pytest.mark.parametrize("consumed", [False, True])
async def test_released_notification_does_not_replace_consumption(
    tmp_path: Path,
    notified_generation: int,
    consumed: bool,
) -> None:
    """Only the exact old notification suppresses redelivery; explicit reads remain lossless."""
    path = _install_snapshot(tmp_path, "completed")
    payload = json.loads(path.read_text())
    payload["deliveries"][0]["generation"] = notified_generation
    payload["wait_acknowledged"] = consumed
    path.write_text(json.dumps(payload))
    original = read_job_snapshot(path)
    for _ in range(2):
        runtime = ToolJobRuntime(tmp_path)
        try:
            await runtime.recover()
            saved = await runtime.lookup("completed", owner=original.owner, depth=0)
            assert saved.updated_at == original.updated_at
            assert saved.wait_acknowledged is consumed
            assert saved.result == "saved result"
            assert saved.result_payload == {"value": [1]}
            assert "config_signature" not in saved.adapter["authority"]["construction"]
            pending = [item.job_id for item in await runtime.pending_outcomes()]
            assert pending == (["completed"] if not consumed and notified_generation != 2 else [])
            waited = await runtime.wait("completed", owner=saved.owner, depth=0)
            assert waited.job.result == "saved result"
            await runtime.release_wait("completed", waited.token)
        finally:
            await runtime.shutdown()
    normalized = json.loads(path.read_text())
    assert normalized["schema_version"] == 2
    assert "deliveries" not in normalized
    assert "human_paused" not in normalized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "status", "result"),
    [
        ("paused", "interrupted", "not replayed"),
        ("native", "completed", "finished child result"),
    ],
)
async def test_released_execution_recovers_only_durable_outcomes(
    tmp_path: Path,
    name: str,
    status: str,
    result: str,
) -> None:
    """An abandoned hold interrupts; a finished native child's sole result survives."""
    path = _install_snapshot(tmp_path, name)
    original = read_job_snapshot(path)
    runtime = ToolJobRuntime(tmp_path)
    try:
        await runtime.recover()
        saved = await runtime.lookup(name, owner=original.owner, depth=0)
        assert saved.status == status
        assert result in (saved.result or "")
        assert saved.generation == original.generation
        if name == "native":
            assert saved.adapter["child"]["result"] is None
        assert [item.job_id for item in await runtime.pending_outcomes()] == [name]
    finally:
        await runtime.shutdown()


def test_constructor_identity_is_a_digest_with_separate_function_filters() -> None:
    """Secrets do not survive in access evidence; order and function filters do not change identity."""
    options = {"token": "synthetic-constructor-secret", "nested": {"endpoint": "example.test"}}
    signature = tool_config_signature(options)
    assert len(signature) == 64
    assert "synthetic-constructor-secret" not in signature
    assert signature == tool_config_signature({"nested": options["nested"], "token": options["token"]})
    assert signature == tool_config_signature({**options, "include_tools": ["add"], "exclude_tools": ["subtract"]})
    assert signature != tool_config_signature({**options, "token": "changed"})


def test_released_missing_constructor_proof_remains_unauthorized(tmp_path: Path) -> None:
    """Recovery cannot infer that an old invocation used today's constructor settings."""
    job = read_job_snapshot(_install_snapshot(tmp_path, "completed"))
    config = Config.model_validate(
        {
            "agents": {
                "parent": {
                    "display_name": "Parent",
                    "tools": ["calculator"],
                    "worker_scope": "shared",
                },
            },
        },
    )
    options = {
        "tool_name": job.tool_name,
        "toolkit_name": job.toolkit_name,
        "depth": job.depth,
        "origin": job.adapter["origin"],
        "authority": job.adapter["authority"],
    }
    assert not locally_allowed(config, job.owner, **options)
    job.adapter["authority"]["construction"]["config_signature"] = tool_config_signature(None)
    assert locally_allowed(config, job.owner, **options)


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_raw_constructor_identity_is_scrubbed_on_recovery(tmp_path: Path, expired: bool) -> None:
    """Schema-one constructor evidence is hashed once even when the result already expired."""
    path = _install_snapshot(tmp_path, "completed")
    payload = json.loads(path.read_text())
    payload.pop("deliveries")
    payload.pop("human_paused")
    options = {"token": "synthetic-constructor-secret"}
    payload["adapter"]["authority"]["construction"]["config_signature"] = json.dumps(options, sort_keys=True)
    payload["wait_acknowledged"] = True
    payload["result_expired"] = expired
    path.write_text(json.dumps(payload))
    original = read_job_snapshot(path)
    for _ in range(2):
        runtime = ToolJobRuntime(tmp_path)
        try:
            await runtime.recover()
            saved = await runtime.lookup("completed", owner=original.owner, depth=0)
            assert saved.adapter["authority"]["construction"]["config_signature"] == tool_config_signature(options)
            assert saved.updated_at == payload["updated_at"]
            assert saved.result_expired is expired
        finally:
            await runtime.shutdown()
        assert "synthetic-constructor-secret" not in path.read_text()
