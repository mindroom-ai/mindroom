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

## Setup

```bash
mindroom config new-agent apollo --template project
mindroom config new-agent assistant --template work
mindroom config new-agent hermes --template hermes
```

1. Run the command with the agent name you want; names use letters, digits, and underscores.
2. Paste the printed lines under the `agents:` line of `config.yaml` and change what you need, such as `display_name`, `model`, or `tools`.
3. Run `mindroom config validate`.
4. Talk to the agent: a `project` or `hermes` agent joins the room named after it, which MindRoom creates, and a `work` agent answers direct messages and invites.

On its first conversation a `project` agent works through `BOOTSTRAP.md`: it asks the room for its operators, scope, and sources, records the answers, marks setup as finished in `IDENTITY.md`, and deletes `BOOTSTRAP.md`.
To choose the operators yourself, fill the **Operators** line of the `IDENTITY.md` that `new-agent` created before that first conversation, because otherwise whoever answers in the room decides.
A `work` agent has no checklist: the first time a person talks to it, it introduces itself, asks one question such as what to call them, and fills the rest of that person's `USER.md` through normal work.
A `hermes` agent has no checklist: it fills `USER.md` and `MEMORY.md` as it learns and is told to keep them under about 1,400 and 2,200 characters, saves workflows it works out as [skills](https://docs.mindroom.chat/skills/#automatic-skill-learning), and searches past threads in its [thread exports](https://docs.mindroom.chat/thread-exports/).
Its block leaves out the image generation, text-to-speech, and computer-use tools Hermes Agent has; add the `openai` toolkit for images and speech, the [`desktop`](https://docs.mindroom.chat/tools/desktop/) tool to operate apps on a person's computer, or [Worker Computer](https://docs.mindroom.chat/tools/worker-computer/) for a browser they can watch.

The `work` block admits only MindRoom's administrators until you uncomment its `access.users` line, for example `"@*:example.org"` for everyone on your homeserver.
The `project` and `hermes` blocks admit members of the agent's room, the [default](https://docs.mindroom.chat/authorization/#responder-access) when `access` is omitted.

## Files

| File | Role | When it reaches the agent |
| --- | --- | --- |
| `AGENTS.md` | Operating rules | Every turn, as a context file |
| `SOUL.md` | Purpose and tone | Every turn, as a context file |
| `IDENTITY.md` (not `hermes`) | `project`: name, scope, operators, and source precedence; `work`: name, style, and where it runs | Every turn, as a context file |
| `TOOLS.md` (not `hermes`) | Tool notes, connected accounts, and agreed schedules | Every turn, as a context file |
| `USER.md` (`work` and `hermes`) | The person's profile | Every turn, as a context file |
| `MEMORY.md` | Curated long-term memory | Every turn, through [file memory](https://docs.mindroom.chat/memory/) |
| `memory/` | `project`: source register, people, decisions, questions, and tracker; `work`: daily notes the agent creates; `hermes`: daily notes from [auto-flush](https://docs.mindroom.chat/memory/#file-auto-flush-worker) when it is on | When the agent reads or searches them |
| `BOOTSTRAP.md` (`project` only) | First-run checklist | Read while setup is unfinished |
| `skills/` | `project`: `status-report` and `meeting-notes-to-updates`; `work`: `meeting-prep` and `weekly-review`; `hermes`: only the ones it learns | Name and description every turn, full text when used ([Skills](https://docs.mindroom.chat/skills/)) |

## Updating and Rerunning

Once the agent's entry is in `config.yaml`, running the command again restores files that are missing and keeps every existing file.
Once a `project` agent's setup is marked finished, a restored `BOOTSTRAP.md` no longer starts onboarding again.
For `work`, edits to `templates/<name>/` reach existing people only as new files, because MindRoom never overwrites a person's copy; see [Private Instances](https://docs.mindroom.chat/configuration/agents/#private-instances).

## Notes

- A `project` or `hermes` agent has one workspace, so its direct chats write to the same files as its room.
- The `project` and `hermes` commands must run where the storage root lives, because they write the workspace directly.

> [!WARNING]
> **`shell` and `python` are not confined to the workspace.**
> Unless they run in [dedicated workers](https://docs.mindroom.chat/deployment/sandbox-proxy/#worker-scopes), anyone the agent admits can run code as MindRoom, and for `work` that includes reading every person's copy.
> Remove them from the block, or run them in dedicated Docker or Kubernetes workers through [`worker_tools`](https://docs.mindroom.chat/deployment/sandbox-proxy/#worker-routing), before admitting people you do not fully trust.
> The `config init` starter sets `defaults.worker_tools: []`, which keeps every tool out of workers even when a worker backend is configured.
