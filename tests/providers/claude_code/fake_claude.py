"""A scripted stand-in for the ``claude`` binary, speaking stream-JSON with the real Claude Agent SDK over stdio.

Run as ``fake_claude.py <scenario.json> <log.jsonl> <claude arguments...>``. The scenario scripts each process the
SDK starts, in order, and the log records what each process was started with and what it received, one JSON object
per line. Every value in a scenario is invented.

A scenario is ``{"processes": [process, ...]}``; a process past the last scripted one replays the last. A process is::

    {"version": "2.1.328", "account": {...}, "extra_tools": [...], "mcp_status": "connected",
     "turns": [[step, ...], ...]}

with one turn per user message it receives. A step is one model message::

    {"thinking": "...", "text": "...", "tools": [{"id": "toolu_a", "name": "lookup", "input": {...}}],
     "usage": {...}, "stop_reason": "end_turn", "call_before_stop": false, "result_before_stop": false,
     "omit_tool_use_id": false, "error": "rate_limit"}

whose text may hold ``{toolu_a}`` for the text an earlier tool call returned and whose first tool call can start,
or also finish, before the message ends, or one of ``{"rate_limit": {...}}``,
``{"result": {...}}`` to end the turn with those result fields, and ``{"exit": 1}`` to die. A step with ``error`` is
the placeholder message Claude Code writes for a request that failed.
"""
# ruff: noqa: D102

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any

SESSION_ID = "00000000-0000-4000-8000-000000000000"
DEFAULT_USAGE = {"input_tokens": 3, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 5}


