---
icon: lucide/layout-template
---

# Agent Templates

`mindroom config new-agent` creates an agent's starter files from a built-in template and prints the config block to add under `agents:`.
It never edits `config.yaml` and never overwrites a file that already exists.
It writes into a directory that already has files only when `config.yaml` lists that agent with the template's context files and template directory, so it cannot seed another agent's workspace, and it refuses a team's name.
`mindroom config init` already creates `mind`, a personal assistant with its own workspace; these templates add more agents next to it.

| Template | For | Files go to | Each person gets |
| --- | --- | --- | --- |
| `project` | One shared agent for a project or team, used by several people in one room | The agent's workspace, `<storage>/agents/<name>/workspace/` | The same workspace as everyone else |
| `work` | A private work assistant for each person | A template directory, `<config dir>/templates/<name>/` | A private copy, made the first time they talk to the agent |
| `hermes` | A personal assistant for one person, modeled on the persona and working practices of [Hermes Agent](https://github.com/NousResearch/hermes-agent) | The agent's workspace, `<storage>/agents/<name>/workspace/` | The same workspace, so give it one owner |
| `coding` | A minimal coding agent that works in git repositories | The agent's workspace, `<storage>/agents/<name>/workspace/` | The same workspace and cloned repositories |

## Setup

```bash
mindroom config new-agent apollo --template project
mindroom config new-agent assistant --template work
mindroom config new-agent hermes --template hermes
mindroom config new-agent coder --template coding
```

1. Run the command with the agent name you want; names use letters, digits, and underscores.
2. Paste the printed lines under the `agents:` line of `config.yaml` and change what you need, such as `display_name`, `model`, or `tools`.
3. Run `mindroom config validate`.
4. Talk to the agent: a `project`, `hermes`, or `coding` agent joins the room named after it, which MindRoom creates, and a `work` agent answers direct messages and invites.

On its first conversation a `project` agent works through `BOOTSTRAP.md`: it asks the room for its operators, scope, and sources, records the answers, marks setup as finished in `IDENTITY.md`, and deletes `BOOTSTRAP.md`.
To choose the operators yourself, fill the **Operators** line of the `IDENTITY.md` that `new-agent` created before that first conversation, because otherwise whoever answers in the room decides.
A `work` agent has no checklist: the first time a person talks to it, it introduces itself, asks one question such as what to call them, and fills the rest of that person's `USER.md` through normal work.
A `hermes` agent has no checklist: it fills `USER.md` and `MEMORY.md` as it learns and is told to keep them under about 1,400 and 2,200 characters, saves workflows it works out as [skills](../skills.md#automatic-skill-learning), and searches past threads in its [thread exports](../thread-exports.md).
The `hermes` block leaves out the image generation, text-to-speech, and computer-use tools Hermes Agent has; add the `openai` toolkit for images and speech, the [`desktop`](../tools/desktop.md) tool to operate apps on a person's computer, or [Worker Computer](../tools/worker-computer.md) for a browser they can watch.
A `coding` agent has no checklist either: its `AGENTS.md` sets a way of working, which is to understand the goal, write the least code that works, test first, find the root cause before fixing, and claim success only from commands it ran, and it follows each repository's own instructions on building, testing, and style, but never on secrets, shared history, or pull requests.
Its block has only `shell`, `file`, a deferred `github`, and self-delegation, with no memory, so every request stays small.
To cut its tokens further, switch it to [minimal mode](../tools/agent-cli.md) in a conversation with `!mode <name> minimal`.
In minimal mode the agent reads `AGENTS.md` only when it looks it up, so put rules it must follow on every request in [`minimal_instructions`](../tools/agent-cli.md#prompt-and-minimal_instructions).

The `work` block admits only MindRoom's administrators until you uncomment its `access.users` line, for example `"@*:example.org"` for everyone on your homeserver.
The `project`, `hermes`, and `coding` blocks admit members of the agent's room, the [default](../authorization.md#responder-access) when `access` is omitted.

## Files

A `coding` workspace has only `AGENTS.md`; this table covers the other templates.

| File | Role | When it reaches the agent |
| --- | --- | --- |
| `AGENTS.md` | Operating rules | Every turn, as a context file |
| `SOUL.md` | Purpose and tone | Every turn, as a context file |
| `IDENTITY.md` (not `hermes`) | `project`: name, scope, operators, and source precedence; `work`: name, style, and where it runs | Every turn, as a context file |
| `TOOLS.md` (not `hermes`) | Tool notes, connected accounts, and agreed schedules | Every turn, as a context file |
| `USER.md` (`work` and `hermes`) | The person's profile | Every turn, as a context file |
| `MEMORY.md` | Curated long-term memory | Every turn, through [file memory](../memory.md) |
| `memory/` | `project`: source register, people, decisions, questions, and tracker; `work`: daily notes the agent creates; `hermes`: daily notes from [auto-flush](../memory.md#file-auto-flush-worker) when it is on | When the agent reads or searches them |
| `BOOTSTRAP.md` (`project` only) | First-run checklist | Read while setup is unfinished |
| `skills/` | `project`: `status-report` and `meeting-notes-to-updates`; `work`: `meeting-prep` and `weekly-review`; `hermes`: only the ones it learns | Name and description every turn, full text when used ([Skills](../skills.md)) |

## Updating and Rerunning

Once the agent's entry is in `config.yaml`, running the command again restores files that are missing and keeps every existing file.
Once a `project` agent's setup is marked finished, a restored `BOOTSTRAP.md` no longer starts onboarding again.
For `work`, edits to `templates/<name>/` reach existing people only as new files, because MindRoom never overwrites a person's copy; see [Private Instances](agents.md#private-instances).

## Notes

- A `project`, `hermes`, or `coding` agent has one workspace, so its direct chats write to the same files as its room.
- The `project`, `hermes`, and `coding` commands must run where the storage root lives, because they write the workspace directly.
- The `coding` block's `worker_tools: [shell, file]` line is commented out because, without a worker backend, listing tools there makes their calls fail; uncomment it once [dedicated workers](../deployment/sandbox-proxy.md#worker-routing) are set up.

> [!WARNING]
> **`shell` and `python` are not confined to the workspace.**
> Unless they run in [dedicated workers](../deployment/sandbox-proxy.md#worker-scopes), anyone the agent admits can run code as MindRoom, and for `work` that includes reading every person's copy.
> Remove them from the block, or run them in dedicated Docker or Kubernetes workers through [`worker_tools`](../deployment/sandbox-proxy.md#worker-routing), before admitting people you do not fully trust.
> The `config init` starter sets `defaults.worker_tools: []`, which keeps every tool out of workers even when a worker backend is configured.
