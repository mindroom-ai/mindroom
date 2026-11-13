---
icon: lucide/code
---

# Headless Runs

A headless run answers one prompt with a configured MindRoom agent from another Python program, with no Matrix homeserver and no `mindroom run`.
Use it for scripts and CI jobs that hand an agent a task, for evaluation harnesses that compare agents and models on the same tasks, and for embedding an agent in another application.
The agent runs with its configured instructions, model, tools, memory, and subagents, in the calling process and on the caller's event loop.

## Example

```python
import asyncio
from pathlib import Path

from mindroom.headless import run_turn

config = {
    "administrators": ["@me:localhost"],
    "models": {"default": {"provider": "openai", "id": "gpt-6.1-sol"}},
    "memory": "none",
    "defaults": {"allow_shell_without_worker": True},
    "agents": {"coder": {"display_name": "Coder", "tools": ["shell", "file"], "delegate_to": ["coder"]}},
}
result = asyncio.run(
    run_turn(config, agent="coder", prompt="Run the tests and fix what fails.", storage=Path("~/.cache/mindroom-runs"), workdir=Path(".")),
)
print(result.text)
```

## API

`mindroom.headless.run_turn(config, *, agent, prompt, storage, workdir=None, requester_id=None, request_headers=None)` is a coroutine that returns a `HeadlessResult`.

| Argument | Meaning |
|---|---|
| `config` | A mapping with what `config.yaml` holds, validated as MindRoom validates `config.yaml`; tools that read or edit the config, such as `self_config`, use a `config.yaml` in `storage` instead. |
| `agent` | The configured agent that answers. |
| `prompt` | The message it answers. |
| `storage` | The directory where MindRoom keeps the run's state: sessions, saved tool outputs, subagent records, and the agent's workspace. |
| `workdir` | The existing directory where `shell` runs and `file`, `coding`, and `python`'s file functions resolve paths, for the agent and its subagents; without it they work in the agent's workspace under `storage`. |
| `requester_id` | The Matrix user ID the agent answers; it must be in `administrators`, and defaults to the first of them. |
| `request_headers` | A function that takes a `HeadlessRun` and returns the headers that run's model requests carry; see [Request headers](#request-headers). |

Provider API keys come from the process environment, or from a `.env` file in `storage`.
Give headless runs a storage of their own: `mindroom run` with its default password accounts cannot start on a storage a headless run used.
[Budgets](budgets.md) do not count a headless run's spend, so only a `monthly_limit_usd` of `0` switches a priced model to the `fallback_model`.
Each call is a new conversation, so the agent sees no history from earlier calls, and it does not write memories on its own after the turn.

`HeadlessResult` has these fields:

| Field | Meaning |
|---|---|
| `text` | The reply, without tool calls. |
| `completed` | Whether the turn completed. |
| `failed` | Whether the turn failed with `text` as its error reply, such as a provider error. |
| `tool_calls` | How many tool calls the agent itself made in the attempt that ended the turn; its subagents' calls are not counted. |
| `run_metadata` | The run metadata MindRoom stores with a reply, including the model, usage, and cost. |

Cancelling the call, directly or through `asyncio.timeout`, stops the turn, its subagents, and any shell command still running before the cancellation reaches the caller.

`mindroom.headless.validate_config(config, *, agent, storage, workdir=None, requester_id=None)` checks the same things without running a turn and returns the validated config.

## Request headers

`request_headers` adds headers to model requests per run, for LLM gateways and proxies that attribute or limit spend per run, such as LiteLLM, Portkey, or Helicone.
The lead agent and each subagent are separate runs, and a `continue_subagent` follow-up belongs to the run it continues.
MindRoom calls the function with the `HeadlessRun` that sends each model request, compaction summaries included, as it sends that request; it runs on the event loop, so keep it quick and free of blocking calls.
The headers it returns win over headers of the same name that the model's config sets.
Only the agents' own model requests carry them; media downloads for the model, embeddings, the model calls [Mem0](memory.md) makes to store a memory, and requests that tools make do not.
Models on the `google` and `ollama` providers send no request headers; reach a gateway in front of them through an `openai` model with the gateway's `base_url` instead.

