"""A turn:started or turn:resumed hook chooses the directory an agent's turn runs its tools in, for that turn only."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from structlog.testing import capture_logs

from mindroom.config.agent import AgentPrivateConfig
from mindroom.final_delivery import AwaitingApproval, Completed, FinalDeliveryOutcome
from mindroom.hooks import EVENT_TURN_ENDED, EVENT_TURN_PAUSED, EVENT_TURN_RESUMED, EVENT_TURN_STARTED, hook
from mindroom.response_turn import CompletedApprovalRun, ResponsePausedForApproval
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.output_files import current_tool_workdir
from tests.conftest import patch_response_runner_module, unwrap_extracted_collaborator
from tests.fakes.scripted_model import answer, calls, tool_call
from tests.response_runner_helpers import _noop_typing, _plain_request, _target
from tests.test_reply_endings_characterization import _turn
from tests.test_reply_records_approvals import _approval_bot, _paused
from tests.test_turn_lifecycle_events import _finalize, _lifecycle, _no_approval, _publish_hooks

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.hooks import TurnLifecycleContext
    from mindroom.response_lifecycle import ResponseLifecycle

pytestmark = pytest.mark.asyncio


def _asks_for(event_name: str, path: str) -> object:
    """Return a hook on ``event_name`` that asks for ``path`` as the turn's working directory."""

    @hook(event_name, name=f"work_in_{event_name.replace(':', '_')}")
    async def work_in(ctx: TurnLifecycleContext) -> None:
        ctx.set_workdir(path)

    return work_in


def _workspace(tmp_path: Path) -> Path:
    """Return a workspace holding the directory `wt`."""
    workspace = tmp_path / "workspace"
    (workspace / "wt").mkdir(parents=True)
    return workspace


async def _in_attempt() -> Path | None:
    """Return the working directory a task started now, as a turn's attempt is, sees."""
    return await asyncio.create_task(asyncio.to_thread(current_tool_workdir))


async def _run_turn(
    lifecycle: ResponseLifecycle,
    workspace: Path,
    *,
    resumed: bool = False,
    ending: Completed | AwaitingApproval | None = None,
) -> Path | None:
    """Run one settlement of ``lifecycle``'s turn in ``workspace``; return the working directory its attempt saw."""
    async with lifecycle.turn(approval_owns_end=_no_approval):
        await lifecycle.start_turn(resumed=resumed, agent_workspace_root=lambda: workspace)
        seen = await _in_attempt()
        await _finalize(lifecycle, FinalDeliveryOutcome(ending=ending or Completed()))
    return seen


@pytest.mark.parametrize("event_name", [EVENT_TURN_STARTED, EVENT_TURN_RESUMED], ids=["started", "resumed"])
async def test_a_hook_s_directory_holds_for_its_turn_and_not_for_a_later_turn_of_the_same_task(
    tmp_path: Path,
    event_name: str,
) -> None:
    """The turn's attempt works in the directory; once the turn's scope ends, and in the next turn, nothing is set."""
    workspace = _workspace(tmp_path)
    resumed = event_name == EVENT_TURN_RESUMED

    with capture_logs() as logs:
        seen = await _run_turn(_lifecycle(tmp_path, [_asks_for(event_name, "wt")]), workspace, resumed=resumed)
        after = current_tool_workdir()
        later = await _run_turn(_lifecycle(tmp_path, []), workspace, resumed=resumed)

    assert seen == workspace.resolve() / "wt"
    assert after is None
    assert later is None
    assert [log for log in logs if log["log_level"] == "warning"] == []


async def _raising_turn(
    lifecycle: ResponseLifecycle,
    workspace: Path,
    raised: BaseException,
    seen: list[Path | None],
) -> None:
    """Run one settlement of ``lifecycle``'s turn that raises ``raised`` after noting its working directory in ``seen``."""
    async with lifecycle.turn(approval_owns_end=_no_approval):
        await lifecycle.start_turn(resumed=False, agent_workspace_root=lambda: workspace)
        seen.append(current_tool_workdir())
        raise raised


