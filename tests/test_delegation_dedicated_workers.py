"""Subagents that run their worker-routed tool calls in a dedicated worker of their own."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, ClassVar
from unittest.mock import MagicMock

import pytest
from agno.tools import Toolkit

from mindroom.config.agent import AgentConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools import delegate as delegate_module
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation import direct as direct_module
from mindroom.delegation.lifecycle import prepare_child_turn
from mindroom.delegation.state import DelegationChild, DelegationState
from mindroom.delegation.targets import resolve_fresh_target
from mindroom.delegation.workers import child_placement
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_system.runtime_context import current_execution_identity, tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, agent_workspace_root_path
from mindroom.workers.placements import DedicatedWorkerPlacement, placement_for_session
from mindroom.workers.runtime import idle_worker_cleanup_owner
from tests.access_schema_support import with_responder_access
from tests.agent_tool_helpers import toolkit_offers
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, native_delegation_config, respond
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.scripted_agent_helpers import registered_toolkit

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_REQUESTER = "@alice:example.org"
_KUBERNETES = {"MINDROOM_WORKER_BACKEND": "kubernetes", "MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS": "30"}
_DOCKER = {"MINDROOM_WORKER_BACKEND": "docker", "MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS": "30"}


@pytest.fixture
def no_idle_cleanup() -> None:
    """Run the requesting test as a runtime that cleans up no idle workers, as ``mindroom run --no-api`` does."""


@pytest.fixture(autouse=True)
def _idle_worker_cleanup(request: pytest.FixtureRequest) -> Iterator[None]:
    """Run every other test as a runtime whose API server cleans up idle workers."""
    if "no_idle_cleanup" in request.fixturenames:
        yield
        return
    with idle_worker_cleanup_owner():
        yield


def _paths(tmp_path: Path, env: dict[str, str]) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env=dict(env))


def _config(paths: RuntimePaths, *, allowed: bool = True) -> Config:
    config = with_responder_access(
        native_delegation_config(
            {
                "leader": AgentConfig(
                    display_name="Leader",
                    delegate_to=["leader", "code"],
                    dedicated_subagent_workers=allowed,
                ),
                "code": AgentConfig(display_name="Code", tools=["placement_probe"], delegate_to=["code"]),
            },
            models={"default": {"provider": "openai", "id": "test-model"}},
        ),
        "code",
        users=[_REQUESTER],
    )
    entity_ids(config, paths)
    return config


def _identity(agent_name: str = "leader") -> ToolExecutionIdentity:
    return ToolExecutionIdentity("matrix", agent_name, _REQUESTER, "!room:example.org", "$thread", "$thread", "parent")


def _tools(config: Config, paths: RuntimePaths) -> DelegateTools:
    return DelegateTools(
        agent_name="leader",
        runtime_paths=paths,
        config=config,
        workspace_root=agent_workspace_root_path(paths.storage_root, "leader"),
        execution_identity=_identity(),
    )


def _child(config: Config, paths: RuntimePaths, **worker: object) -> DelegationChild:
    return prepare_child_turn(
        "leader",
        "code",
        "task",
        owner=_identity(),
        config=config,
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
        model=None,
        agent_mode="standard",
        previous=None,
        persona=None,
        **worker,  # type: ignore[arg-type]
    )


class PlacementProbeTools(Toolkit):
    """Record the worker placement the calling child's session has when the child calls ``probe``."""

    seen: ClassVar[list[DedicatedWorkerPlacement | None]] = []

    def __init__(self) -> None:
        super().__init__(name="placement_probe", tools=[self.probe])

    def probe(self) -> str:
        """Report where this call's worker tools run."""
        identity = current_execution_identity()
        type(self).seen.append(placement_for_session(identity.session_id if identity is not None else None))
        return "probed"


@pytest.mark.parametrize(
    ("env", "allowed", "offered"),
    [
        (_KUBERNETES, True, {"dedicated_worker", "resource_profile"}),
        (_DOCKER, True, {"dedicated_worker"}),
        (_KUBERNETES, False, set()),
        ({**_KUBERNETES, "MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS": "0"}, True, set()),
        ({}, True, set()),
    ],
    ids=["kubernetes", "docker", "not-allowed", "no-cleanup", "static-runner"],
)
def test_dedicated_options_are_offered_only_where_they_work(
    tmp_path: Path,
    env: dict[str, str],
    *,
    allowed: bool,
    offered: set[str],
) -> None:
    """The caller sees the worker options only with its setting, a dedicated backend, and idle cleanup."""
    paths = _paths(tmp_path, env)
    function = toolkit_offers(_tools(_config(paths, allowed=allowed), paths), "run_subagent")

    assert set(function.parameters["properties"]) & {"dedicated_worker", "resource_profile"} == offered
    assert ("dedicated_worker" in (function.description or "")) == bool(offered)