class FakeClaude:
    """One scripted Claude Code process."""

    def __init__(self, process: dict[str, Any], log: Path, args: list[str]) -> None:
        self.process = process
        self.log_path = log
        self.args = args
        self.turns = iter(process.get("turns", []))
        self.tools: list[str] | None = None
        self.results: dict[str, str] = {}
        self.usage = dict.fromkeys(DEFAULT_USAGE, 0)
        self.messages = 0
        self.queued: list[dict[str, Any]] = []

    def log(self, event: str, **fields: object) -> None:
        with self.log_path.open("a") as stream:
            stream.write(json.dumps({"event": event, **fields}) + "\n")

    def send(self, message: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    def emit(self, kind: str, **fields: object) -> None:
        self.send({"type": kind, **fields, "session_id": SESSION_ID, "uuid": str(uuid.uuid4())})

    def stream_event(self, event: dict[str, Any]) -> None:
        self.emit("stream_event", event=event, parent_tool_use_id=None)

    def read(self) -> dict[str, Any] | None:
        line = sys.stdin.readline()
        return json.loads(line) if line else None

    def start(self, request: dict[str, Any]) -> str:
        """Send one control request and return its id."""
        request_id = str(uuid.uuid4())
        self.send({"type": "control_request", "request_id": request_id, "request": request})
        return request_id

    def finish(self, request_id: str) -> dict[str, Any]:
        """Return the SDK's answer to a control request, keeping what else arrives meanwhile."""
        while (message := self.read()) is not None:
            if message.get("type") == "control_response" and message["response"].get("request_id") == request_id:
                return message["response"].get("response") or {}
            if message.get("type") == "control_request":
                self.answer(message)
            else:
                self.queued.append(message)
        self.log("exit")
        sys.exit(0)

    def mcp(self, server: str, method: str, params: dict[str, Any] | None, request_id: int | None) -> dict[str, Any]:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if request_id is not None:
            message["id"] = request_id
        request_id = self.start({"subtype": "mcp_message", "server_name": server, "message": message})
        return self.finish(request_id)["mcp_response"]

    def answer(self, message: dict[str, Any]) -> None:
        """Answer one control request of the SDK."""
        request = message["request"]
        response: dict[str, Any] = {}
        if request["subtype"] == "initialize":
            response = {
                "commands": [],
                "models": [],
                "account": self.process.get("account", {"subscriptionType": "Claude Max", "apiProvider": "firstParty"}),
                "claude_code_version": self.process.get("version", "2.1.328"),
            }
        else:
            self.log(request["subtype"])
        self.send(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": message["request_id"], "response": response},
            },
        )

    def servers(self) -> list[str]:
        if "--mcp-config" not in self.args:
            return []
        config = json.loads(self.args[self.args.index("--mcp-config") + 1])
        return list(config.get("mcpServers", {}))

    def connect_tools(self) -> None:
        """Shake hands with each SDK MCP server and list its tools, as Claude Code does before its first turn."""
        self.tools = []
        for server in self.servers():
            self.mcp(
                server,
                "initialize",
                {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "claude", "version": "0"}},
                1,
            )
            self.mcp(server, "notifications/initialized", None, None)
            listed = self.mcp(server, "tools/list", {}, 2)["result"]["tools"]
            self.log("tools_list", server=server, tools=listed)
            self.tools += [f"mcp__{server}__{tool['name']}" for tool in listed]

    def init(self) -> None:
        if self.tools is None:
            self.connect_tools()
        assert self.tools is not None
        self.emit(
            "system",
            subtype="init",
            cwd=str(Path.cwd()),
            tools=[*self.tools, *self.process.get("extra_tools", [])],
            mcp_servers=[
                {"name": server, "status": self.process.get("mcp_status", "connected"), "source": "sdk"}
                for server in self.servers()
            ],
            model="claude-invented-1",
            permissionMode="dontAsk",
            apiKeySource="none",
            claude_code_version=self.process.get("version", "2.1.328"),
        )

    def text_of(self, template: str) -> str:
        for call_id, result in self.results.items():
            template = template.replace("{" + call_id + "}", result)
        return template

    def block(self, message: dict[str, Any], index: int, block: dict[str, Any], deltas: list[dict[str, Any]]) -> None:
        """Stream one content block as Claude Code does: its start, its pieces, the complete message, its stop."""
        empty = {"thinking": {"thinking": ""}, "text": {"text": ""}, "tool_use": {"input": {}}}[block["type"]]
        self.stream_event({"type": "content_block_start", "index": index, "content_block": {**block, **empty}})
        for delta in deltas:
            self.stream_event({"type": "content_block_delta", "index": index, "delta": delta})
        self.emit("assistant", message={**message, "content": [block]}, parent_tool_use_id=None)
        self.stream_event({"type": "content_block_stop", "index": index})

    def start_call(self, call: dict[str, Any], *, with_id: bool) -> str:
        """Call one MindRoom tool through its MCP server and return the id of the request that carries the call."""
        meta = {"claudecode/toolUseId": call["id"], "progressToken": self.messages} if with_id else {}
        params = {"name": call["name"], "arguments": call.get("input", {}), "_meta": meta}
        message = {"jsonrpc": "2.0", "id": 10 + len(self.results), "method": "tools/call", "params": params}
        return self.start({"subtype": "mcp_message", "server_name": "mindroom", "message": message})

    def finish_call(self, call: dict[str, Any], request_id: str) -> None:
        """Wait for one tool call's result and echo it as Claude Code does."""
        answer = self.finish(request_id)["mcp_response"]
        result = answer.get("result") or {"content": [{"type": "text", "text": json.dumps(answer)}], "isError": True}
        self.log("tool_result", id=call["id"], result=result)
        self.results[call["id"]] = "".join(item.get("text", "") for item in result["content"])
        echoed = {"type": "tool_result", "tool_use_id": call["id"], "content": result["content"]}
        self.emit("user", message={"role": "user", "content": [echoed]}, parent_tool_use_id=None)

    def model_message(self, step: dict[str, Any]) -> str:
        """Stream one scripted model message, run its tool calls, and return its text."""
        self.messages += 1
        usage = {**DEFAULT_USAGE, **step.get("usage", {})}
        for key, value in usage.items():
            self.usage[key] += value
        text = self.text_of(step.get("text", ""))
        message = {
            "id": f"msg_{self.messages:04d}",
            "type": "message",
            "role": "assistant",
            "model": "claude-invented-1",
            "content": [],
            "stop_reason": None,
            "usage": {**usage, "output_tokens": 1},
        }
        if error := step.get("error"):
            synthetic = {**message, "model": "<synthetic>", "content": [{"type": "text", "text": text}]}
            self.emit("assistant", message=synthetic, parent_tool_use_id=None, error=error)
            return text
        self.stream_event({"type": "message_start", "message": message})
        index = 0
        if thinking := step.get("thinking"):
            half = len(thinking) // 2
            deltas = [{"type": "thinking_delta", "thinking": piece} for piece in (thinking[:half], thinking[half:])]
            self.block(message, index, {"type": "thinking", "thinking": thinking, "signature": "invented"}, deltas)
            index += 1
        if text:
            half = len(text) // 2
            deltas = [{"type": "text_delta", "text": piece} for piece in (text[:half], text[half:])]
            self.block(message, index, {"type": "text", "text": text}, deltas)
            index += 1
        calls = step.get("tools", [])
        for call in calls:
            block = {
                "type": "tool_use",
                "id": call["id"],
                "name": f"mcp__mindroom__{call['name']}",
                "input": call.get("input", {}),
            }
            partial = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            self.block(message, index, block, [partial])
            index += 1
        with_id = not step.get("omit_tool_use_id")
        stop_reason = step.get("stop_reason", "tool_use" if calls else "end_turn")
        # Claude Code can start a call before the model message that made it has ended.
        early = [(call, self.start_call(call, with_id=with_id)) for call in calls[:1] if step.get("call_before_stop")]
        if step.get("result_before_stop"):
            self.finish_call(*early.pop())
        self.stream_event(
            {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": usage},
        )
        self.stream_event({"type": "message_stop"})
        for call, request_id in early:
            self.finish_call(call, request_id)
        for call in calls[1 if step.get("call_before_stop") else 0 :]:
            self.finish_call(call, self.start_call(call, with_id=with_id))
        return text

    def turn(self, user: dict[str, Any]) -> None:
        """Answer one user message with the next scripted turn."""
        self.log("user", content=user["message"]["content"], client_composed=user.get("client_composed"))
        self.init()
        steps = next(self.turns, [{"text": "(unscripted turn)"}])
        text = ""
        result: dict[str, Any] = {}
        for step in steps:
            if "exit" in step:
                self.log("exit")
                sys.exit(step["exit"])
            elif "rate_limit" in step:
                self.emit("rate_limit_event", rate_limit_info=step["rate_limit"])
            elif "result" in step:
                result = step["result"]
            else:
                text = self.model_message(step)
        self.emit(
            "result",
            **{
                "subtype": "success",
                "is_error": False,
                "duration_ms": 1,
                "duration_api_ms": 1,
                "num_turns": self.messages,
                "stop_reason": "end_turn",
                "usage": self.usage,
                "result": text,
                **result,
            },
        )

    def run(self) -> None:
        system_prompt = ""
        if "--system-prompt-file" in self.args:
            system_prompt = Path(self.args[self.args.index("--system-prompt-file") + 1]).read_text()
        elif "--system-prompt" in self.args:
            system_prompt = self.args[self.args.index("--system-prompt") + 1]
        self.log("spawn", args=self.args, cwd=str(Path.cwd()), system_prompt=system_prompt)
        while (message := self.queued.pop(0) if self.queued else self.read()) is not None:
            if message.get("type") == "control_request":
                self.answer(message)
            elif message.get("type") == "user":
                self.turn(message)
        self.log("exit")


def main() -> None:
    """Run the process the scenario scripts for this start."""
    scenario = json.loads(Path(sys.argv[1]).read_text())
    log = Path(sys.argv[2])
    started = log.read_text().count('"event": "spawn"') if log.exists() else 0
    processes = scenario["processes"]
    FakeClaude(processes[min(started, len(processes) - 1)], log, sys.argv[3:]).run()


if __name__ == "__main__":
    main()
