"""The stdlib-only mindroom-agent console entry point."""

from __future__ import annotations

__all__ = ["main", "parse_arguments", "read_call_arguments"]

import argparse
import base64
import json
import os
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
# How long `workers run` keeps trying to reach its command's output before it gives up on a command that may run.
_OUTPUT_UNREACHABLE_SECONDS = 60
# What a command whose reader left exits with when SIGPIPE ends it, which is how `workers run` exits then.
_EXIT_STDOUT_CLOSED = 128 + signal.SIGPIPE
_CANCEL_TIMEOUT_SECONDS = 8.0
# The most bytes of stdin sent to the command at once, and the longest wait until a chunk it has not taken is sent
# again.
_STDIN_CHUNK_BYTES = 32 * 1024
_STDIN_RETRY_SECONDS = 1.0
_WORKER_SIGNALS = tuple(
    sig for name in ("SIGINT", "SIGTERM", "SIGHUP") if (sig := getattr(signal, name, None)) is not None
)
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
    run.add_argument("--tail", type=int, default=100, help="Ignored: the command's whole output is streamed")


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


class _WorkerStderr:
    """Best-effort stderr writer for `workers run`; rewrites progress in place on a TTY.

    With stderr closed, `print` would use stdout. A stderr that fails a write is dropped for the rest of the run:
    flushing what it kept at exit would fail again and change the exit code.
    """

    def __init__(self) -> None:
        self._progress_open = False

    def _write(self, text: str) -> bool:
        if sys.stderr is None:
            return False
        try:
            sys.stderr.write(text)
            sys.stderr.flush()
        except (OSError, ValueError):
            sys.stderr = None
            self._progress_open = False
            return False
        return True

    def finish_progress(self) -> None:
        """End an in-place TTY progress line before any other CLI line, command output, or exit."""
        if self._progress_open:
            self._progress_open = False
            self._write("\n")

    def progress(self, message: str) -> None:
        """Write one warmup progress message, rewriting in place on a TTY."""
        is_tty = False
        if sys.stderr is not None:
            with suppress(OSError, ValueError):
                is_tty = bool(sys.stderr.isatty())
        if is_tty:
            # A line wider than the terminal wraps, and the next rewrite would clear only its last row.
            columns = 0
            with suppress(OSError, ValueError):
                columns = os.get_terminal_size(sys.stderr.fileno()).columns
            self._progress_open = True
            if not self._write(f"\r\x1b[K{message[: columns - 1] if columns > 1 else message}"):
                self._progress_open = False
            return
        self.note(message)

    def note(self, message: str) -> None:
        """Write one complete `workers run` line on stderr, closing any open progress line first."""
        self.finish_progress()
        self._write(f"{message}\n")

    def command_output(self, data: bytes) -> None:
        """Write the command's stderr where the CLI's own lines go, and as they do: if it can be."""
        self.finish_progress()
        if sys.stderr is None:
            return
        try:
            sys.stderr.buffer.write(data)
            sys.stderr.buffer.flush()
        except (OSError, ValueError, AttributeError):
            sys.stderr = None


class _StdoutClosedError(Exception):
    """The CLI's stdout takes no more of the command's output."""


def _write_stdout(data: bytes) -> None:
    try:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    except (OSError, ValueError, AttributeError):
        raise _StdoutClosedError from None


def _discard_stdout() -> None:
    """Point stdout nowhere, so output it still holds for a reader that left or stalled cannot hold up the exit."""
    with suppress(OSError, ValueError, AttributeError):
        nowhere = os.open(os.devnull, os.O_WRONLY)
        os.dup2(nowhere, sys.stdout.fileno())
        os.close(nowhere)


