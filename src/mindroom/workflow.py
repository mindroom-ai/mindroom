"""The workflow SDK: what a workflow script imports, running in the script's own process.

Standard library only, plus the shared script gateway transport.
MindRoom calls `main(args)`; every SDK call inside it is one saved node of the run.
"""

from __future__ import annotations

import base64
import difflib
import functools
import hashlib
import json
import math
import sys
import threading
import time
import traceback
import types
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast

from mindroom.script_runs.transport import (
    GatewayDecodeError,
    GatewayHTTPError,
    GatewayTransportError,
    ScriptGatewayTransport,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

__all__ = [
    "AgentError",
    "PartialFailure",
    "Session",
    "ShellResult",
    "ToolError",
    "WorkflowError",
    "agent",
    "ask_user",
    "foreach",
    "inbox",
    "log",
    "now",
    "parallel",
    "phase",
    "random",
    "run",
    "run_workflow",
    "send",
    "shell",
    "spawn",
    "state",
    "tool",
]

_NODE_WAIT_SECONDS = 25
_HTTP_TIMEOUT_SECONDS = 40.0
_RETRY_SECONDS = 1.0
_MAX_SERVER_FAULTS = 5
_SUPERSEDED_EXIT_CODE = 3
_ERROR_BYTES = 4096
_SETTLED_STATES = frozenset({"completed", "failed", "cancelled", "indeterminate"})
_OPTION_ALIASES = {"system_prompt": "system", "agent_name": "agent", "task": "prompt"}
_MODULE_LEVEL_CALL = "Call agent() inside main(args). MindRoom calls main; remove your own call."
_RAW_THREAD_CALL = (
    "Call agent() from main or inside foreach()/parallel(). "
    "Raw threads are not tracked, so resume could not match their calls."
)
_NO_MAIN = "Define def main(args): and return the report. MindRoom calls it."
_BAD_OPTIONS = "options must be a list of strings, such as ['Yes', 'No']."
# How many lanes of one foreach() or parallel() run at once in the script; the run's own limit is the gateway's.
_MAX_ACTIVE_LANES = 256
_MAX_RUN_DEPTH = 8
# How many failures a PartialFailure names in its message; `errors` holds them all.
_MAX_NAMED_FAILURES = 5
_NOT_FUNCTIONS = (
    "parallel() takes functions. Write parallel([lambda: agent(a), lambda: agent(b)]) or use foreach(items, fn)."
)
_SAME_LOOP_VARIABLE = (
    "These functions capture the same loop variable, so each would see its last value. "
    "Use foreach(prompts, lambda p: agent(p))."
)
_BAD_LIMIT = "max_concurrent must be a whole number of at least 1."


class WorkflowError(RuntimeError):
    """Base error; `kind` is a stable word and the message says what to do next."""

    def __init__(self, message: str, *, kind: str = "failed") -> None:
        super().__init__(message)
        self.kind = kind


class AgentError(WorkflowError):
    """kind: failed | cancelled | denied | timeout | schema | refused | interrupted | indeterminate."""


class ToolError(WorkflowError):
    """kind: failed | denied | indeterminate (the call may have run; check its effect, then resume with rerun=[...])."""


class PartialFailure(WorkflowError):  # noqa: N818 - the name the workflow's author catches.
    """Some calls of a foreach() or parallel() failed.

    ok: the results that succeeded, in order.
    results: one entry per position; a failed position holds None, so check `errors`, since None can also be a real result.
    errors: {position: exception}. items: the foreach items.
    """

    def __init__(self, results: list[object], errors: dict[int, BaseException], items: list[object] | None) -> None:
        named = [f"[{position}] {_describe(errors[position])}" for position in sorted(errors)[:_MAX_NAMED_FAILURES]]
        more = len(errors) - len(named)
        listed = "; ".join(named) + (f"; and {more} more" if more else "")
        super().__init__(f"{len(errors)} of {len(results)} calls failed: {listed}", kind="partial")
        self.results, self.errors, self.items = results, errors, items
        self.ok = [result for position, result in enumerate(results) if position not in errors]


def _describe(error: BaseException) -> str:
    kind = f"({error.kind})" if isinstance(error, WorkflowError) else ""
    return f"{type(error).__name__}{kind}: {error}"


@dataclass(frozen=True, slots=True)
class ShellResult:
    """How a `shell()` command ended: `ok` is `exit_code == 0`, `output` its last lines."""

    ok: bool
    exit_code: int
    output: str


@dataclass(slots=True)
class _Lane:
    """One sequence of calls whose keys count up, so a resumed run matches each call to its saved node."""

    path: str
    next_index: int = 0
    phase: str | None = None


class _Local(threading.local):
    lane: _Lane | None = None
    # The enclosing fan-out whose admission place this thread holds, given up while its call waits.
    lanes: _Lanes | None = None
    parked: bool = False
    loading: bool = False


_local = _Local()
_lanes_cond = threading.Condition()


def _current_lane() -> _Lane:
    lane = _local.lane
    if lane is None:
        on_main_thread = _local.loading or threading.current_thread() is threading.main_thread()
        raise WorkflowError(_MODULE_LEVEL_CALL if on_main_thread else _RAW_THREAD_CALL)
    return lane


def _next_key() -> str:
    lane = _current_lane()
    index, lane.next_index = lane.next_index, lane.next_index + 1
    return f"{lane.path}@{index}"


class _Gateway:
    """Blocking client of the attempt's workflow routes on the script gateway."""

    def __init__(self) -> None:
        self._transport = ScriptGatewayTransport(timeout_seconds=_HTTP_TIMEOUT_SECONDS)
        self._prefix = f"workflow/{urllib.parse.quote(self._transport.run_id, safe='')}"

    def _request(
        self,
        method: str,
        route: str,
        body: dict[str, object] | None = None,
        *,
        state: str | None = None,
    ) -> object:
        headers = None if state is None else {"X-Workflow-State": state}
        faults = 0
        while True:
            try:
                return self._transport.request_json(method, f"{self._prefix}/{route}", body, headers=headers)
            except GatewayHTTPError as exc:
                # A plain 500 that repeats is a fault of MindRoom, not a restart: any other 5xx says to try again.
                faults = faults + 1 if exc.status == 500 else 0
                if exc.status < 500 or faults > _MAX_SERVER_FAULTS:
                    raise _refusal(exc, route=route) from None
            except GatewayTransportError:
                pass
            except GatewayDecodeError:
                msg = "MindRoom returned an invalid JSON response."
                raise WorkflowError(msg) from None
            # A restarting gateway looks like this; the node's outcome is saved there, so asking again is safe.
            time.sleep(_RETRY_SECONDS)

    def run_info(self) -> dict[str, object]:
        return cast("dict[str, object]", self._request("GET", "run"))

    def source(self, name: str) -> tuple[bytes, str]:
        payload = cast("dict[str, object]", self._request("GET", f"sources/{urllib.parse.quote(name, safe='')}"))
        raw = base64.b64decode(str(payload["source_b64"]))
        sha256 = str(payload["sha256"])
        if hashlib.sha256(raw).hexdigest() != sha256:
            msg = f"Saved workflow '{name}' failed digest check."
            raise WorkflowError(msg)
        return raw, sha256

    def submit(self, key: str, kind: str, args: dict[str, object], meta: dict[str, object]) -> dict[str, object]:
        body: dict[str, object] = {"key": key, "kind": kind, "args": args, "meta": meta}
        return cast("dict[str, object]", self._request("POST", "nodes", body))

    def poll(self, node_id: str, *, state: str | None = None) -> dict[str, object]:
        """Return the node's receipt, held by the gateway until the node changes or the wait is over."""
        route = f"nodes/{urllib.parse.quote(node_id, safe='')}?wait={_NODE_WAIT_SECONDS}"
        return cast("dict[str, object]", self._request("GET", route, state=state))

    def result(self, node_id: str) -> object:
        return self._request("GET", f"results/{urllib.parse.quote(node_id, safe='')}")

    def log(self, text: str) -> None:
        self._request("POST", "log", {"text": text})

    def finish(self, *, result: object = None, error: str | None = None) -> None:
        self._request("POST", "finish", {"result": result} if error is None else {"error": error})


def _refusal(exc: GatewayHTTPError, *, route: str) -> BaseException:
    """Turn a refusal into what the script should see; a superseded attempt must stop acting."""
    if exc.status == 409:
        return SystemExit(_SUPERSEDED_EXIT_CODE)
    details = exc.payload or {}
    if exc.status == 413:
        raw_size = details.get("bytes")
        size = raw_size if isinstance(raw_size, int) and not isinstance(raw_size, bool) else exc.sent_bytes
        raw_limit = details.get("limit")
        if isinstance(raw_limit, int) and raw_limit > 0:
            limit = f"{raw_limit // (1024 * 1024)} MiB" if raw_limit % (1024 * 1024) == 0 else f"{raw_limit:,} bytes"
        else:
            limit = "1 MiB"
        if route == "finish":
            return WorkflowError(
                f"Report is {size:,} bytes, limit {limit}. Write the full output to a workspace file and return a summary or the path.",
            )
        return WorkflowError(
            f"Request is {size:,} bytes, limit {limit}. Write the inputs to a workspace file and pass the path.",
        )
    return WorkflowError(f"MindRoom refused the request ({exc.status}): {exc.detail or exc.reason}")


@functools.cache
def _gateway() -> _Gateway:
    return _Gateway()


def _call(
    kind: str,
    args: dict[str, object],
    *,
    key: str | None = None,
    label: str | None = None,
    name: str | None = None,
    cached: bool = True,
    timeout: float | None = None,
) -> object:
    """Run one node: submit it under *key* or the lane's next key, wait until it settles, return its result or raise."""
    lane = _current_lane()
    meta = {"cached": cached, "phase": lane.phase, "label": label, "name": name, "timeout": timeout}
    gateway = _gateway()
    node_key = _next_key() if key is None else key
    receipt = gateway.submit(node_key, kind, args, {k: value for k, value in meta.items() if value is not None})
    node_id = str(receipt["id"])
    # A call that waits for a person or peer is no active lane of its fan-out: the next lane starts in its place,
    # and the call takes a place again before it goes on.
    owner, waiting = _local.lanes, False
    while receipt["state"] not in _SETTLED_STATES or waiting:
        if owner is not None and waiting != (receipt["state"] == "waiting"):
            waiting = not waiting
            _local.parked = waiting
            (owner.park if waiting else owner.unpark)()
        if receipt["state"] not in _SETTLED_STATES:
            receipt = gateway.poll(node_id, state=str(receipt["state"]))
    if receipt["state"] == "completed":
        return gateway.result(node_id) if receipt.get("result_ref") else receipt.get("result")
    errs = {"agent": AgentError, "turn": AgentError, "session": AgentError, "tool": ToolError}
    raise _failure(receipt, errs.get(kind, WorkflowError))


def _failure(receipt: dict[str, object], error_type: type[WorkflowError]) -> WorkflowError:
    """Return what a call whose node ended without a result raises: its message, and its kind to branch on."""
    details = cast("dict[str, object]", receipt.get("error") or {})
    message = str(details.get("message") or f"The call ended {receipt['state']}.")
    return error_type(message, kind=str(details.get("kind") or receipt["state"]))


def _check_options(function: str, given: Iterable[str], allowed: frozenset[str]) -> None:
    """Refuse an option *function* does not take, naming the one the caller most likely meant."""
    for option in given:
        if option in allowed:
            continue
        alias = _OPTION_ALIASES.get(option)
        guesses = [alias] if alias in allowed else difflib.get_close_matches(option, sorted(allowed), n=1)
        hint = f" Did you mean {guesses[0]}?" if guesses else ""
        msg = f"{function}() has no option '{option}'.{hint}"
        raise WorkflowError(msg)


def agent(
    prompt: str,
    *,
    schema: object = None,
    name: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    system: str | None = None,
    tools: list[str] | None = None,
    skills: list[str] | None = None,
    knowledge: list[str] | None = None,
    profile: str | None = None,
    minimal: bool = False,
    timeout: float | None = None,
    output_path: str | None = None,
    dedicated_worker: bool | None = None,
    resource_profile: str | None = None,
    sandbox: bool | None = None,
    mount_dir: str | None = None,
    mount_read_only: bool | None = None,
    **unknown: object,
) -> object:
    """Run one agent turn and return its answer.

    Returns text, or parsed JSON matching `schema` (a JSON Schema dict).
    With `schema` the agent is asked again, up to 5 turns, until its answer matches; then AgentError(kind="schema"),
    or kind "indeterminate" when one of its turns started a tool call. `schema` excludes `output_path`.
    The agent is a fresh copy of the workflow's owner with all its tools, unless `agent` names another configured agent.
    `system`, `tools`, `skills`, `knowledge` or `profile` (a `subagents/<profile>.md` file) shape a copy of the owner.
    `model` is any configured model name, such as "haiku" or "opus@high".
    `name` labels the node and is the address other agents message; it does not select an agent.
    `output_path` also writes the returned answer to that caller-workspace file on the primary;
    `dedicated_worker`, `resource_profile`, `sandbox`, `mount_dir`, and `mount_read_only` match run_subagent.
    Raises AgentError when the turn fails, is cancelled, is denied, or times out after `timeout` seconds.
    """
    _current_lane()
    given = _agent_options("agent", locals(), unknown, extra=frozenset({"name", "schema", "timeout"}))
    call = {"prompt": str(prompt), **given}
    if schema is None:
        return str(_call("agent", call, name=name, timeout=timeout))
    return _call("agent", {**call, "schema": schema}, name=name, timeout=timeout)


_SHARED_OPTIONS = (
    "agent",
    "model",
    "system",
    "tools",
    "skills",
    "knowledge",
    "profile",
    "minimal",
    "output_path",
    "dedicated_worker",
    "resource_profile",
    "sandbox",
    "mount_dir",
    "mount_read_only",
)


def _agent_options(
    function: str,
    values: dict[str, object],
    unknown: dict[str, object],
    *,
    extra: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Validate *unknown* and return the non-default agent options from *values*; empty lists are kept.

    Existing defaults (`minimal=False`, lone `dedicated_worker=False`, lone `sandbox=False`/`mount_read_only=False`)
    are omitted so replay digests stay unchanged, while contradictory combinations (such as
    `dedicated_worker=False, sandbox=True` or `sandbox=False, mount_dir="repo"`) travel to the engine and are refused.
    """
    allowed = frozenset(_SHARED_OPTIONS) | extra
    _check_options(function, unknown, allowed)
    keys = (*_SHARED_OPTIONS, "name") if "name" in extra else _SHARED_OPTIONS
    has_worker_option = any(
        values.get(opt) is not None for opt in ("resource_profile", "sandbox", "mount_dir", "mount_read_only")
    )
    has_sandbox_target = values.get("mount_dir") is not None or values.get("mount_read_only") is True
    out: dict[str, object] = {}
    for k in keys:
        value = values[k]
        if value is None:
            continue
        if k == "minimal" and value is False:
            continue
        if k == "dedicated_worker" and value is False and not has_worker_option:
            continue
        if k == "sandbox" and value is False and not has_sandbox_target:
            continue
        if (
            k == "mount_read_only"
            and value is False
            and values.get("mount_dir") is None
            and values.get("sandbox") is not True
        ):
            continue
        out[k] = value
    return out


class Session:
    """An agent that keeps its conversation across `ask()` turns."""

    def __init__(self, key: str, name: str) -> None:
        self._key, self._name, self._turn, self._lock = key, name, 0, threading.Lock()

    def ask(self, message: str, *, schema: object = None, timeout: float | None = None) -> object:
        """Send the next message to this agent and return its answer; it remembers the earlier turns."""
        _current_lane()
        with self._lock:
            self._turn += 1
            turn = self._turn
        call: dict[str, object] = {"session": self._key, "turn": turn, "message": str(message)}
        if schema is None:
            return str(_call("turn", call, name=self._name, timeout=timeout))
        return _call("turn", {**call, "schema": schema}, name=self._name, timeout=timeout)


def spawn(
    name: str,
    *,
    agent: str | None = None,
    model: str | None = None,
    system: str | None = None,
    tools: list[str] | None = None,
    skills: list[str] | None = None,
    knowledge: list[str] | None = None,
    profile: str | None = None,
    minimal: bool = False,
    output_path: str | None = None,
    dedicated_worker: bool | None = None,
    resource_profile: str | None = None,
    sandbox: bool | None = None,
    mount_dir: str | None = None,
    mount_read_only: bool | None = None,
    **unknown: object,
) -> Session:
    """Create an agent that keeps its conversation; takes the options of agent() except prompt, schema and timeout."""
    _current_lane()
    given = _agent_options("spawn", locals(), unknown)
    label, key = str(name), _next_key()
    _call("session", {"name": label, **given}, key=key, name=label)
    return Session(key, label)


def foreach(items: Iterable[object], fn: Callable[..., object], *, max_concurrent: int | None = None) -> list[object]:
    """Call fn(item) for every item in parallel and return the results in item order.

    When calls fail, the others still finish, then PartialFailure is raised.
    Finished items are remembered by value, so adding or reordering items on resume reuses them.
    Items that are not JSON values are remembered by position.
    """
    _current_lane()
    values = list(items)
    seen: dict[str, int] = {}
    names = [_item_key(item, position, seen) for position, item in enumerate(values)]
    calls = [functools.partial(fn, item) for item in values]
    return _fan_out("foreach", calls, names, values, max_concurrent)


def parallel(fns: Iterable[Callable[[], object]], *, max_concurrent: int | None = None) -> list[object]:
    """Call every zero-argument function in parallel and return the results in order; raises PartialFailure like foreach."""
    _current_lane()
    calls = list(fns)
    if not all(callable(call) for call in calls):
        raise WorkflowError(_NOT_FUNCTIONS)
    captured: dict[tuple[object, ...], object] = {}
    for call in calls:
        # Two functions with one body, the very same variables and no value bound to either differ in nothing.
        cells, bound = getattr(call, "__closure__", None) or (), getattr(call, "__defaults__", None) or ()
        same = captured.setdefault((getattr(call, "__code__", None), *map(id, cells), None, *map(id, bound)), call)
        if cells and same is not call:
            raise WorkflowError(_SAME_LOOP_VARIABLE)
    return _fan_out("parallel", calls, [f"p{position}" for position in range(len(calls))], None, max_concurrent)


def _item_key(item: object, position: int, seen: dict[str, int]) -> str:
    """Return the name of a foreach item's lane: a hash of its JSON value, numbered from its second occurrence on.

    An item that is no JSON value is named by its *position*, so a resume matches it only while it stays there.
    """
    try:
        text = json.dumps(item, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return f"p{position}"
    name = hashlib.sha256(text.encode()).hexdigest()[:12]
    seen[name] = count = seen.get(name, 0) + 1
    return name if count == 1 else f"{name}#{count}"


def _fan_out(
    op: str,
    calls: Sequence[Callable[[], object]],
    names: Sequence[str],
    items: list[object] | None,
    max_concurrent: int | None,
) -> list[object]:
    """Run *calls* each in a lane of its own, at most `max_concurrent` at a time, and return their results in order.

    The group node comes first and names every lane, so a resume sees which items came, went or moved. Every call
    ends before a failure is raised, as one PartialFailure. An attempt that MindRoom told to exit, as it does when
    the run was cancelled or moved on, is no failure of an item: it stops the fan-out and leaves it as it is.
    """
    if max_concurrent is not None and (type(max_concurrent) is not int or max_concurrent < 1):
        raise WorkflowError(_BAD_LIMIT)
    lane, key = _current_lane(), _next_key()
    members = [f"{lane.path}/{key.rpartition('@')[2]}:{name}" for name in names]
    positional = items is not None and any(name.startswith("p") for name in names)
    args: dict[str, object] = {"op": op, "positional": True} if positional else {"op": op}
    _submit_group(key, args, lane, members)
    if not calls:
        return []
    active = min(len(calls), max_concurrent or _MAX_ACTIVE_LANES, _MAX_ACTIVE_LANES)
    lanes = _Lanes(threading.Semaphore(active), [None] * len(calls), _local.lanes, len(calls), len(calls))
    threads: list[threading.Thread] = []
    try:
        for position, call in enumerate(calls):
            # Waits for a lane to end, or to wait for a person/peer, so 1,000 items are never 1,000 threads at work.
            lanes.places.acquire()
            if lanes.stopped:
                lanes.places.release()
                break
            with _lanes_cond:
                lanes.unstarted -= 1
                lanes.working += 1
            work = (position, call, _Lane(members[position], phase=lane.phase))
            threads.append(threading.Thread(target=lanes.run, args=work, daemon=True))
            threads[-1].start()
        for thread in threads:
            thread.join()
    finally:
        lanes.restore_parent()
    if lanes.stopped:
        raise lanes.stopped[0]
    if lanes.errors:
        raise PartialFailure(lanes.results, lanes.errors, items)
    return lanes.results


@dataclass(slots=True)
class _Lanes:
    """The lanes of one fan-out while they run: the places they share, their results, and what failed or stopped it."""

    places: threading.Semaphore
    results: list[object]
    parent: _Lanes | None = None
    live: int = 0
    unstarted: int = 0
    working: int = 0
    parent_state: str = "held"
    errors: dict[int, BaseException] = field(default_factory=dict)
    stopped: list[BaseException] = field(default_factory=list)

    def park(self, *, finished: bool = False, was_parked: bool = False) -> None:
        """Release this lane's place while waiting or ending; cascade to parent when all child lanes are parked."""
        if not was_parked:
            self.places.release()
        with _lanes_cond:
            self.working -= 0 if was_parked else 1
            self.live -= 1 if finished else 0
            idle = self.working == 0 and self.unstarted == 0 and self.live > 0
            cascade = bool(self.parent and self.parent_state == "held" and idle)
            if cascade:
                self.parent_state = "parked"
        if cascade and self.parent is not None:
            self.parent.park()

    def unpark(self) -> None:
        """Reacquire enclosing fan-out place (if cascaded) and then this fan-out's place before resuming."""
        self.restore_parent()
        self.places.acquire()
        with _lanes_cond:
            self.working += 1

    def restore_parent(self) -> None:
        """Reacquire the enclosing fan-out place if this fan-out parked it."""
        with _lanes_cond:
            while self.parent_state == "acquiring":
                _lanes_cond.wait()
            reacquire = self.parent is not None and self.parent_state == "parked"
            if reacquire:
                self.parent_state = "acquiring"
        if reacquire and self.parent is not None:
            self.parent.unpark()
            with _lanes_cond:
                self.parent_state = "held"
                _lanes_cond.notify_all()

    def run(self, position: int, call: Callable[[], object], lane: _Lane) -> None:
        """Run one call in *lane* on this thread, and keep its result or its failure under its position."""
        _local.lane, _local.lanes, _local.parked = lane, self, False
        try:
            self.results[position] = call()
        except SystemExit as error:
            self.stopped.append(error)
        except BaseException as error:
            # Every other failure is the item's, a bug of the script included.
            self.errors[position] = error
            if not isinstance(error, (AgentError, ToolError, PartialFailure)):
                # No call of the lane shows this failure, so a node of its own does.
                report: dict[str, object] = {"type": type(error).__name__, "message": str(error)[:_ERROR_BYTES]}
                _gateway().submit(_next_key(), "error", report, {"phase": lane.phase} if lane.phase else {})
        finally:
            was_parked, _local.parked = _local.parked, False
            self.park(finished=True, was_parked=was_parked)


def ask_user(question: str, options: list[str] | None = None) -> str:
    """Show the question in the thread and return the answer; only the calling branch waits.

    `options` is a list of strings; the answer is one of them. Without options the answer is free text.
    The question waits as long as it takes, across restarts.
    """
    if options is not None and not (
        isinstance(options, list) and options and all(isinstance(option, str) and option.strip() for option in options)
    ):
        raise WorkflowError(_BAD_OPTIONS)
    return str(_call("ask", {"question": str(question), "options": options}))


def shell(
    command: str,
    *,
    timeout: int = 3600,
    tail: int = 2000,
    cached: bool = True,
    worker: str | None = None,
    resource_profile: str | None = None,
    sandbox: bool | None = None,
    mount_dir: str | None = None,
    mount_read_only: bool | None = None,
) -> ShellResult:
    """Run a command with the owner's shell tool; returns ShellResult(ok, exit_code, output) and never raises on a non-zero exit.

    `output` is the last `tail` lines. With cached=False the command runs again on every resume.
    shell() returns at most the last 50 KiB of output; redirect to a file and read it for more.
    """
    extra = {
        key: value
        for key, value in (
            ("worker", worker),
            ("resource_profile", resource_profile),
            ("sandbox", sandbox),
            ("mount_dir", mount_dir),
            ("mount_read_only", mount_read_only),
        )
        if value is not None
    }
    result = cast(
        "dict[str, object]",
        tool("shell", "run_shell_command", cached=cached, args=command, timeout=timeout, tail=tail, **extra),
    )
    exit_code = cast("int", result["exit_code"])
    return ShellResult(ok=exit_code == 0, exit_code=exit_code, output=str(result["output"]))


def tool(toolkit: str, function: str, /, *, cached: bool = True, **arguments: object) -> object:
    """Call one of the owner's tools and return its JSON result; raises ToolError when the call fails."""
    return _call("tool", {"toolkit": toolkit, "function": function, "arguments": arguments}, cached=cached)


class _Phase:
    """What `phase()` returns, so `with phase(title):` ends the phase with its block."""

    def __init__(self, lane: _Lane, previous: str | None) -> None:
        self._lane = lane
        self._previous = previous

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._lane.phase = self._previous


def phase(title: str) -> _Phase:
    """Group the calls that follow under `title` in the graph; also works as `with phase(title):`."""
    lane = _current_lane()
    previous, lane.phase = lane.phase, str(title)
    return _Phase(lane, previous)


def log(text: str) -> None:
    """Show a progress line on the run."""
    _current_lane()
    _gateway().log(str(text))


def now() -> datetime:
    """Return the current UTC time, remembered across resume."""
    return datetime.fromisoformat(str(_call("value", {"op": "now"})))


def random() -> float:
    """Return a random float in [0, 1), remembered across resume."""
    return float(cast("float", _call("value", {"op": "random"})))


def send(to: str, text: str) -> dict[str, object]:
    """Message an agent of this run by name; "*" reaches every agent that has not finished."""
    _current_lane()
    return cast("dict[str, object]", _call("send", {"to": str(to), "text": str(text)}))


def inbox(wait_seconds: float = 0) -> dict[str, object]:
    """Return only unread mail sent to "script" (advancing the cursor) as {messages, note?}; waits end on mail or after wait_seconds with note. Prefer short waits (10 to 30 s) in a bounded loop."""
    _current_lane()
    if isinstance(wait_seconds, bool) or not math.isfinite(wait := float(wait_seconds)) or wait < 0:
        msg = "wait_seconds must be a finite non-negative number."
        raise WorkflowError(msg)
    return cast("dict[str, object]", _call("inbox", {"wait_seconds": wait}))


class state:  # noqa: N801 - public SDK namespace matching the spec (`state.get` / `state.set`).
    """The run's shared values, also readable and writable by its agents."""

    @staticmethod
    def get(key: str, default: object = None) -> object:
        """Return the shared JSON value stored under *key* directly, or *default* when absent (unlike agent get_shared_state, which returns {key, found, value})."""
        _current_lane()
        entry = cast("dict[str, object]", _call("state", {"op": "get", "key": str(key)}))
        return entry["value"] if entry.get("found") else default

    @staticmethod
    def set(key: str, value: object) -> None:
        """Store any JSON value (pass the value directly; don't json.dumps it) under *key* in shared state; last write wins, so pick one writer per key."""
        _current_lane()
        json.dumps(value, allow_nan=False)
        _call("state", {"op": "set", "key": str(key), "value": value})


def _submit_group(key: str, args: dict[str, object], lane: _Lane, members: list[str]) -> None:
    meta: dict[str, object] = {"cached": True, "phase": lane.phase, "members": members}
    group = _gateway().submit(
        key,
        "group",
        args,
        {field_name: value for field_name, value in meta.items() if value is not None},
    )
    if group["state"] != "completed":
        # The key holds a call that may have run, which no lane of a group there may run again unasked.
        raise _failure(group, ToolError)


def _load_main(raw: bytes, filename: str, module_name: str) -> Callable[[object], object]:
    """Execute *raw* as *module_name* with `__file__ = filename`, no active lane, and return its `main`."""
    module = types.ModuleType(module_name)
    module.__file__ = filename
    sys.modules[module_name] = module
    _local.lane = None
    _local.loading = True
    try:
        # No lane exists while the module body runs, so an SDK call there fails before it submits a node.
        exec(compile(raw, filename, "exec"), module.__dict__)  # noqa: S102
    finally:
        _local.loading = False
    main = module.__dict__.get("main")
    if not callable(main):
        raise WorkflowError(_NO_MAIN)
    return main


def run(name: str, args: object = None) -> object:
    """Run the saved workflow `workflows/<name>.py` inside this run and return what its main(args) returns."""
    lane = _current_lane()
    if lane.path.count(":run") >= _MAX_RUN_DEPTH:
        msg = f"Nested run() depth limit ({_MAX_RUN_DEPTH}) exceeded."
        raise WorkflowError(msg)
    raw, sha256 = _gateway().source(name)
    key = _next_key()
    child_path = f"{lane.path}/{key.rpartition('@')[2]}:run"
    _submit_group(key, {"op": "run", "name": name, "source_sha256": sha256}, lane, [child_path])
    module_name = f"__workflow_run_{name}_{child_path}__"
    saved_lane = _local.lane
    try:
        main = _load_main(raw, f"workflows/{name}.py", module_name)
        _local.lane = _Lane(child_path, phase=lane.phase)
        return main(args)
    finally:
        _local.lane = saved_lane
        sys.modules.pop(module_name, None)


def run_workflow(source_path: str) -> None:
    """Load the script as the module `__workflow__`, call its `main(args)` and report what it returned or raised.

    MindRoom's launcher calls this once per attempt; a script never does.
    """
    gateway = _gateway()
    try:
        main = _load_main(Path(source_path).read_bytes(), source_path, "__workflow__")
        args = gateway.run_info()["args"]
        _local.lane = _Lane("")
        try:
            result = main(args)
        finally:
            _local.lane = None
        gateway.finish(result=result)
    except Exception:
        text = traceback.format_exc()
        sys.stderr.write(text)
        gateway.finish(error=text.encode()[-_ERROR_BYTES:].decode(errors="ignore"))
        raise SystemExit(1) from None
