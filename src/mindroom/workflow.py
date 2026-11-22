"""The workflow SDK: what a workflow script imports, running in the script's own process.

Standard library only, plus the launch contract.
MindRoom calls `main(args)`; every SDK call inside it is one saved node of the run.
"""

from __future__ import annotations

import difflib
import functools
import hashlib
import http.client
import json
import os
import sys
import threading
import time
import traceback
import types
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast

from mindroom.script_runs.launch_contract import GATEWAY_URL_ENV, RUN_ID_ENV, TOKEN_PATH_ENV

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

__all__ = [
    "AgentError",
    "PartialFailure",
    "ShellResult",
    "ToolError",
    "WorkflowError",
    "agent",
    "foreach",
    "log",
    "now",
    "parallel",
    "phase",
    "random",
    "run_workflow",
    "shell",
    "tool",
]

_NODE_WAIT_SECONDS = 25
_HTTP_TIMEOUT_SECONDS = 40.0
_RETRY_SECONDS = 1.0
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
_NO_SCHEMA = "schema is not available yet in this MindRoom version."
# How many lanes of one foreach() or parallel() run at once in the script; the run's own limit is the gateway's.
_MAX_ACTIVE_LANES = 256
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
    # The place the thread's lane holds among the active lanes of its fan-out, given up while its call waits.
    place: threading.Semaphore | None = None


_local = _Local()


def _current_lane() -> _Lane:
    lane = _local.lane
    if lane is None:
        on_main_thread = threading.current_thread() is threading.main_thread()
        raise WorkflowError(_MODULE_LEVEL_CALL if on_main_thread else _RAW_THREAD_CALL)
    return lane


def _next_key() -> str:
    lane = _current_lane()
    index, lane.next_index = lane.next_index, lane.next_index + 1
    return f"{lane.path}@{index}"


