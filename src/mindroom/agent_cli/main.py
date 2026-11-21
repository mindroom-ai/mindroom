"""The stdlib-only mindroom-agent console entry point."""

from __future__ import annotations

__all__ = ["main", "parse_arguments", "read_call_arguments"]

import argparse
import json
import os
import re
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from mindroom.agent_cli.client import AgentCliClient, AgentCliUnavailableError
from mindroom.agent_cli.json_io import MAX_ENVELOPE_BYTES, canonical_json, read_json
from mindroom.script_runs.launch_contract import RUN_ID_ENV
from mindroom.script_sdk import MindRoomToolCallError, MindRoomTools

_CALL_TIMEOUT_SECONDS = 30
# How long a command in a named worker may run when `workers run` names no limit.
_WORKER_COMMAND_TIMEOUT_SECONDS = 3600
# `workers run` exits with its command's exit code, so its own failures use codes commands rarely do, as `timeout` and
# `docker run` do: the command was stopped at its time limit, or it never ran or its outcome is unknown.
_EXIT_TIMED_OUT = 124
_EXIT_NOT_RUN = 125
# How long `workers run` keeps reading a result it cannot reach before it gives up on a command that may still run.
_RESULT_UNREACHABLE_SECONDS = 60
# How a shell result leads when its command failed (`format_shell_completion`): its exit code, negative for the
# signal that killed it, then the streams it kept under their labels. A command that succeeds while printing exactly
# these lines first reads as failed, which the shell result cannot tell apart.
_FAILED_COMMAND = re.compile(r"Exit code (-?\d+)(?:\n(?=(?:Output|Stderr):\n)|\Z)")
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


def _settled(client: AgentCliClient, call_id: str, result: dict[str, object] | None) -> dict[str, object]:
    """Read a submitted call's receipt until it settles, however long its command runs.

    The command runs on in its worker. The retry budget starts at the first failed read's start (or after a lost
    submission response) and includes time spent in HTTP as well as between attempts.
    """
    delay = 0.2
    deadline = time.monotonic() + _RESULT_UNREACHABLE_SECONDS if result is None else None
    read_error: AgentCliUnavailableError | None = None
    while result is None or _exit_code(result) == 3:
        sleep_for = delay if deadline is None else min(delay, deadline - time.monotonic())
        if sleep_for > 0:
            time.sleep(sleep_for)
        now = time.monotonic()
        if deadline is None:
            deadline = now + _RESULT_UNREACHABLE_SECONDS
        remaining = deadline - now
        if remaining <= 0:
            if read_error is not None:
                raise read_error
            msg = "Timed out waiting for a call receipt"
            raise AgentCliUnavailableError(msg)
        # A long command is polled less often, so many of them at once stay cheap for MindRoom.
        delay = min(delay * 1.5, 2.0)
        try:
            result = client.receipt(call_id, timeout=remaining)
        except AgentCliUnavailableError as exc:
            read_error = exc
        else:
            deadline = None
            read_error = None
    return result


def _run_in_worker(args: argparse.Namespace) -> int:
    """Run one command in a named worker, print its output, and return its exit code.

    The command is a call of the agent's own shell tool that names the worker, so it runs under the same rules as
    every other call. It is waited for however long it runs, since its own time limit stops it.
    """
    if os.environ.get(RUN_ID_ENV):
        print(
            "mindroom-agent: A named worker belongs to a reply in a conversation; "
            "it cannot run from a background script.",
            file=sys.stderr,
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
    submitted = False
    try:
        client = AgentCliClient()
        # Sent once: a second submission after an unknown outcome could run the command twice.
        try:
            result = client.operation(
                {
                    "operation": "tools.call",
                    "toolkit": "shell",
                    "function": "run_shell_command",
                    "call_id": call_id,
                    "arguments": arguments,
                },
            )
        except AgentCliUnavailableError:
            # The server may have accepted the command even though it could not return the receipt.
            result = None
        submitted = True
        result = _settled(client, call_id, result)
    except (AgentCliUnavailableError, ValueError) as exc:
        running = f"; the command may still be running in worker {args.name}" if submitted else ""
        print(f"mindroom-agent: {exc}{running} (call {call_id})", file=sys.stderr)
        return _EXIT_NOT_RUN
    return _print_worker_result(args, result)


def _print_worker_result(args: argparse.Namespace, result: dict[str, object]) -> int:
    """Print a command's output or a tool error, and return its CLI exit code."""
    outcome = result.get("outcome")
    saved_path = None
    saved = cast("dict[str, object]", outcome).get("mindroom_tool_output") if isinstance(outcome, dict) else None
    if isinstance(saved, dict):
        receipt = cast("dict[str, object]", saved)
        if receipt.get("status") == "error":
            print(f"mindroom-agent: {receipt.get('error', 'Could not save command output')}", file=sys.stderr)
            return _EXIT_NOT_RUN
        # A long result arrives as its start and the workspace file that holds all of it.
        outcome, saved_path = receipt.get("preview"), receipt.get("path")
    text = outcome if isinstance(outcome, str) else canonical_json(outcome)
    header, _, body = text.partition("\n")
    if result.get("status") != "completed" or header not in (
        f"[worker: {args.name}]",
        f"[worker: {args.name}, started]",
    ):
        # The call failed, or its shell ran it without a worker of that name.
        print(f"mindroom-agent: {text}", file=sys.stderr)
        return _EXIT_NOT_RUN
    failed = _FAILED_COMMAND.match(body)
    output = body[failed.end() :] if failed is not None else body
    if output:
        print(output)
    if saved_path is not None:
        print(f"mindroom-agent: full output saved to {saved_path}", file=sys.stderr)
    if failed is not None:
        code = int(failed[1])
        # A command a signal killed exits as a shell reports it: 128 plus the signal.
        return code if code >= 0 else 128 - code
    # `format_shell_timeout` gives an exact line, then only output and stderr sections if present.
    first_line, newline, streams = body.partition("\n")
    stopped = first_line == f"Stopped after {args.timeout:g}s." and (
        not newline or streams.startswith(("Output:\n", "Stderr:\n"))
    )
    return _EXIT_TIMED_OUT if stopped else 0


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
    try:
        return parse_arguments(argv)
    except SystemExit as exc:
        if exc.code:
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