async def test_a_turn_s_scope_unsets_the_directory_however_it_ends(tmp_path: Path) -> None:
    """A turn that fails, is cancelled, or pauses for an approval leaves nothing set in its task."""
    workspace = _workspace(tmp_path)
    seen: list[Path | None] = []

    for raised in (RuntimeError("boom"), asyncio.CancelledError()):
        lifecycle = _lifecycle(tmp_path, [_asks_for(EVENT_TURN_STARTED, "wt")])
        with pytest.raises(type(raised)):
            await _raising_turn(lifecycle, workspace, raised, seen)
        seen.append(current_tool_workdir())
    paused = _lifecycle(tmp_path, [_asks_for(EVENT_TURN_STARTED, "wt")])
    seen.extend((await _run_turn(paused, workspace, ending=AwaitingApproval()), current_tool_workdir()))

    workdir = workspace.resolve() / "wt"
    assert seen == [workdir, None, workdir, None, workdir, None]


def _no_workspace() -> Path:
    msg = "Private agent 'code' requires a requester identity to resolve requester-local state"
    raise ValueError(msg)


@pytest.mark.parametrize(
    ("requested", "workspace_root", "reason"),
    [
        ("../outside", None, "../outside leads outside the workspace"),
        ("{outside}", None, "{outside} leads outside the workspace"),
        ("out-link", None, "out-link leads outside the workspace"),
        ("missing", None, "missing is not an existing directory in the workspace"),
        ("notes.txt", None, "notes.txt is not an existing directory in the workspace"),
        ("wt", lambda tmp_path: lambda: tmp_path / "not-yet", "wt is not an existing directory in the workspace"),
        ("wt", lambda _tmp_path: _no_workspace, "Private agent 'code' requires a requester identity"),
    ],
    ids=["dot-dot", "absolute-outside", "link-out", "missing", "file", "no-workspace", "unresolved-workspace"],
)
async def test_a_refused_directory_leaves_the_turn_in_its_workspace_with_one_warning(
    tmp_path: Path,
    requested: str,
    workspace_root: Callable[[Path], Callable[[], Path]] | None,
    reason: str,
) -> None:
    """A path that is not an existing directory inside the workspace, reached without leaving it, is refused."""
    workspace = _workspace(tmp_path)
    (workspace / "notes.txt").write_text("notes", encoding="utf-8")
    (tmp_path / "outside").mkdir()
    (workspace / "out-link").symlink_to(tmp_path / "outside")
    requested = requested.format(outside=tmp_path / "outside")
    root = (lambda: workspace) if workspace_root is None else workspace_root(tmp_path)
    lifecycle = _lifecycle(tmp_path, [_asks_for(EVENT_TURN_STARTED, requested)])

    with capture_logs() as logs:
        async with lifecycle.turn(approval_owns_end=_no_approval):
            await lifecycle.start_turn(resumed=False, agent_workspace_root=root)
            seen = await _in_attempt()

    assert seen is None
    warnings = [log for log in logs if log["log_level"] == "warning"]
    assert [(log["event"], log["workdir"]) for log in warnings] == [("turn_workdir_refused", requested)]
    assert warnings[0]["reason"].startswith(reason.format(outside=tmp_path / "outside"))
    assert not (tmp_path / "not-yet").exists()


async def test_a_team_s_turn_ignores_the_directory_without_a_warning(tmp_path: Path) -> None:
    """A team's members work in their own workspaces, so its turn keeps no directory a hook asks for."""
    workspace = _workspace(tmp_path)
    lifecycle = _lifecycle(tmp_path, [_asks_for(EVENT_TURN_STARTED, "wt")])
    lifecycle.identity = replace(lifecycle.identity, response_kind="team")

    with capture_logs() as logs:
        seen = await _run_turn(lifecycle, workspace)

    assert seen is None
    assert [log for log in logs if log["log_level"] == "warning"] == []


async def test_a_pause_or_end_hook_that_asks_for_a_directory_changes_nothing(tmp_path: Path) -> None:
    """Only turn:started and turn:resumed requests count: the resumed turn after them runs in its workspace."""
    workspace = _workspace(tmp_path)
    hooks = [_asks_for(EVENT_TURN_PAUSED, "wt"), _asks_for(EVENT_TURN_ENDED, "wt")]

    with capture_logs() as logs:
        started = await _run_turn(_lifecycle(tmp_path, hooks), workspace, ending=AwaitingApproval())
        after_pause = current_tool_workdir()
        resumed = await _run_turn(_lifecycle(tmp_path, hooks), workspace, resumed=True)
        after_end = current_tool_workdir()

    assert (started, after_pause, resumed, after_end) == (None, None, None, None)
    assert [log for log in logs if log["log_level"] == "warning"] == []


