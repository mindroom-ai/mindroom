"""The workflow SDK: what a workflow script imports, running in the script's own process.

Standard library only, plus the launch contract.
MindRoom calls `main(args)`; every SDK call inside it is one saved node of the run.
"""

from __future__ import annotations

import difflib
import functools
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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast

from mindroom.script_runs.launch_contract import GATEWAY_URL_ENV, RUN_ID_ENV, TOKEN_PATH_ENV

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "AgentError",
    "ShellResult",
    "ToolError",
    "WorkflowError",
    "log",
    "now",
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


class WorkflowError(RuntimeError):
    """Base error; `kind` is a stable word and the message says what to do next."""

    def __init__(self, message: str, *, kind: str = "failed") -> None:
        super().__init__(message)
        self.kind = kind


class AgentError(WorkflowError):
    """kind: failed | cancelled | denied | timeout | schema | refused | indeterminate."""


class ToolError(WorkflowError):
    """kind: failed | denied | indeterminate (the call may have run; check its effect, then resume with rerun=[...])."""


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

    def wait(self, node_id: str) -> dict[str, object]:
        route = f"nodes/{urllib.parse.quote(node_id, safe='')}?wait={_NODE_WAIT_SECONDS}"
        while True:
            receipt = cast("dict[str, object]", self._request("GET", route))
            if receipt["state"] in _SETTLED_STATES:
                return receipt

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
) -> object:
    """Run one node: submit it under the lane's next key, wait until it settles, return its result or raise."""
    lane = _current_lane()
    meta = {"cached": cached, "phase": lane.phase, "label": label, "name": name}
    gateway = _gateway()
    receipt = gateway.submit(_next_key(), kind, args, {key: value for key, value in meta.items() if value is not None})
    node_id = str(receipt["id"])
    if receipt["state"] not in _SETTLED_STATES:
        receipt = gateway.wait(node_id)
    if receipt["state"] == "completed":
        return gateway.result(node_id) if receipt.get("result_ref") else receipt.get("result")
    details = cast("dict[str, object]", receipt.get("error") or {})
    error_type = ToolError if kind == "tool" else AgentError
    raise error_type(
        str(details.get("message") or f"The call ended {receipt['state']}."),
        kind=str(details.get("kind") or receipt["state"]),
    )


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
