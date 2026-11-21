"""A scripted stand-in for the ``claude`` binary, speaking stream-JSON with the real Claude Agent SDK over stdio.

Run as ``fake_claude.py <scenario.json> <log.jsonl> <claude arguments...>``. The scenario scripts each process the
SDK starts, in order, and the log records what each process was started with and what it received, one JSON object
per line. Every value in a scenario is invented.

A scenario is ``{"processes": [process, ...]}``; a process past the last scripted one replays the last. A process is::

    {"version": "2.1.328", "account": {...}, "mcp_status": "connected", "status": [...], "init": {...},
     "later_init": {...}, "status_error": "...", "status_stall": false, "turns": [[step, ...], ...]}

with one turn per user message it receives. ``status`` replaces the MCP servers it reports before its first turn,
or with null leaves them out; ``status_error`` rejects the request for them and ``status_stall`` never answers it.
``init`` replaces fields of the message that starts each turn: a null field is left out, and a null ``init``
sends no such message. ``later_init`` does the same for the turns after the first. A step is one model message::

    {"thinking": "...", "text": "...", "tools": [{"id": "toolu_a", "name": "lookup", "input": {...}}],
     "usage": {...}, "stop_reason": "end_turn", "call_before_stop": false, "result_before_stop": false,
     "omit_tool_use_id": false, "error": "rate_limit", "start_text": false, "complete_only": false,
     "abandon": false, "cancel": false, "repeat": "toolu_a"}

whose text may hold ``{toolu_a}`` for the text an earlier tool call returned and whose first tool call can start,
or also finish, before the message ends, or one of ``{"rate_limit": {...}}``,
``{"result": {...}}`` to end the turn with those result fields, ``{"exit": 1}`` to die, ``{"wait": true}`` to send
nothing more, and ``{"die_after_result": true}`` to die once the turn has ended. A step with ``error`` is the placeholder message
Claude Code writes for a request that failed. ``start_text`` puts the first half of the text in the event that
starts its block, and ``complete_only`` sends the message whole, without streaming it and without running its
tools. A tool with ``"mcp": {"name": ..., "input": ...}`` reaches the tool server as that other call, and one with
``"hidden": true`` reaches it without the model having made it. ``abandon`` starts each tool call and goes on
without its result, ``cancel`` also cancels it, and ``repeat`` calls the tool of that id once more after the
message's calls returned. A tool with ``"denied": true`` never reaches the tool server, one with ``"unused": true``
reaches it and its result is dropped, and one with ``raw_name``
is written under that name as it is. A step with ``after_file`` starts once that file exists.
"""
# ruff: noqa: D102

from __future__ import annotations

import json
import os
import stat
import sys
import time
import uuid
from pathlib import Path
from typing import Any

