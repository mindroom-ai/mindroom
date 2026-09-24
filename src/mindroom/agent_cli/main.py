"""The stdlib-only mindroom-agent console entry point."""

from __future__ import annotations

__all__ = ["main", "parse_arguments", "read_call_arguments"]

import argparse
import sys
import time
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from mindroom.agent_cli.client import AgentCliClient, AgentCliUnavailableError
from mindroom.agent_cli.json_io import MAX_ENVELOPE_BYTES, canonical_json, read_json


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse only fixed operations; runtime configuration is unnecessary for help."""
    parser = argparse.ArgumentParser(prog="mindroom-agent")
    groups = parser.add_subparsers(dest="group", required=True)
    tools = groups.add_parser(
        "tools",
        help="list, search QUERY, describe TOOLKIT FUNCTION, call TOOLKIT FUNCTION",
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
        command.add_argument("toolkit")
        command.add_argument("function")
        if action == "call":
            command.add_argument("--call-id", type=UUID)
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
    args = parser.parse_args(argv)
    if args.group == "calls" and args.action == "wait" and args.timeout < 0:
        parser.error("--timeout must be nonnegative")
    return args


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
    return 0


def main(argv: list[str] | None = None) -> int:
    """Print one bounded JSON envelope and return the documented exit code."""
    call_id: str | None = None
    try:
        try:
            args = parse_arguments(argv)
        except SystemExit as exc:
            if exc.code:
                print(canonical_json({"error": "Invalid CLI arguments"}))
            return int(exc.code or 0)
        if args.group == "calls":
            call_id = str(args.call_id)
            client = AgentCliClient()
            result = client.receipt(call_id)
            deadline = time.monotonic() + args.timeout if args.action == "wait" else 0
            while args.action == "wait" and _exit_code(result) == 3 and time.monotonic() < deadline:
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
                if time.monotonic() >= deadline:
                    break
                result = client.receipt(call_id)
        else:
            payload = {
                key: value
                for key, value in vars(args).items()
                if key not in {"group", "action", "json", "json_file", "json_stdin", "call_id"} and value is not None
            }
            payload["operation"] = f"{args.group}.{args.action}"
            if args.action == "call":
                call_id = str(args.call_id or uuid4())
                payload.update(call_id=call_id, arguments=read_call_arguments(args))
            result = AgentCliClient().operation(payload)
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
