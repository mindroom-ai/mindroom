"""The workflow SDK: what a workflow script imports, running in the script's own process.

Standard library only, plus the launch contract.
MindRoom calls `main(args)`; every SDK call inside it is one saved node of the run.
"""

from __future__ import annotations

import base64
import difflib
import functools
import hashlib
import http.client
import json
import math
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
_MAX_PEER_BYTES = 1024 * 1024
_TOO_LARGE = "Too large ({size:,} bytes, limit 1 MiB). Write it to a workspace file and send the path."


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
    loading: bool = False


class _Runnable:
    """Tracks lanes that can still run Python (unstarted or between calls) with a monotonic change counter."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._count = 0

    def reset(self, count: int) -> None:
        with self._lock:
            self._seq += 1
            self._count = max(0, count)

    def change(self, delta: int) -> None:
        with self._lock:
            self._seq += 1
            self._count = max(0, self._count + delta)

    def header(self) -> str:
        with self._lock:
            return f"{self._seq}:{self._count}"


_local = _Local()
_runnable = _Runnable()


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
        run_id = urllib.parse.quote(os.environ[RUN_ID_ENV], safe="")
        self._base = f"{os.environ[GATEWAY_URL_ENV].rstrip('/')}/workflow/{run_id}"
        self._token = Path(os.environ[TOKEN_PATH_ENV]).read_text(encoding="utf-8").strip()

    def _request(
        self,
        method: str,
        route: str,
        body: dict[str, object] | None = None,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> object:
        data = None if body is None else json.dumps(body, allow_nan=False, separators=(",", ":")).encode()
        faults = 0
        while True:
            headers = {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "X-Workflow-Runnable": _runnable.header(),
                **(extra_headers or {}),
            }
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
                # A plain 500 that repeats is a fault of MindRoom, not a restart: any other 5xx says to try again.
                faults = faults + 1 if exc.code == http.client.INTERNAL_SERVER_ERROR else 0
                if exc.code < http.client.INTERNAL_SERVER_ERROR or faults > _MAX_SERVER_FAULTS:
                    raise _refusal(exc, route=route, sent_bytes=len(data or b"")) from None
            except (urllib.error.URLError, OSError, http.client.HTTPException):
                pass
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
        extra = {"X-Workflow-State": state} if state is not None else None
        return cast("dict[str, object]", self._request("GET", route, extra_headers=extra))

    def result(self, node_id: str) -> object:
        return self._request("GET", f"results/{urllib.parse.quote(node_id, safe='')}")

    def log(self, text: str) -> None:
        self._request("POST", "log", {"text": text})

    def finish(self, *, result: object = None, error: str | None = None) -> None:
        self._request("POST", "finish", {"result": result} if error is None else {"error": error})


def _refusal(exc: urllib.error.HTTPError, *, route: str, sent_bytes: int) -> BaseException:
    """Turn a refusal into what the script should see; a superseded attempt must stop acting."""
    if exc.code == http.client.CONFLICT:
        return SystemExit(_SUPERSEDED_EXIT_CODE)
    try:
        answer = json.loads(exc.read())
    except ValueError:
        answer = None
    details: dict[str, object] = answer if isinstance(answer, dict) else {}
    if exc.code == http.client.REQUEST_ENTITY_TOO_LARGE:
        size = details.get("bytes", sent_bytes)
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
    return WorkflowError(f"MindRoom refused the request ({exc.code}): {details.get('detail', exc.reason)}")


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
    _runnable.change(-1)
    try:
        receipt = gateway.submit(node_key, kind, args, {k: value for k, value in meta.items() if value is not None})
        node_id = str(receipt["id"])
        # A call that waits for a person or peer is no active lane of its fan-out: the next lane starts in its place,
        # and the call takes a place again before it goes on.
        place, waiting = _local.place, False
        while receipt["state"] not in _SETTLED_STATES or waiting:
            if place is not None and waiting != (receipt["state"] == "waiting"):
                waiting = not waiting
                (place.release if waiting else place.acquire)()
            if receipt["state"] not in _SETTLED_STATES:
                receipt = gateway.poll(node_id, state=str(receipt["state"]))
        if receipt["state"] == "completed":
            return gateway.result(node_id) if receipt.get("result_ref") else receipt.get("result")
        error_type = {
            "tool": ToolError,
            "ask": WorkflowError,
            "send": WorkflowError,
            "inbox": WorkflowError,
            "state": WorkflowError,
        }.get(kind, AgentError)
        raise _failure(receipt, error_type)
    finally:
        _runnable.change(1)


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
) -> object:
    """Run one agent turn and return its answer.

    Returns text, or parsed JSON matching `schema` (a JSON Schema dict).
    With `schema` the agent is asked again, up to 5 turns, until its answer matches; then AgentError(kind="schema"),
    or kind "indeterminate" when one of its turns started a tool call. `schema` excludes `output_path`.
    The agent is a fresh copy of the workflow's owner with all its tools, unless `agent` names another configured agent.
    `system`, `tools`, `skills`, `knowledge` or `profile` (a `subagents/<profile>.md` file) shape a copy of the owner.
    `model` is any configured model name, such as "haiku" or "opus@high".
    `name` labels the node and is the address other agents message; it does not select an agent.
    `output_path` also writes the answer to that workspace file; `dedicated_worker` and `resource_profile` are those of run_subagent.
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
)


