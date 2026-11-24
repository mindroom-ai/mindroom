"""Summarize the LLM request log: when each request went out, for which agent, room, and thread, and with which tools.

The log exists only with ``debug.log_llm_requests: true``, in ``<storage>/logs/llm_requests/`` unless
``debug.llm_request_log_dir`` moves it. Its timestamps are local time with a UTC offset and its daily files are named
by local date, while the backend log and ``tracking/tool_calls.jsonl`` use UTC with a ``Z`` suffix, so this prints UTC
and reads ``--since`` and ``--until`` without an offset as UTC, the form ``date -u +%Y-%m-%dT%H:%M:%S`` prints:

    llm_requests.py --storage "$MINDROOM_STORAGE_PATH" --since 2026-11-03T19:48:00 --agent mind --show 'You are'

``--grep`` and ``--show`` search the request body as JSON text, so they work for every provider's body shape.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _utc(timestamp: str) -> datetime:
    """Parse an ISO timestamp, reading one without an offset as UTC."""
    parsed = datetime.fromisoformat(timestamp)
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def _tool_names(body: dict[str, Any]) -> list[str | None]:
    """Return the names of the tools a request body offers, in the OpenAI, Anthropic, and Gemini shapes.

    A Gemini body is the google-genai call, with its tools under ``config``, and a native tool such as
    ``google_search`` named only by its key.
    """
    names: list[str | None] = []
    for tool in body.get("tools") or (body.get("config") or {}).get("tools") or []:
        if declarations := tool.get("function_declarations") or tool.get("functionDeclarations"):
            names += [declaration.get("name") for declaration in declarations]
        else:
            names.append(
                tool.get("name")
                or (tool.get("function") or {}).get("name")
                or tool.get("type")
                or next(iter(tool), None),
            )
    return names


def requests(log_dir: Path) -> list[tuple[datetime, dict[str, Any]]]:
    """Return every logged request with its UTC time, oldest first; response records carry no body and are skipped."""
    found = []
    for path in sorted(log_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if "body" in record:
                found.append((_utc(record["timestamp"]), record))
    return sorted(found, key=lambda item: item[0])


def main() -> None:
    """Print one summary per matching request."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--storage", default=os.environ.get("MINDROOM_STORAGE_PATH", "mindroom_data"))
    parser.add_argument("--log-dir", help="the configured debug.llm_request_log_dir, when set")
    parser.add_argument("--since", help="ISO time; without an offset it is UTC")
    parser.add_argument("--until", help="ISO time; without an offset it is UTC")
    parser.add_argument("--agent", help="substring of the request's agent_id")
    parser.add_argument("--room", help="the request's room_id")
    parser.add_argument("--id", help="one request_log_id")
    parser.add_argument("--grep", help="only requests whose body contains this text")
    parser.add_argument("--show", action="append", default=[], help="print the body text around this needle")
    parser.add_argument("--width", type=int, default=600, help="characters --show prints")
    args = parser.parse_args()
    log_dir = Path(args.log_dir) if args.log_dir else Path(args.storage) / "logs" / "llm_requests"
    since = _utc(args.since) if args.since else None
    until = _utc(args.until) if args.until else None
    for sent, record in requests(log_dir):
        text = json.dumps(record["body"], ensure_ascii=False)
        if (
            (since and sent < since)
            or (until and sent > until)
            or (args.agent and args.agent not in (record.get("agent_id") or ""))
            or (args.room and record.get("room_id") != args.room)
            or (args.id and record["request_log_id"] != args.id)
            or (args.grep and args.grep not in text)
        ):
            continue
        print(
            f"{sent.isoformat(timespec='seconds').replace('+00:00', 'Z')} {record.get('model_name')}/"
            f"{record.get('model_id')} agent={record.get('agent_id')} room={record.get('room_id')} "
            f"thread={record.get('thread_id')} requester={record.get('requester_id')} id={record['request_log_id']}",
        )
        print(f"    tools={_tool_names(record['body'])} deferred={record.get('deferred_tool_names')}")
        for needle in args.show:
            if (index := text.find(needle)) >= 0:
                snippet = text[max(0, index - 100) : index + args.width].replace("\\n", " | ")
                print(f"    [{needle}] {snippet}")


if __name__ == "__main__":
    main()