```python
from mindroom.headless import HeadlessRun


def request_headers(run: HeadlessRun) -> dict[str, str]:
    return {"x-run-id": f"task-42/{run.subagent_id or 'lead'}", "x-agent": run.agent_name}
```

| `HeadlessRun` field | Meaning |
|---|---|
| `agent_name` | The agent whose run sends the request. |
| `subagent_id` | The subagent's ID; `None` for the lead agent. |
| `parent_subagent_id` | The ID of the subagent that started this one; `None` for the lead agent and the lead's own subagents. |
| `depth` | `0` for the lead agent, `1` for its subagents, and one more for each nested level. |

## What headless runs refuse

`run_turn` and `validate_config` raise `HeadlessConfigError` before the model is called when the config needs a Matrix room or a running MindRoom.
Its `problems` attribute lists every reason, and each names the setting to change.
The checks cover the agent and every agent it can reach through `delegate_to`.

| Refused | Why |
|---|---|
| A team as `agent` | Headless runs answer with one agent. |
| Tools that run in a worker, through `worker_tools` or the sandbox settings | Every tool of a headless run runs in the calling process. |
| `shell` without `defaults.allow_shell_without_worker: true` | Without it, every command the agent runs returns an error. |
| [MCP](mcp.md) tools | A running MindRoom connects to MCP servers. |
| [`knowledge_bases`](knowledge.md), semantic file `memory_search`, and the [knowledge graph](knowledge-graph.md) | A running MindRoom indexes them. |
| Enabled [`plugins`](plugins.md) | A running MindRoom loads them. |
| A requester who is not in `administrators`, or no administrators | Without a room, only administrators pass every shared agent's responder access, which delegation checks. |
| A `storage` holding Matrix account credentials, such as one `mindroom run` set up | Headless runs record accounts without credentials, which would stop that instance from starting. |

Memory runs headless: [Mem0](memory.md) memories are recalled into the turn, and file memory is read as in Matrix; set `memory: none` to run without memory.
As in the [OpenAI-compatible API](openai-api.md#limitations), tools that need a Matrix room, such as `scheduler`, and tool functions that a [tool approval](tool-approval.md) rule gates are hidden from the agent.

## Subagents

An agent's `run_subagent` and `continue_subagent` work as in Matrix, following its `delegate_to` ([Agent Orchestration](tools/agent-orchestration.md)), which may name the agent itself to run copies of it.
Subagents run inside the turn and share its `workdir`, and several `run_subagent` calls in one model response run at the same time.
They run in standard mode; [minimal mode](tools/agent-cli.md) needs `mindroom run`.

## Files

With a `workdir`, the files that `shell`, `file`, and `coding` write land there, and MindRoom's own files stay under `storage`.
Code that `python` runs uses the calling process's current directory, so give it absolute paths.
`file_access: workspace` confines `file` and `coding` to the `workdir` instead of the workspace.
Tool outputs saved to a file, because they are over `defaults.tool_output_auto_save_threshold_bytes` or the call set `mindroom_output_path`, go to `mindroom_tool_outputs/` in the agent's workspace, and the agent gets their absolute path.
`file` and `coding` can read that directory under `file_access: workspace`, but not write it, as long as `storage` is outside the `workdir`.
Other tools that work on files, such as `file_generation`, `visualization`, `gmail` attachments, and `google_drive` downloads, keep the agent's workspace under `storage`, so under `file_access: workspace` they cannot reach files in the `workdir`.
The agent's skills, subagent profiles, and delegation records stay in its workspace too, which its system prompt names by absolute path; while `storage` is outside the `workdir`, `file` and `coding` under `file_access: workspace` cannot reach them, so an agent that writes its own profiles or skills there needs `file_access: unrestricted`, or `shell` or `python` given absolute paths.

All tools run with the calling process's permissions, and `shell` and `python` can reach anything that process can, whatever `file_access` says; see [Security Model](architecture/security-posture.md).