@pytest.mark.usefixtures("no_idle_cleanup")
def test_a_runtime_without_idle_cleanup_offers_no_dedicated_workers(tmp_path: Path) -> None:
    """Without a running cleanup loop dedicated workers would never be removed, so none are offered or placed."""
    paths = _paths(tmp_path, _KUBERNETES)
    config = _config(paths)
    child = _child(config, paths, dedicated_worker=True)

    assert "dedicated_worker" not in toolkit_offers(_tools(config, paths), "run_subagent").parameters["properties"]
    assert child_placement(child, config, paths) is None
    with idle_worker_cleanup_owner():
        assert "dedicated_worker" in toolkit_offers(_tools(config, paths), "run_subagent").parameters["properties"]
        assert child_placement(child, config, paths) == DedicatedWorkerPlacement(child.subagent_id)


@pytest.mark.parametrize(
    ("env", "allowed", "args", "refusal"),
    [
        (
            _KUBERNETES,
            False,
            {"dedicated_worker": True},
            "Cannot delegate: leader may not run subagents in dedicated workers.",
        ),
        (
            {},
            True,
            {"dedicated_worker": True},
            "Cannot delegate: dedicated workers need the docker or kubernetes worker backend and idle-worker "
            "cleanup, which the API server runs when MINDROOM_WORKER_CLEANUP_INTERVAL_SECONDS is above 0.",
        ),
        (
            _KUBERNETES,
            True,
            {"resource_profile": "large"},
            "Cannot delegate: resource_profile requires dedicated_worker=true.",
        ),
        (
            _DOCKER,
            True,
            {"dedicated_worker": True, "resource_profile": "large"},
            "Cannot delegate: resource_profile requires the kubernetes worker backend.",
        ),
        (
            _KUBERNETES,
            True,
            {"dedicated_worker": True, "resource_profile": "huge"},
            "Cannot delegate: resource_profile must be small, standard, or large.",
        ),
        (
            _KUBERNETES,
            True,
            {"dedicated_worker": "yes"},
            "Cannot delegate: dedicated_worker must be a boolean or null.",
        ),
    ],
    ids=["not-allowed", "no-backend", "profile-alone", "profile-on-docker", "unknown-profile", "not-a-boolean"],
)
def test_a_fresh_child_that_cannot_have_its_own_worker_is_refused(
    tmp_path: Path,
    env: dict[str, str],
    args: dict[str, object],
    refusal: str,
    *,
    allowed: bool,
) -> None:
    """Each reason a fresh child cannot get the worker it asked for is reported before it starts."""
    paths = _paths(tmp_path, env)

    result = resolve_fresh_target(
        {"agent_name": "code", "task": "task", **args},
        caller_name="leader",
        config=_config(paths, allowed=allowed),
        runtime_paths=paths,
        depth=0,
        workspace_root=lambda: agent_workspace_root_path(paths.storage_root, "leader"),
        cap=None,
    )

    assert result == refusal


def test_a_dedicated_child_records_its_choice_and_its_follow_ups_keep_it(tmp_path: Path) -> None:
    """A follow-up keeps its child's worker and profile; a child's own children choose for themselves."""
    paths = _paths(tmp_path, _KUBERNETES)
    config = _config(paths)

    child = _child(config, paths, dedicated_worker=True, worker_resource_profile="large")
    follow_up = prepare_child_turn(
        "leader",
        "code",
        "again",
        owner=_identity(),
        config=config,
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
        model=None,
        agent_mode="standard",
        previous=child,
        persona=None,
    )
    grandchild = prepare_child_turn(
        "code",
        "code",
        "nested",
        owner=_identity("code"),
        config=config,
        runtime_paths=paths,
        depth=1,
        budget_monitor=None,
        model=None,
        agent_mode="standard",
        previous=None,
        persona=None,
    )

    assert (child.dedicated_worker, child.worker_resource_profile) == (True, "large")
    assert (follow_up.dedicated_worker, follow_up.worker_resource_profile) == (True, "large")
    assert follow_up.subagent_id == child.subagent_id
    assert child_placement(child, config, paths) == DedicatedWorkerPlacement(child.subagent_id, "large")
    assert child_placement(grandchild, config, paths) is None
    assert grandchild.session_id != child.session_id