SESSION_ID = "00000000-0000-4000-8000-000000000000"
DEFAULT_USAGE = {"input_tokens": 3, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 5}
# The names of the same counters in the ledger a result carries, which sums every model's use since the process started.
LEDGER_NAMES = {
    "input_tokens": "inputTokens",
    "cache_creation_input_tokens": "cacheCreationInputTokens",
    "cache_read_input_tokens": "cacheReadInputTokens",
    "output_tokens": "outputTokens",
}


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
        self.ledger = dict.fromkeys(LEDGER_NAMES.values(), 0)
        self.messages = 0
        self.rpc_id = 10
        self.queued: list[dict[str, Any]] = []
        self.turn_number = 0

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
        elif request["subtype"] == "mcp_status":
            if self.process.get("status_stall"):
                return
            if error := self.process.get("status_error"):
                rejected = {"subtype": "error", "request_id": message["request_id"], "error": error}
                self.send({"type": "control_response", "response": rejected})
                return
            if self.tools is None:
                self.connect_tools()
            assert self.tools is not None
            names = [{"name": tool.rpartition("__")[2]} for tool in self.tools]
            servers = [{"name": server, "status": self.status(), "tools": names} for server in self.servers()]
            reported = self.process.get("status", servers)
            response = {} if reported is None else {"mcpServers": reported}
        else:
            self.log(request["subtype"])
        self.send(
            {
                "type": "control_response",
                "response": {"subtype": "success", "request_id": message["request_id"], "response": response},
            },
        )

    def status(self) -> str:
        return self.process.get("mcp_status", "connected")

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
        changed = self.process.get("init", {})
        if self.turn_number > 1:
            changed = self.process.get("later_init", changed)
        if changed is None:
            return
        fields = {
            "cwd": str(Path.cwd()),
            "tools": self.tools,
            "mcp_servers": [{"name": server, "status": self.status(), "source": "sdk"} for server in self.servers()],
            "model": "claude-invented-1",
            "permissionMode": "dontAsk",
            "apiKeySource": "none",
            "claude_code_version": self.process.get("version", "2.1.328"),
            **changed,
        }
        self.emit("system", subtype="init", **{key: value for key, value in fields.items() if value is not None})

    def text_of(self, template: str) -> str:
        for call_id, result in self.results.items():
            template = template.replace("{" + call_id + "}", result)
        return template

    def block(
        self,
        message: dict[str, Any],
        index: int,
        block: dict[str, Any],
        deltas: list[dict[str, Any]],
        start: dict[str, Any] | None = None,
    ) -> None:
        """Stream one content block as Claude Code does: its start, its pieces, the complete message, its stop."""
        empty = {"thinking": {"thinking": ""}, "text": {"text": ""}, "tool_use": {"input": {}}}[block["type"]]
        started = {**block, **empty, **(start or {})}
        self.stream_event({"type": "content_block_start", "index": index, "content_block": started})
        for delta in deltas:
            self.stream_event({"type": "content_block_delta", "index": index, "delta": delta})
        self.emit("assistant", message={**message, "content": [block]}, parent_tool_use_id=None)
        self.stream_event({"type": "content_block_stop", "index": index})

    def start_call(self, call: dict[str, Any], *, with_id: bool) -> str:
        """Call one MindRoom tool through its MCP server and return the id of the request that carries the call."""
        meta = {"claudecode/toolUseId": call["id"], "progressToken": self.messages} if with_id else {}
        sent = {**call, **call.get("mcp", {})}
        params = {"name": sent["name"], "arguments": sent.get("input", {}), "_meta": meta}
        self.rpc_id += 1
        message = {"jsonrpc": "2.0", "id": self.rpc_id, "method": "tools/call", "params": params}
        return self.start({"subtype": "mcp_message", "server_name": "mindroom", "message": message})

    def abandon_call(self, call: dict[str, Any], *, with_id: bool, cancel: bool) -> None:
        """Start one tool call and go on without its result, as Claude Code does when the call takes it too long."""
        self.start_call(call, with_id=with_id)
        if cancel:
            self.mcp("mindroom", "notifications/cancelled", {"requestId": self.rpc_id, "reason": "timeout"}, None)
        self.results[call["id"]] = "(no result)"

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
            self.ledger[LEDGER_NAMES[key]] += value
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
        if step.get("complete_only"):
            blocks = [{"type": "text", "text": text}] if text else []
            blocks += [
                {"type": "tool_use", "id": call["id"], "name": f"mcp__mindroom__{call['name']}", "input": call["input"]}
                for call in step.get("tools", [])
            ]
            for block in blocks:
                whole = {**message, "content": [block], "usage": usage}
                self.emit("assistant", message=whole, parent_tool_use_id=None)
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
            start = {"text": text[:half]} if step.get("start_text") else None
            self.block(message, index, {"type": "text", "text": text}, deltas[1:] if start else deltas, start)
            index += 1
        calls = step.get("tools", [])
        for call in calls:
            if call.get("hidden"):
                continue
            block = {
                "type": "tool_use",
                "id": call["id"],
                "name": call.get("raw_name") or f"mcp__mindroom__{call['name']}",
                "input": call.get("input", {}),
            }
            partial = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            self.block(message, index, block, [partial])
            index += 1
        self.run_calls(step, calls, usage)
        return text

    def run_calls(self, step: dict[str, Any], calls: list[dict[str, Any]], usage: dict[str, int]) -> None:
        """End one streamed model message and bring its tool calls to the tool server."""
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
            if call.get("denied"):
                self.results[call["id"]] = "(denied)"
            elif call.get("unused"):
                self.finish(self.start_call(call, with_id=with_id))
                self.results[call["id"]] = "(unused)"
            elif step.get("abandon") or step.get("cancel"):
                self.abandon_call(call, with_id=with_id, cancel=bool(step.get("cancel")))
            else:
                self.finish_call(call, self.start_call(call, with_id=with_id))
        for call in calls:
            if call["id"] == step.get("repeat"):
                self.finish_call(call, self.start_call(call, with_id=with_id))

    def turn(self, user: dict[str, Any]) -> None:
        """Answer one user message with the next scripted turn."""
        self.log("user", content=user["message"]["content"], client_composed=user.get("client_composed"))
        self.usage = dict.fromkeys(DEFAULT_USAGE, 0)
        self.turn_number += 1
        self.init()
        steps = next(self.turns, [{"text": "(unscripted turn)"}])
        text = ""
        result: dict[str, Any] = {}
        dies = False
        for step in steps:
            if "exit" in step:
                self.log("exit")
                sys.exit(step["exit"])
            elif "wait" in step:
                self.finish("")
            elif "die_after_result" in step:
                dies = True
            elif "rate_limit" in step:
                self.emit("rate_limit_event", rate_limit_info=step["rate_limit"])
            elif "result" in step:
                result = step["result"]
            else:
                while "after_file" in step and not Path(step["after_file"]).exists():
                    time.sleep(0.01)
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
                "modelUsage": {"claude-invented-1": self.ledger},
                "result": text,
                **result,
            },
        )
        self.log("turn_end")
        if dies:
            self.log("exit")
            sys.exit(0)

    def run(self) -> None:
        system_prompt = ""
        modes: list[int] = []
        if "--system-prompt-file" in self.args:
            file = Path(self.args[self.args.index("--system-prompt-file") + 1])
            system_prompt = file.read_text()
            modes = [stat.S_IMODE(path.stat().st_mode) for path in (file, file.parent)]
        elif "--system-prompt" in self.args:
            system_prompt = self.args[self.args.index("--system-prompt") + 1]
        timeout = os.environ.get("MCP_TOOL_TIMEOUT")
        started = {"cwd": str(Path.cwd()), "system_prompt": system_prompt, "prompt_modes": modes}
        background = ("CLAUDE_CODE_DISABLE_MCP_TASK_BACKGROUND", "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS")
        started["background"] = [os.environ.get(name) for name in background]
        trusted = (
            "DISABLE_AUTO_COMPACT",
            "CLAUDE_CODE_SDK_READS_SESSION_STATE",
            "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS",
        )
        started["trusted_env"] = {name: os.environ.get(name) for name in trusted}
        self.log("spawn", args=self.args, tool_timeout=timeout, env_names=sorted(os.environ), **started)
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