def _agent_workspace(bot: AgentBot) -> Path:
    """Return the workspace of the bot's agent, holding the directories `started` and `resumed`."""
    workspace = resolve_agent_runtime(bot.agent_name, bot.config, bot.runtime_paths, execution_identity=None)
    for name in ("started", "resumed"):
        (workspace.workspace.root / name).mkdir(parents=True)
    return workspace.workspace.root


async def _paused_then_resumed(bot: AgentBot) -> list[Path | None]:
    """Answer one message whose run pauses for an approval that resumes at once; return where each run worked."""
    runner = unwrap_extracted_collaborator(bot._response_runner)
    request = _plain_request(_target())
    paused = replace(_paused(), session_id=request.response_envelope.target.session_id)
    seen: list[Path | None] = []

    def pausing_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        async def chunks() -> AsyncIterator[str]:
            seen.append(current_tool_workdir())
            yield paused.response_text
            raise ResponsePausedForApproval(paused)

        return chunks()

    async def resume(*_args: object, **_kwargs: object) -> CompletedApprovalRun:
        seen.append(current_tool_workdir())
        return CompletedApprovalRun("Approved.", {})

    with (
        patch_response_runner_module(
            stream_agent_response=pausing_stream,
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
        ),
        patch.object(type(runner.approvals), "_continue_entity_call", AsyncMock(side_effect=resume)),
    ):
        await runner.generate_response(request)
    return seen


@pytest.mark.parametrize("asks_on_resume", [True, False], ids=["resumed-hook", "started-hook-only"])
async def test_a_turn_resumed_in_the_same_task_works_where_its_turn_resumed_hooks_ask(
    tmp_path: Path,
    asks_on_resume: bool,
) -> None:
    """The response that pauses resumes its approval in the same task, which works only where turn:resumed asks."""
    async with _approval_bot(tmp_path, requires_human=False) as bot:
        workspace = _agent_workspace(bot)
        hooks = [_asks_for(EVENT_TURN_STARTED, "started")]
        if asks_on_resume:
            hooks.append(_asks_for(EVENT_TURN_RESUMED, "resumed"))
        _publish_hooks(bot, hooks)

        seen = await _paused_then_resumed(bot)

    resolved = workspace.resolve()
    assert seen == [resolved / "started", resolved / "resumed" if asks_on_resume else None]
    assert current_tool_workdir() is None


@pytest.mark.parametrize("private", [False, True], ids=["shared", "private"])
async def test_the_hook_s_directory_is_shell_s_cwd_and_file_s_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    private: bool,
) -> None:
    """In a Matrix turn, `shell` runs in the directory the hook asked for and `file` writes there.

    A private agent's directory is in its requester's workspace, where the turn's tools work.
    """
    turn = await _turn(
        tmp_path,
        monkeypatch,
        streaming=False,
        tools=["shell", "file"],
        scripts={
            "default": [
                calls(
                    tool_call("run_shell_command", "call-1", args="pwd"),
                    tool_call("save_file", "call-2", contents="a", file_name="a.txt"),
                ),
                answer("Done."),
            ],
        },
    )
    config = turn.bot.config
    request = _plain_request(_target())
    identity = turn.runner.deps.tool_runtime.build_execution_identity(target=_target(), user_id=request.user_id)
    shared = resolve_agent_runtime("general", config, turn.bot.runtime_paths, identity, create=True).workspace.root
    if private:
        config.agents["general"].private = AgentPrivateConfig(per="user")
    workspace = resolve_agent_runtime("general", config, turn.bot.runtime_paths, identity, create=True).workspace.root
    assert (workspace != shared) is private
    (workspace / "wt").mkdir()
    _publish_hooks(turn.bot, [_asks_for(EVENT_TURN_STARTED, "wt")])

    await turn.runner.generate_response(request)

    workdir = workspace.resolve() / "wt"
    results = [
        str(message.content) for message in turn.models["default"].requests[1].messages if message.role == "tool"
    ]
    assert results[0].splitlines()[-1] == str(workdir)
    assert (workdir / "a.txt").read_text(encoding="utf-8") == "a"
    assert not (workspace / "a.txt").exists()
    assert current_tool_workdir() is None
