"""The stdlib-only mindroom-agent console entry point."""

from __future__ import annotations

__all__ = ["main", "parse_arguments", "read_call_arguments"]

import argparse
import json
import os
import re
import signal
import sys
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from collections.abc import Iterator

from mindroom.agent_cli.client import AgentCliClient, AgentCliRefusedError, AgentCliUnavailableError
from mindroom.agent_cli.json_io import MAX_ENVELOPE_BYTES, canonical_json, read_json
from mindroom.script_runs.launch_contract import RUN_ID_ENV
from mindroom.script_sdk import MindRoomToolCallError, MindRoomTools

_CALL_TIMEOUT_SECONDS = 30
# How long a command in a named worker may run when `workers run` names no limit.
_WORKER_COMMAND_TIMEOUT_SECONDS = 3600
# `workers run` exits with its command's exit code, so its own failures use codes commands rarely do, as `timeout` and
# `docker run` do: 124 for timeout, 125 when the command never ran, and 255 when its outcome is unknown.
_EXIT_TIMED_OUT = 124
_EXIT_NOT_RUN = 125
_EXIT_UNKNOWN = 255
# How long `workers run` keeps reading a result it cannot reach before it gives up on a command that may still run.
_RESULT_UNREACHABLE_SECONDS = 60
_HTTP_TIMEOUT_SECONDS = 30.0
_LONG_POLL_SECONDS = 25.0
_CANCEL_TIMEOUT_SECONDS = 8.0
# The least time between two reads of a receipt, for a server that answers a long-poll at once.
_POLL_FLOOR_SECONDS = 0.2
_WORKER_SIGNALS = tuple(
    sig for name in ("SIGINT", "SIGTERM", "SIGHUP") if (sig := getattr(signal, name, None)) is not None
)
# How a shell result leads when its command failed (`format_shell_completion`): its exit code, negative for the
# signal that killed it, then the streams it kept under their labels. A command that succeeds while printing exactly
# these lines first reads as failed, which the shell result cannot tell apart.
_FAILED_COMMAND = re.compile(r"Exit code (-?[1-9][0-9]*)(?:\n(?=(?:Output|Stderr):\n)|\Z)")
# How a shell result reports a missing or non-executable binary (`format_shell_launch_failure`).
_LAUNCH_FAILED = re.compile(r"Could not start command \(exit code (126|127)\): (.+)")
# Parsed options the CLI handles itself instead of sending in the operation payload.
_LOCAL_ARGUMENTS = frozenset(
    {"group", "action", "json", "json_argument", "json_file", "json_stdin", "call_id", "timeout"},
)


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse only fixed operations; runtime configuration is unnecessary for help."""
    argv, worker_command = _split_worker_command(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="mindroom-agent")
    groups = parser.add_subparsers(dest="group", required=True)
    tools = groups.add_parser(
        "tools",
        help="list, search QUERY, describe TOOLKIT FUNCTION, call TOOLKIT FUNCTION (or TOOLKIT.FUNCTION)",
    ).add_subparsers(dest="action", required=True)
    listing = tools.add_parser("list")
    listing.add_argument("--cursor")
    listing.add_argument("--limit", type=int, default=100)
    search = tools.add_parser("search")
    search.add_argument("query")
    search.add_argument("--toolkit")
    search.add_argument("--limit", type=int, default=20)
    for action in ("describe", "call"):
        command = tools.add_parser(action)
        command.add_argument("toolkit", help="toolkit name, or TOOLKIT.FUNCTION")
        command.add_argument("function", nargs="?")
        if action == "call":
            command.add_argument("json_argument", nargs="?", metavar="JSON", help="tool arguments, same as --json")
            command.add_argument("--call-id", type=UUID)
            command.add_argument(
                "--timeout",
                type=int,
                # Unset rather than 30, so a background script, whose calls always wait, can refuse an explicit one.
                default=None,
                help="Seconds to wait for the result before returning the pending receipt (default: 30; 0 returns at once)",
            )
            inputs = command.add_mutually_exclusive_group()
            inputs.add_argument("--json")
            inputs.add_argument("--json-file", type=Path)
            inputs.add_argument("--json-stdin", action="store_true")
    calls = groups.add_parser(
        "calls",
        help="get CALL_ID, wait CALL_ID (wait for a queued, running, or waiting call)",
    ).add_subparsers(dest="action", required=True)
    for action in ("get", "wait"):
        command = calls.add_parser(action)
        command.add_argument("call_id", type=UUID)
        if action == "wait":
            command.add_argument(
                "--timeout",
                type=int,
                default=30,
                help="Polling seconds before returning the current receipt (default: 30)",
            )
    context = groups.add_parser(
        "context",
        help="list, read NAME (read interactive before emitting an interactive question)",
    ).add_subparsers(dest="action", required=True)
    listing = context.add_parser("list")
    listing.add_argument("--cursor")
    listing.add_argument("--limit", type=int, default=100)
    read = context.add_parser("read")
    read.add_argument("name")
    read.add_argument("--offset", type=int, default=0)
    read.add_argument("--limit", type=int, default=8000)
    _add_workers_run(groups)
    args = parser.parse_args(argv)
    if args.group == "workers":
        _select_worker_command(parser, args, worker_command)
    if args.group == "tools" and args.action in {"describe", "call"}:
        _select_tool(parser, args)
    if (args.group, args.action) in {("calls", "wait"), ("tools", "call")} and (args.timeout or 0) < 0:
        parser.error("--timeout must be nonnegative")
    return args


def _split_worker_command(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split off the command `workers run` runs: everything after `--`, whatever options it holds."""
    if argv[:2] != ["workers", "run"] or "--" not in argv:
        return argv, []
    separator = argv.index("--")
    return argv[:separator], argv[separator + 1 :]