def _forward_stdin(client: AgentCliClient, call_id: str, done: threading.Event, stderr: _WorkerStderr) -> None:
    """Send the CLI's stdin to the command in order as it arrives, then its end; a terminal sends its end at once.

    Each chunk is answered once the command took it, so a command that reads slowly slows this down. It runs in a
    daemon thread until ``done``: a command that ends without reading its stdin does not wait for it. A chunk that
    is refused for good is reported, and the command, which would wait for it, is cancelled.
    """
    seq = 0
    while True:
        try:
            piped = sys.stdin is not None and not sys.stdin.isatty()
            data = os.read(sys.stdin.fileno(), _STDIN_CHUNK_BYTES) if piped else b""
        except (OSError, ValueError):
            data = b""
        taking_more, delay = None, _STDIN_RETRY_SECONDS / 4
        while taking_more is None:
            if done.is_set():
                return
            try:
                taking_more = bool(client.stdin(call_id, seq, data, eof=not data).get("open"))
            except ValueError as exc:
                stderr.note(f"mindroom-agent: the command's stdin was refused ({exc}); cancelling the command")
                with suppress(AgentCliUnavailableError, ValueError):
                    client.cancel(call_id, timeout=_CANCEL_TIMEOUT_SECONDS)
                return
            except AgentCliUnavailableError:
                # The command has not read yet, or the answer was lost: the same chunk again is written once.
                done.wait(delay)
                delay = min(2 * delay, _STDIN_RETRY_SECONDS)
        if not data or not taking_more:
            return
        seq += 1


def _frames(client: AgentCliClient, call_id: str) -> Iterator[dict[str, object]]:
    """Yield a streamed call's frames as they arrive.

    The command runs on in its worker, so a stream that cannot be reached is tried again for a minute. Once its
    first frame arrived it is not: MindRoom keeps no output for a second reader, and cancels the call of one that left.
    """
    delay, give_up = 0.2, None
    while True:
        started, reached = time.monotonic(), False
        try:
            for frame in client.stream(call_id):
                reached = True
                yield frame
        except AgentCliUnavailableError:
            give_up = started + _OUTPUT_UNREACHABLE_SECONDS if give_up is None else give_up
            if reached or (remaining := give_up - time.monotonic()) <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(delay * 1.5, 2.0)
        else:
            return


def _relay(
    client: AgentCliClient,
    call_id: str,
    worker: str,
    ended: dict[str, object],
    stderr: _WorkerStderr,
) -> dict[str, object]:
    """Write a command's output as it arrives, stdout to stdout and stderr to stderr, and return its call's receipt.

    The command's exit, which follows the last of its output, is kept in ``ended``, so a signal that then stops
    the relay loses nothing. A stream that ends before the exit or the receipt leaves the command's outcome unknown.
    """
    done = threading.Event()
    try:
        return _relay_frames(client, call_id, worker, ended, done, stderr)
    finally:
        done.set()


def _relay_frames(
    client: AgentCliClient,
    call_id: str,
    worker: str,
    ended: dict[str, object],
    done: threading.Event,
    stderr: _WorkerStderr,
) -> dict[str, object]:
    for frame in _frames(client, call_id):
        kind = frame.get("t")
        if kind is None:
            return frame
        if kind == "out":
            stderr.finish_progress()
            _write_stdout(base64.b64decode(str(frame.get("b"))))
        elif kind == "err":
            stderr.command_output(base64.b64decode(str(frame.get("b"))))
        elif kind == "progress":
            stderr.progress(f"mindroom-agent: {frame.get('text')}")
        elif kind == "started":
            if frame.get("header") == f"[worker: {worker}, started]":
                stderr.note(f"mindroom-agent: started worker {worker}")
            threading.Thread(target=_forward_stdin, args=(client, call_id, done, stderr), daemon=True).start()
        elif kind == "exit":
            ended.update(frame)
    if ended:
        return {}
    msg = "The command's output stream ended early"
    raise AgentCliUnavailableError(msg)