def _agent_options(
    function: str,
    values: dict[str, object],
    unknown: dict[str, object],
    *,
    extra: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Validate *unknown* and return the non-default agent options from *values*; empty lists are kept."""
    allowed = frozenset(_SHARED_OPTIONS) | extra
    _check_options(function, unknown, allowed)
    keys = (*_SHARED_OPTIONS, "name") if "name" in extra else _SHARED_OPTIONS
    return {k: values[k] for k in keys if values[k] is not None and values[k] is not False}


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
    dedicated_worker: bool = False,
    resource_profile: str | None = None,
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
    lanes = _Lanes(threading.Semaphore(active), [None] * len(calls))
    threads: list[threading.Thread] = []
    started = 0
    _runnable.change(len(calls) - 1)
    try:
        for position, call in enumerate(calls):
            # Waits for a lane to end, or to wait for a person/peer, so 1,000 items are never 1,000 threads at work.
            lanes.places.acquire()
            if lanes.stopped:
                lanes.places.release()
                break
            started += 1
            work = (position, call, _Lane(members[position], phase=lane.phase))
            threads.append(threading.Thread(target=lanes.run, args=work, daemon=True))
            threads[-1].start()
        for thread in threads:
            thread.join()
    finally:
        _runnable.change(1 - (len(calls) - started))
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
            _runnable.change(-1)
            self.places.release()


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


def send(to: str, text: str) -> dict[str, object]:
    """Message an agent of this run by name; "*" reaches every agent that has not finished."""
    _current_lane()
    body = str(text)
    raw = body.encode("utf-8")
    if len(raw) > _MAX_PEER_BYTES:
        raise WorkflowError(_TOO_LARGE.format(size=len(raw)))
    return cast("dict[str, object]", _call("send", {"to": str(to), "text": body}))


def inbox(wait_seconds: float = 0) -> list[dict[str, object]]:
    """Return messages agents sent to "script", waiting up to wait_seconds for the first one."""
    _current_lane()
    wait = float(wait_seconds)
    if not math.isfinite(wait) or wait < 0:
        msg = "wait_seconds must be a finite non-negative number."
        raise WorkflowError(msg)
    return cast("list[dict[str, object]]", _call("inbox", {"wait_seconds": wait}))


class state:  # noqa: N801 - public SDK namespace matching the spec (`state.get` / `state.set`).
    """The run's shared values, also readable and writable by its agents."""

    @staticmethod
    def get(key: str, default: object = None) -> object:
        """Return the shared value stored under *key*, or *default* when absent."""
        _current_lane()
        entry = cast("dict[str, object]", _call("state", {"op": "get", "key": str(key)}))
        return entry["value"] if entry.get("found") else default

    @staticmethod
    def set(key: str, value: object) -> None:
        """Store *value* under *key* in the run's shared state; last write wins."""
        _current_lane()
        raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > _MAX_PEER_BYTES:
            raise WorkflowError(_TOO_LARGE.format(size=len(raw)))
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
    module = types.ModuleType(module_name)
    module.__file__ = f"workflows/{name}.py"
    saved_lane = _local.lane
    sys.modules[module_name] = module
    try:
        _local.lane = None
        _local.loading = True
        try:
            exec(compile(raw, module.__file__, "exec"), module.__dict__)  # noqa: S102
        finally:
            _local.loading = False
        main = module.__dict__.get("main")
        if not callable(main):
            raise WorkflowError(_NO_MAIN)
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
        _runnable.reset(1)
        try:
            result = main(args)
        finally:
            _runnable.change(-1)
            _local.lane = None
        gateway.finish(result=result)
    except Exception:
        text = traceback.format_exc()
        sys.stderr.write(text)
        gateway.finish(error=text.encode()[-_ERROR_BYTES:].decode(errors="ignore"))
        raise SystemExit(1) from None