def _select_worker_command(parser: argparse.ArgumentParser, args: argparse.Namespace, command: list[str]) -> None:
    """Require the command and usable limits of `workers run`."""
    if not command:
        parser.error("workers run needs a command after `--`: mindroom-agent workers run NAME -- COMMAND")
    if args.timeout <= 0 or args.tail <= 0:
        parser.error("--timeout and --tail must be positive")
    args.command = command


def _add_workers_run(groups: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add `workers run`, whose command follows `--` and is split off before parsing."""
    workers = groups.add_parser(
        "workers",
        help=(
            "run NAME -- COMMAND (run a command in a named worker of its own and exit with its exit code; "
            "needs the agent's named_workers setting)"
        ),
    ).add_subparsers(dest="action", required=True)
    run = workers.add_parser("run", usage="mindroom-agent workers run NAME [options] -- COMMAND [ARG ...]")
    run.add_argument("name", help="the worker's name: 1 to 32 lowercase letters, digits, or hyphens")
    run.add_argument(
        "--resource-profile",
        choices=("small", "standard", "large"),
        help="CPU and memory size of the worker, set by the first command that names it",
    )
    run.add_argument(
        "--timeout",
        type=int,
        default=_WORKER_COMMAND_TIMEOUT_SECONDS,
        help=f"Seconds after which the command is stopped (default: {_WORKER_COMMAND_TIMEOUT_SECONDS})",
    )
    run.add_argument("--tail", type=int, default=100, help="Positive number of lines to print (default: 100)")


def _select_tool(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Accept TOOLKIT.FUNCTION and, for calls, a trailing JSON object in place of --json."""
    call = args.action == "call"
    if call and args.json_argument is None and args.function is not None and args.function.lstrip().startswith("{"):
        args.function, args.json_argument = None, args.function
    if args.function is None:
        toolkit, _, function = args.toolkit.rpartition(".")
        if not toolkit or not function:
            parser.error(
                f"tools {args.action} needs TOOLKIT FUNCTION or TOOLKIT.FUNCTION; "
                f"find function names with: mindroom-agent tools search {args.toolkit}",
            )
        args.toolkit, args.function = toolkit, function
    if call and args.json_argument is not None:
        if args.json is not None or args.json_file is not None or args.json_stdin:
            parser.error("give tool arguments once: a JSON argument or one of --json, --json-file, --json-stdin")
        args.json = args.json_argument


def read_call_arguments(namespace: argparse.Namespace) -> dict[str, object]:
    """Read one selected bounded argument source, requiring a strict object."""
    if namespace.json_file is not None:
        with namespace.json_file.open("rb") as stream:
            payload = stream.read(MAX_ENVELOPE_BYTES + 1)
    elif namespace.json_stdin:
        payload = sys.stdin.buffer.read(MAX_ENVELOPE_BYTES + 1)
    else:
        payload = namespace.json if namespace.json is not None else "{}"
    arguments = read_json(payload)
    if not isinstance(arguments, dict):
        msg = "Tool arguments must be a JSON object"
        raise ValueError(msg)  # noqa: TRY004 - Input validation shares the JSON ValueError contract.
    return cast("dict[str, object]", arguments)


def _exit_code(result: dict[str, object]) -> int:
    status = result.get("status")
    if status in {"queued", "running", "waiting"}:
        return 3
    if status in {"failed", "cancelled"}:
        return 1
    if status == "indeterminate":
        return 4
    return 0


def _wait(client: AgentCliClient, call_id: str, result: dict[str, object], timeout: int) -> dict[str, object]:
    """Poll a pending receipt until it settles or ``timeout`` seconds pass."""
    deadline = time.monotonic() + timeout
    while _exit_code(result) == 3 and time.monotonic() < deadline:
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        if time.monotonic() >= deadline:
            break
        result = client.receipt(call_id)
    return result


class _WorkerSignal(BaseException):
    def __init__(self, signum: int) -> None:
        self.signum = signum


class _WorkerSignals:
    """What SIGINT, SIGTERM and SIGHUP do while `workers run` submits its command and waits for it.

    The first signal raises `_WorkerSignal`, which cancels the command. It is held instead until the command is
    submitted, since a cancel must not overtake the submit, and once the outcome is known. A second signal ends
    the CLI at once, as a signal without a handler does.
    """

    def __init__(self) -> None:
        self.held = True
        self.first: int | None = None

    def handle(self, signum: int, _frame: object) -> None:
        """Raise the first signal unless it is held, and let a second one end the CLI."""
        if self.first is not None:
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)
        self.first = signum
        if not self.held:
            raise _WorkerSignal(signum)

    def release(self) -> None:
        """Stop holding signals, and raise one that arrived meanwhile."""
        self.held = False
        if self.first is not None:
            raise _WorkerSignal(self.first)