class _Gateway:
    """Blocking client of the attempt's workflow routes on the script gateway."""

    def __init__(self) -> None:
        run_id = urllib.parse.quote(os.environ[RUN_ID_ENV], safe="")
        self._base = f"{os.environ[GATEWAY_URL_ENV].rstrip('/')}/workflow/{run_id}"
        self._token = Path(os.environ[TOKEN_PATH_ENV]).read_text(encoding="utf-8").strip()

    def _request(self, method: str, route: str, body: dict[str, object] | None = None) -> object:
        data = None if body is None else json.dumps(body, allow_nan=False, separators=(",", ":")).encode()
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        while True:
            request = urllib.request.Request(  # noqa: S310 - the launcher sets the gateway URL.
                f"{self._base}/{route}",
                data=data,
                headers=headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                if exc.code < http.client.INTERNAL_SERVER_ERROR:
                    raise _refusal(exc, sent_bytes=len(data or b"")) from None
            except (urllib.error.URLError, OSError, http.client.HTTPException):
                pass
            # A restarting gateway looks like this; the node's outcome is saved there, so asking again is safe.
            time.sleep(_RETRY_SECONDS)

    def run_info(self) -> dict[str, object]:
        return cast("dict[str, object]", self._request("GET", "run"))

    def submit(self, key: str, kind: str, args: dict[str, object], meta: dict[str, object]) -> dict[str, object]:
        body: dict[str, object] = {"key": key, "kind": kind, "args": args, "meta": meta}
        return cast("dict[str, object]", self._request("POST", "nodes", body))

    def poll(self, node_id: str) -> dict[str, object]:
        """Return the node's receipt, held by the gateway until the node changes or the wait is over."""
        route = f"nodes/{urllib.parse.quote(node_id, safe='')}?wait={_NODE_WAIT_SECONDS}"
        return cast("dict[str, object]", self._request("GET", route))

    def result(self, node_id: str) -> object:
        return self._request("GET", f"results/{urllib.parse.quote(node_id, safe='')}")

    def log(self, text: str) -> None:
        self._request("POST", "log", {"text": text})

    def finish(self, *, result: object = None, error: str | None = None) -> None:
        self._request("POST", "finish", {"result": result} if error is None else {"error": error})


def _refusal(exc: urllib.error.HTTPError, *, sent_bytes: int) -> BaseException:
    """Turn a 4xx answer into what the script should see; a superseded attempt must stop acting."""
    if exc.code == http.client.CONFLICT:
        return SystemExit(_SUPERSEDED_EXIT_CODE)
    try:
        answer = json.loads(exc.read())
    except ValueError:
        answer = None
    details: dict[str, object] = answer if isinstance(answer, dict) else {}
    if exc.code == http.client.REQUEST_ENTITY_TOO_LARGE:
        size = details.get("bytes", sent_bytes)
        return WorkflowError(
            f"Prompt is {size:,} bytes, limit 1 MiB. Write the inputs to a workspace file and pass the path.",
        )
    return WorkflowError(f"MindRoom refused the request ({exc.code}): {details.get('detail', exc.reason)}")


@functools.cache
def _gateway() -> _Gateway:
    return _Gateway()


def _call(
    kind: str,
    args: dict[str, object],
    *,
    label: str | None = None,
    name: str | None = None,
    cached: bool = True,
    timeout: float | None = None,
) -> object:
    """Run one node: submit it under the lane's next key, wait until it settles, return its result or raise."""
    lane = _current_lane()
    meta = {"cached": cached, "phase": lane.phase, "label": label, "name": name, "timeout": timeout}
    gateway = _gateway()
    receipt = gateway.submit(_next_key(), kind, args, {key: value for key, value in meta.items() if value is not None})
    node_id = str(receipt["id"])
    # A call that waits for a person is no active lane of its fan-out: the next lane starts in its place, and the
    # call takes a place again before it goes on.
    place, waiting = _local.place, False
    while receipt["state"] not in _SETTLED_STATES or waiting:
        if place is not None and waiting != (receipt["state"] == "waiting"):
            waiting = not waiting
            (place.release if waiting else place.acquire)()
        if receipt["state"] not in _SETTLED_STATES:
            receipt = gateway.poll(node_id)
    if receipt["state"] == "completed":
        return gateway.result(node_id) if receipt.get("result_ref") else receipt.get("result")
    raise _failure(receipt, ToolError if kind == "tool" else AgentError)


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
    dedicated_worker: bool = False,
    resource_profile: str | None = None,
    **unknown: object,
) -> str:
    """Run one agent turn and return its answer.

    Returns text, or parsed JSON matching `schema` (a JSON Schema dict).
    The agent is a fresh copy of the workflow's owner with all its tools, unless `agent` names another configured agent.
    `system`, `tools`, `skills`, `knowledge` or `profile` (a `subagents/<profile>.md` file) shape a copy of the owner.
    `model` is any configured model name, such as "haiku" or "opus@high".
    `name` labels the node and is the address other agents message; it does not select an agent.
    `output_path` also writes the answer to that workspace file; `dedicated_worker` and `resource_profile` are those of run_subagent.
    Raises AgentError when the turn fails, is cancelled, is denied, or times out after `timeout` seconds.
    """
    _current_lane()
    options: dict[str, object] = {
        "name": name,
        "agent": agent,
        "model": model,
        "system": system,
        "tools": tools,
        "skills": skills,
        "knowledge": knowledge,
        "profile": profile,
        "minimal": minimal,
        "output_path": output_path,
        "dedicated_worker": dedicated_worker,
        "resource_profile": resource_profile,
    }
    _check_options("agent", unknown, frozenset(options) | {"schema", "timeout"})
    if schema is not None:
        raise WorkflowError(_NO_SCHEMA)
    # What the turn is, which a resume compares; how long this attempt waits for it is no part of that.
    # An empty list is a choice, of none: only an option that was not given is left out.
    given = {option: value for option, value in options.items() if value is not None and value is not False}
    call = {"prompt": str(prompt), **given}
    return str(_call("agent", call, name=name, timeout=timeout))


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
    meta = {"cached": True, "phase": lane.phase, "members": members}
    args: dict[str, object] = {"op": op, "positional": True} if positional else {"op": op}
    group = _gateway().submit(key, "group", args, {name: value for name, value in meta.items() if value is not None})
    if group["state"] != "completed":
        # The key holds a call that may have run, which no lane of a fan-out there may run again unasked.
        raise _failure(group, ToolError)
    lanes = _Lanes(threading.Semaphore(min(len(calls), max_concurrent or _MAX_ACTIVE_LANES) or 1), [None] * len(calls))
    threads: list[threading.Thread] = []
    for position, call in enumerate(calls):
        # Waits for a lane to end, or to wait for a person, so 1,000 items are never 1,000 threads at work.
        lanes.places.acquire()
        if lanes.stopped:
            break
        work = (position, call, _Lane(members[position], phase=lane.phase))
        threads.append(threading.Thread(target=lanes.run, args=work, daemon=True))
        threads[-1].start()
    for thread in threads:
        thread.join()
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
    errors: dict[int, BaseException] = field(default_factory=dict)
    stopped: list[BaseException] = field(default_factory=list)

    def run(self, position: int, call: Callable[[], object], lane: _Lane) -> None:
        """Run one call in *lane* on this thread, and keep its result or its failure under its position."""
        _local.lane, _local.place = lane, self.places
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
            self.places.release()


def shell(command: str, *, timeout: int = 3600, tail: int = 2000, cached: bool = True) -> ShellResult:
    """Run a command with the owner's shell tool; returns ShellResult(ok, exit_code, output) and never raises on a non-zero exit.

    `output` is the last `tail` lines. With cached=False the command runs again on every resume.
    shell() returns at most the last 50 KiB of output; redirect to a file and read it for more.
    """
    result = cast(
        "dict[str, object]",
        tool("shell", "run_shell_command", cached=cached, args=command, timeout=timeout, tail=tail),
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


def run_workflow(source_path: str) -> None:
    """Load the script as the module `__workflow__`, call its `main(args)` and report what it returned or raised.

    MindRoom's launcher calls this once per attempt; a script never does.
    """
    gateway = _gateway()
    try:
        module = types.ModuleType("__workflow__")
        module.__file__ = source_path
        sys.modules["__workflow__"] = module
        # No lane exists while the module body runs, so an SDK call there fails before it submits a node.
        exec(compile(Path(source_path).read_bytes(), source_path, "exec"), module.__dict__)  # noqa: S102
        main = module.__dict__.get("main")
        if not callable(main):
            raise WorkflowError(_NO_MAIN)  # noqa: TRY301 - reported like any failure of the script.
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