def _cancel_notice(worker: str, call_id: str, signum: int, receipt: dict[str, object] | None) -> str:
    """Say what became of a command a signal cancelled; ``receipt`` is None when it was never submitted."""
    cancelled = f"mindroom-agent: cancelled by {signal.Signals(signum).name}"
    if receipt is None:
        return f"{cancelled} (call {call_id})"
    status = receipt.get("status")
    if status == "cancelled" and receipt.get("cancel_outcome") == "never_ran":
        return f"{cancelled}; the command never ran in worker {worker}"
    if status == "cancelled" and receipt.get("cancel_outcome") == "stopped":
        # The command's exit followed the stop, so nothing is left to look up under a call ID.
        return f"{cancelled}; stopped the command in worker {worker}"
    if status == "failed" and receipt.get("error_kind") == "named_worker_refusal":
        what = f"command never ran ({receipt.get('outcome')})"
    elif status == "waiting":
        what = "the command was waiting for approval and has not run"
    elif status == "completed":
        what = "the command had ended, and the rest of its output was not read"
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
    """Run one command in a named worker with its output streamed as it arrives, and return its exit code.

    The command is a call of the agent's own shell tool that names the worker, so it runs under the same rules as
    every other call. It is read however long it runs, since its own time limit stops it. A signal while it is
    submitted or read cancels it, and so does a stdout that takes no more; the CLI's own closing lines are printed
    under the signal handlers it started with.
    """
    stderr = _WorkerStderr()
    if os.environ.get(RUN_ID_ENV):
        stderr.note(
            "mindroom-agent: A named worker belongs to a reply in a conversation; "
            "it cannot run from a background script.",
        )
        return _EXIT_NOT_RUN
    arguments: dict[str, object] = {"args": args.command, "worker": args.name, "timeout": args.timeout}
    # `tail` stays an argument, as before output was streamed, so approval, hooks and audit see the same call.
    arguments["tail"] = args.tail
    if args.resource_profile is not None:
        arguments["resource_profile"] = args.resource_profile
    call_id = str(uuid4())
    client: AgentCliClient | None = None
    result: dict[str, object] | None = None
    ended: dict[str, object] = {}
    error: Exception | None = None
    signum: int | None = None
    try:
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
                            "stream": True,
                        },
                    )
                    client = submitting
                    signals.release()
                    result = _relay(client, call_id, args.name, ended, stderr)
                except _StdoutClosedError:
                    with suppress(AgentCliUnavailableError, ValueError):
                        submitting.cancel(call_id, timeout=_CANCEL_TIMEOUT_SECONDS)
                    _discard_stdout()
                    return _EXIT_STDOUT_CLOSED
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
    finally:
        # From here on the CLI prints under the signal handlers it started with.
        stderr.finish_progress()
    if ended or signum is not None or error is None:
        return _worker_exit_code(args, call_id, ended, signum, result, stderr)
    if client is None:
        stderr.note(f"mindroom-agent: {error}")
        return _EXIT_NOT_RUN
    stderr.note(f"mindroom-agent: {error}; the command may still be running in worker {args.name} (call {call_id})")
    return _EXIT_UNKNOWN


def _worker_exit_code(
    args: argparse.Namespace,
    call_id: str,
    ended: dict[str, object],
    signum: int | None,
    receipt: dict[str, object] | None,
    stderr: _WorkerStderr,
) -> int:
    """Return the exit code of a command that ended, of a signal that cancelled it, or of a call that ended unrun."""
    if ended:
        # The command ended and all of its output is written, whatever came after.
        if ended.get("timed_out"):
            stderr.note(f"mindroom-agent: stopped after {args.timeout:g}s")
            return _EXIT_TIMED_OUT
        code = cast("int", ended.get("code"))
        # A command a signal killed exits as a shell reports it: 128 plus the signal.
        return code if code >= 0 else 128 - code
    if signum is not None:
        _discard_stdout()
        stderr.note(_cancel_notice(args.name, call_id, signum, receipt))
        return 128 + signum
    # The call ended without its command's exit: it was refused, failed, or was cancelled.
    outcome = (receipt or {}).get("outcome")
    text = outcome if isinstance(outcome, str) else canonical_json(outcome)
    # Reserve 125 for typed named-worker refusals; everything else after submit is 255 with call id.
    if (
        receipt is not None
        and receipt.get("status") == "failed"
        and receipt.get("error_kind") == "named_worker_refusal"
    ):
        stderr.note(f"mindroom-agent: {text}")
        return _EXIT_NOT_RUN
    stderr.note(f"mindroom-agent: {text} (call {call_id})")
    return _EXIT_UNKNOWN


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