def test_a_child_whose_caller_lost_the_setting_runs_on_its_normal_worker(tmp_path: Path) -> None:
    """Each attempt rechecks its caller's setting and the backend, so later turns fall back to the normal worker."""
    paths = _paths(tmp_path, _KUBERNETES)
    child = _child(_config(paths), paths, dedicated_worker=True)

    assert child_placement(child, _config(paths, allowed=False), paths) is None
    assert child_placement(child, _config(paths), _paths(tmp_path / "static", {})) is None


def test_child_snapshot_without_worker_fields_runs_on_its_normal_worker(tmp_path: Path) -> None:
    """A child an earlier release stored reads as running on its normal worker."""
    paths = _paths(tmp_path, _KUBERNETES)
    config = _config(paths)
    stored = asdict(_child(config, paths, dedicated_worker=True, worker_resource_profile="small"))
    del stored["dedicated_worker"], stored["worker_resource_profile"]

    child = DelegationChild.from_dict(stored)

    assert (child.dedicated_worker, child.worker_resource_profile) == (False, None)
    assert child_placement(child, config, paths) is None


@pytest.mark.asyncio
async def test_a_native_child_calls_its_tools_placed_in_its_own_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tools of a child started with dedicated_worker run placed in that child's worker, and only during its run."""
    paths = _paths(tmp_path, _KUBERNETES)
    config = _config(paths)
    PlacementProbeTools.seen.clear()
    leader = ScriptedModel(
        [
            calls(
                tool_call(
                    "run_subagent",
                    "delegate-0",
                    agent_name="code",
                    task="Probe",
                    dedicated_worker=True,
                    resource_profile="large",
                ),
            ),
            answer("Parent done"),
        ],
    )
    code = ScriptedModel([calls(tool_call("probe", "probe-0")), answer("Probed")])
    ScriptedAgents({"leader": leader, "code": code}).install(monkeypatch)

    with registered_toolkit("placement_probe", PlacementProbeTools):
        await respond("leader", "delegate", config=config, runtime_paths=paths, identity=_identity(), run_id="parent")

    run = create_session_storage("leader", config, paths, _identity()).get_run("parent")
    assert run is not None
    [child] = DelegationState.from_metadata(run.metadata).children
    assert (child.dedicated_worker, child.worker_resource_profile) == (True, "large")
    assert PlacementProbeTools.seen == [DedicatedWorkerPlacement(child.subagent_id, "large")]
    assert placement_for_session(child.session_id) is None


@pytest.mark.asyncio
async def test_a_direct_child_runs_placed_in_its_own_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The direct adapter forwards the choice and runs the child placed in its worker."""
    paths = _paths(tmp_path, _DOCKER)
    config = _config(paths)
    seen: list[DedicatedWorkerPlacement | None] = []

    async def run_child(child: DelegationChild, **_kwargs: object) -> str:
        seen.append(placement_for_session(child.session_id))
        return "done"

    monkeypatch.setattr(direct_module, "run_delegated_child_response", run_child)
    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=_identity())):
        result = await _tools(config, paths).run_subagent(agent_name="code", task="Probe", dedicated_worker=True)

    assert result.startswith("done")
    [placement] = seen
    assert placement is not None
    assert placement.resource_profile is None


@pytest.mark.asyncio
async def test_a_background_child_records_its_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A managed background call forwards the choice into the child its job runs."""
    paths = _paths(tmp_path, _KUBERNETES)
    config = _config(paths)
    jobs: list[DelegationChild] = []

    def delegation_call_job(child: DelegationChild, **_kwargs: object) -> MagicMock:
        jobs.append(child)
        return MagicMock()

    monkeypatch.setattr(delegate_module, "delegation_call_job", delegation_call_job)
    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=_identity())):
        await _tools(config, paths).managed_job(
            "run_subagent",
            {"agent_name": "code", "task": "Probe", "dedicated_worker": True, "resource_profile": "small"},
            job_id="job-1",
            runtime=MagicMock(),
        )

    [child] = jobs
    assert (child.dedicated_worker, child.worker_resource_profile) == (True, "small")