@contextmanager
def _worker_signal_handlers() -> Iterator[_WorkerSignals]:
    """Handle SIGINT, SIGTERM and SIGHUP on the main thread, and restore their previous handlers on exit."""
    signals = _WorkerSignals()
    if threading.current_thread() is not threading.main_thread():
        yield signals
        return
    previous = [(signum, signal.signal(signum, signals.handle)) for signum in _WORKER_SIGNALS]
    try:
        yield signals
    finally:
        signals.held = True
        for signum, handler in previous:
            signal.signal(signum, handler)


def _note(message: str) -> None:
    """Print one of `workers run`'s own lines on stderr if it can be; losing one changes nothing else.

    With stderr closed, `print` would use stdout. A stderr that fails a write is dropped for the rest of the run:
    flushing what it kept at exit would fail again and change the exit code.
    """
    if sys.stderr is None:
        return
    try:
        print(message, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        sys.stderr = None


def _emit_progress(
    receipt: dict[str, object] | None,
    last_progress: str | None,
    last_revision: int,
) -> tuple[str | None, int]:
    """Print each new warmup progress message on stderr as one `mindroom-agent:` line."""
    if not isinstance(receipt, dict):
        return last_progress, last_revision
    rev = receipt.get("progress_revision")
    next_revision = rev if isinstance(rev, int) and rev > last_revision else last_revision
    progress = receipt.get("progress")
    if isinstance(progress, str) and progress and (progress != last_progress or next_revision > last_revision):
        _note(f"mindroom-agent: {progress}")
        return progress, next_revision
    return last_progress, next_revision


def _settled(client: AgentCliClient, call_id: str, result: dict[str, object] | None) -> dict[str, object]:
    """Long-poll a submitted call's receipt until it settles, however long its command runs.

    The command runs on in its worker. The retry budget starts at the first failed read's start (or after a lost
    submission response) and includes time spent in HTTP as well as between attempts.
    """
    delay = _POLL_FLOOR_SECONDS
    next_read = 0.0
    deadline = time.monotonic() + _RESULT_UNREACHABLE_SECONDS if result is None else None
    read_error: AgentCliUnavailableError | None = None
    last_progress, last_revision = _emit_progress(result, None, 0)
    while result is None or _exit_code(result) == 3:
        if deadline is not None:
            if (sleep_for := min(delay, deadline - time.monotonic())) > 0:
                time.sleep(sleep_for)
            delay = min(delay * 1.5, 2.0)
        elif (sleep_for := next_read - time.monotonic()) > 0:
            # A server that answers at once, as one without `?wait` does, is still read at this interval.
            time.sleep(sleep_for)
        now = time.monotonic()
        attempt_deadline = now + _RESULT_UNREACHABLE_SECONDS if deadline is None else deadline
        remaining = attempt_deadline - now
        if remaining <= 0:
            raise read_error or AgentCliUnavailableError("Timed out waiting for a call receipt")
        http_timeout = min(remaining, _HTTP_TIMEOUT_SECONDS)
        wait = min(_LONG_POLL_SECONDS, max(0.0, http_timeout - 5.0))
        try:
            result = client.receipt(call_id, timeout=http_timeout, wait=wait, after_progress=last_revision)
        except AgentCliUnavailableError as exc:
            deadline, read_error = attempt_deadline, exc
        else:
            deadline, read_error, delay, next_read = None, None, _POLL_FLOOR_SECONDS, now + _POLL_FLOOR_SECONDS
            last_progress, last_revision = _emit_progress(result, last_progress, last_revision)
    return result


def _cancel_notice(worker: str, call_id: str, signum: int, receipt: dict[str, object] | None) -> str:
    """Say what became of a command a signal cancelled; ``receipt`` is None when it was never submitted."""
    cancelled = f"mindroom-agent: cancelled by {signal.Signals(signum).name}"
    if receipt is None:
        return f"{cancelled} (call {call_id})"
    status = receipt.get("status")
    if status == "failed" and receipt.get("error_kind") == "named_worker_refusal":
        what = f"command never ran ({receipt.get('outcome')})"
    elif status == "waiting":
        what = "the command was waiting for approval and has not run"
    else:
        what = f"the command may still be running in worker {worker}"
    return f"{cancelled}; {what} (call {call_id})"


def _submit_once(client: AgentCliClient, payload: dict[str, object]) -> dict[str, object] | None:
    """Submit a command once: a second submission after an unknown outcome could run it twice.

    None means the server may have accepted the command even though it could not return the receipt. A refusal is
    raised, since the server then accepted nothing.
    """
    try:
        return client.operation(payload)
    except AgentCliRefusedError:
        raise
    except AgentCliUnavailableError:
        return None


def _run_in_worker(args: argparse.Namespace) -> int:
    """Run one command in a named worker, print its output, and return its exit code.

    The command is a call of the agent's own shell tool that names the worker, so it runs under the same rules as
    every other call. It is waited for however long it runs, since its own time limit stops it. A signal while it
    is submitted or waited for cancels it; what the CLI then prints, it prints under the signal handlers it
    started with.
    """
    if os.environ.get(RUN_ID_ENV):
        _note(
            "mindroom-agent: A named worker belongs to a reply in a conversation; "
            "it cannot run from a background script.",
        )
        return _EXIT_NOT_RUN
    arguments: dict[str, object] = {
        "args": args.command,
        "worker": args.name,
        "timeout": args.timeout,
        "tail": args.tail,
    }
    if args.resource_profile is not None:
        arguments["resource_profile"] = args.resource_profile
    call_id = str(uuid4())
    client: AgentCliClient | None = None
    result: dict[str, object] | None = None
    error: Exception | None = None
    signum: int | None = None
    with _worker_signal_handlers() as signals:
        try:
            try:
                # Signals are held until the submit returns: a cancel that overtook it would find no call.
                submitting = AgentCliClient()
                result = _submit_once(
                    submitting,
                    {
                        "operation": "tools.call",
                        "toolkit": "shell",
                        "function": "run_shell_command",
                        "call_id": call_id,
                        "arguments": arguments,
                    },
                )
                client = submitting
                signals.release()
                result = _settled(client, call_id, result)
            except (AgentCliUnavailableError, ValueError) as exc:
                error = exc
            # A signal held through a refused submit is raised now; after that nothing is left to cancel.
            signals.release()
            signals.held = True
        except _WorkerSignal as exc:
            signum, error, result = exc.signum, None, None
            if client is not None:
                result = {}
                with suppress(AgentCliUnavailableError, ValueError):
                    result = client.cancel(call_id, timeout=_CANCEL_TIMEOUT_SECONDS)
    # From here on the CLI prints under the signal handlers it started with.
    if signum is not None and (result is None or result.get("status") != "completed"):
        _note(_cancel_notice(args.name, call_id, signum, result))
        return 128 + signum
    if error is None and result is not None:
        return _print_worker_result(args, result, call_id=call_id)
    if client is None:
        _note(f"mindroom-agent: {error}")
        return _EXIT_NOT_RUN
    _note(f"mindroom-agent: {error}; the command may still be running in worker {args.name} (call {call_id})")
    return _EXIT_UNKNOWN


def _print_worker_result(args: argparse.Namespace, result: dict[str, object], *, call_id: str) -> int:
    """Print a command's output or a tool error, and return its CLI exit code."""
    outcome = result.get("outcome")
    saved_path = None
    saved = cast("dict[str, object]", outcome).get("mindroom_tool_output") if isinstance(outcome, dict) else None
    if isinstance(saved, dict):
        receipt = cast("dict[str, object]", saved)
        if receipt.get("status") == "error":
            _note(f"mindroom-agent: {receipt.get('error', 'Could not save command output')} (call {call_id})")
            return _EXIT_UNKNOWN
        # A long result arrives as its start and the workspace file that holds all of it.
        outcome, saved_path = receipt.get("preview"), receipt.get("path")
    text = outcome if isinstance(outcome, str) else canonical_json(outcome)
    header, _, body = text.partition("\n")
    started_header = f"[worker: {args.name}, started]"
    status = result.get("status")
    if status != "completed" or header not in (f"[worker: {args.name}]", started_header):
        # Reserve 125 for typed named-worker refusals; everything else after submit is 255 with call id.
        if status == "failed" and result.get("error_kind") == "named_worker_refusal":
            _note(f"mindroom-agent: {text}")
            return _EXIT_NOT_RUN
        _note(f"mindroom-agent: {text} (call {call_id})")
        return _EXIT_UNKNOWN
    if header == started_header:
        _note(f"mindroom-agent: started worker {args.name}")
    if (launch_failed := _LAUNCH_FAILED.fullmatch(body)) is not None:
        _note(f"mindroom-agent: {launch_failed[2]}")
        return int(launch_failed[1])
    failed = _FAILED_COMMAND.match(body)
    # `format_shell_timeout` gives an exact line, then only output and stderr sections if present.
    first_line, newline, streams = body.partition("\n")
    stopped = (
        failed is None
        and first_line == f"Stopped after {args.timeout:g}s."
        and (not newline or streams.startswith(("Output:\n", "Stderr:\n")))
    )
    output = body[failed.end() :] if failed is not None else (streams if stopped else body)
    if output:
        print(output)
    if saved_path is not None:
        _note(f"mindroom-agent: full output saved to {saved_path}")
    if failed is not None:
        code = int(failed[1])
        # A command a signal killed exits as a shell reports it: 128 plus the signal.
        return code if code >= 0 else 128 - code
    if stopped:
        _note(f"mindroom-agent: stopped after {args.timeout:g}s")
        return _EXIT_TIMED_OUT
    return 0


def _script_tool_call(args: argparse.Namespace) -> dict[str, object]:
    """Call one tool with a background script's own capability; a response's CLI grant does not reach scripts."""
    if (args.group, args.action) != ("tools", "call") or args.call_id is not None or args.timeout is not None:
        msg = "Inside a background script, mindroom-agent offers only `tools call`, which waits for the result, without --call-id or --timeout"
        raise ValueError(msg)
    receipt: dict[str, object] = {"toolkit": args.toolkit, "function": args.function}
    try:
        outcome = MindRoomTools().call(args.toolkit, args.function, **read_call_arguments(args))
    except MindRoomToolCallError as exc:
        error = {"error": str(exc), "kind": exc.kind}
        # A failed receipt or a refused submission settles the call; any other error leaves whether the tool ran unknown.
        status = "failed" if exc.state in {"failed", "rejected"} else "indeterminate"
        return receipt | {"call_id": exc.call_id, "status": status, "outcome": error}
    except RuntimeError as exc:
        raise AgentCliUnavailableError(str(exc)) from exc
    return receipt | {"status": "completed", "outcome": outcome}


def _parsed_or_exit_code(argv: list[str] | None) -> argparse.Namespace | int:
    """Return the parsed arguments, or the exit code of a command line that asked for help or is invalid."""
    raw_argv = sys.argv[1:] if argv is None else argv
    try:
        return parse_arguments(argv)
    except SystemExit as exc:
        if exc.code and raw_argv[:1] != ["workers"]:
            print(canonical_json({"error": "Invalid CLI arguments"}))
        return int(exc.code or 0)


def main(argv: list[str] | None = None) -> int:
    """Print one bounded JSON envelope and return the documented exit code."""
    call_id: str | None = None
    try:
        args = _parsed_or_exit_code(argv)
        if isinstance(args, int):
            return args
        if args.group == "workers":
            return _run_in_worker(args)
        # Only a background script's launcher sets its run ID.
        if os.environ.get(RUN_ID_ENV):
            result = _script_tool_call(args)
            # The gateway bounded the outcome already; its receipt may still pass the CLI's 64 KiB wire limit.
            print(json.dumps(result, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
            return _exit_code(result)
        if args.group == "calls":
            call_id = str(args.call_id)
            client = AgentCliClient()
            result = client.receipt(call_id)
            if args.action == "wait":
                result = _wait(client, call_id, result, args.timeout)
        else:
            payload = {
                key: value for key, value in vars(args).items() if key not in _LOCAL_ARGUMENTS and value is not None
            }
            payload["operation"] = f"{args.group}.{args.action}"
            if args.action == "call":
                call_id = str(args.call_id or uuid4())
                payload.update(call_id=call_id, arguments=read_call_arguments(args))
            client = AgentCliClient()
            result = client.operation(payload)
            if args.action == "call" and call_id is not None:
                # The call is admitted; if polling fails, print its submission receipt so `calls wait` can resume.
                with suppress(AgentCliUnavailableError):
                    timeout = _CALL_TIMEOUT_SECONDS if args.timeout is None else args.timeout
                    result = _wait(client, call_id, result, timeout)
        print(canonical_json(result))
        return _exit_code(result)
    except (AgentCliUnavailableError, ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, (AgentCliUnavailableError, ValueError)) else "Cannot read CLI input"
        envelope: dict[str, object] = {"error": message}
        if call_id is not None:
            envelope["call_id"] = call_id
        print(canonical_json(envelope))
        print(message, file=sys.stderr)
        return 4 if isinstance(exc, AgentCliUnavailableError) else 2
