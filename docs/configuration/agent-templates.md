---
icon: lucide/layout-template
---

# Agent Templates

`mindroom config new-agent` creates an agent's starter files from a built-in template and prints the config block to add under `agents:`.
It never edits `config.yaml` and never overwrites a file that already exists.
It writes into a directory that already has files only when `config.yaml` lists that agent with the template's context files and template directory, so it cannot seed another agent's workspace, and it refuses a team's name.
`mindroom config init` already creates `mind`, a personal assistant with its own workspace; these templates add more agents next to it, including more copies of Mind.

| Template | For | Files go to | Each person gets |
| --- | --- | --- | --- |
| `mind` | The `config init` starter's personal assistant, for example a second one for another person | The agent's workspace, `<storage>/agents/<name>/workspace/` | The same workspace, so give it one owner |
| `hermes` | A personal assistant for one person, modeled on the persona and working practices of [Hermes Agent](https://github.com/NousResearch/hermes-agent) | The agent's workspace, `<storage>/agents/<name>/workspace/` | The same workspace, so give it one owner |
| `coding` | A minimal coding agent that works in git repositories | The agent's workspace, `<storage>/agents/<name>/workspace/` | The same workspace and cloned repositories |
| `legal` | A private helper for each person's contracts, leases, letters, and legal deadlines; legal information, not advice | A template directory, `<config dir>/templates/<name>/` | A private copy, made the first time they talk to the agent |
| `finance` | A private helper for each person's budget, spending, savings goals, and bills; it never moves money | A template directory, `<config dir>/templates/<name>/` | A private copy, made the first time they talk to the agent |

## Setup

```bash
mindroom config new-agent ada --template mind
mindroom config new-agent hermes --template hermes
mindroom config new-agent coder --template coding
mindroom config new-agent counsel --template legal
mindroom config new-agent money --template finance
```

1. Run the command with the agent name you want; names use letters, digits, and underscores.
2. Paste the printed lines under the `agents:` line of `config.yaml` and change what you need, such as `display_name`, `model`, or `tools`.
3. Run `mindroom config validate`.
4. Talk to the agent: a `mind`, `hermes`, or `coding` agent joins the room named after it, which MindRoom creates, and a `legal` or `finance` agent answers direct messages and invites.

A `mind` agent is a copy of the starter's Mind, with the same tools, instructions, and workspace files, but it joins and admits the members of the room named after it instead of `personal`, and its `TOOLS.md` names the active config file.
To restore files deleted from the starter's own Mind, run `mindroom config new-agent mind --template mind`; it adds only the missing files, and its printed block, which names a `mind` room, does not need pasting.
A `hermes` agent has no checklist: it fills `USER.md` and `MEMORY.md` as it learns and is told to keep them under about 1,400 and 2,200 characters, saves workflows it works out as [skills](../skills.md#automatic-skill-learning), and searches past threads in its [thread exports](../thread-exports.md).
The `hermes` block leaves out the image generation, text-to-speech, and computer-use tools Hermes Agent has; add the `openai` toolkit for images and speech, the [`desktop`](../tools/desktop.md) tool to operate apps on a person's computer, or [Worker Computer](../tools/worker-computer.md) for a browser they can watch.
A `coding` agent has no checklist either: its `AGENTS.md` sets a way of working, which is to understand the goal, write the least code that works, test first, find the root cause before fixing, and claim success only from commands it ran, and it follows each repository's own instructions on building, testing, and style, but never on secrets, shared history, or pull requests.
Its block has only `shell`, `file`, a deferred `github`, and self-delegation, with no memory, so every request stays small.
To cut its tokens further, switch it to [minimal mode](../tools/agent-cli.md) in a conversation with `!mode <name> minimal`.
In minimal mode the agent reads `AGENTS.md` only when it looks it up, so put rules it must follow on every request in [`minimal_instructions`](../tools/agent-cli.md#prompt-and-minimal_instructions).
A `legal` or `finance` agent has no checklist either: it helps with a person's first question before anything else, introduces itself once and only in a direct chat, and then asks where they live, because the answers depend on local law, currency, and tax.
Both say they are not a lawyer or financial adviser, and say when a professional is needed in their first reply rather than at the end.
A `legal` agent quotes the clause it explains, counts deadlines out loud, and keeps one file per matter in `memory/matters/`; a `finance` agent computes every figure with the `calculator` tool and shows its working.
Neither has `shell`, `python`, or the deployment's default tools, and both pin `file_access: workspace`, so their own tools cannot read another person's copy.
Other agents in the same deployment that run `shell` or `python` outside [dedicated workers](../deployment/sandbox-proxy.md#worker-scopes), such as a `mind`, `hermes`, or `coding` agent, still can, so run those in dedicated workers when private copies matter.

The `legal` and `finance` blocks admit only MindRoom's administrators until you uncomment their `access.users` line, for example `"@*:example.org"` for everyone on your homeserver.
The `mind` block admits only members of the agent's room, and the `hermes` and `coding` blocks admit members of the agent's room, the [default](../authorization.md#responder-access) when `access` is omitted.

## Files

A `coding` workspace has only `AGENTS.md`; this table covers the other templates.

| File | Role | When it reaches the agent |
| --- | --- | --- |
| `AGENTS.md` | Operating rules | Every turn, as a context file |
| `SOUL.md` | Purpose and tone | Every turn, as a context file |
| `USER.md` | The person's profile | Every turn, as a context file |
| `IDENTITY.md`, `TOOLS.md`, `HEARTBEAT.md` (`mind` only) | Who the agent is, tool notes with the active config file, and a checklist for requested or scheduled checks | Every turn, as context files |
| `MEMORY.md` | Curated long-term memory | Every turn, through [file memory](../memory.md) |
| `memory/` | `mind`: daily notes, in the folder `new-agent` creates; `hermes`: daily notes from [auto-flush](../memory.md#file-auto-flush-worker) when it is on; `legal`: one file per matter and daily notes; `finance`: daily notes and past monthly reviews | When the agent reads or searches them |
| `skills/` | `mind` and `hermes`: only the ones they learn; `legal`: `contract-review` and `deadline-check`; `finance`: `monthly-review` and `savings-goal` | Name and description every turn, full text when used ([Skills](../skills.md)) |

## Updating and Rerunning

Once the agent's entry is in `config.yaml`, running the command again restores files that are missing and keeps every existing file.
For `legal` and `finance`, edits to `templates/<name>/` reach existing people only as new files, because MindRoom never overwrites a person's copy; see [Private Instances](agents.md#private-instances).

## Notes

- A `mind`, `hermes`, or `coding` agent has one workspace, so its direct chats write to the same files as its room.
- The `mind`, `hermes`, and `coding` commands must run on the machine that holds the storage root, because they write the workspace directly.
- With [auto-flush](../memory.md#file-auto-flush-worker) on, as in the `config init` starter, MindRoom also writes daily notes from conversations into `memory/YYYY-MM-DD.md` for every template with file memory, including each person's private copy; turn it off in `memory.auto_flush` if conversations should not be kept there.
- The `coding` block's `worker_tools: [shell, file]` line is commented out because, without a worker backend, listing tools there makes their calls fail; uncomment it once [dedicated workers](../deployment/sandbox-proxy.md#worker-routing) are set up.

> [!WARNING]
> **`shell` and `python` are not confined to the workspace.**
> Unless they run in [dedicated workers](../deployment/sandbox-proxy.md#worker-scopes), anyone the agent admits can run code as MindRoom, and that includes reading every person's private `legal` and `finance` copy.
> A `coding` agent also runs code from the repositories it clones, such as their tests, as MindRoom, so until its `shell` runs in dedicated workers, point it only at repositories you trust.
> Remove them from the block, or run them in dedicated Docker or Kubernetes workers through [`worker_tools`](../deployment/sandbox-proxy.md#worker-routing), before admitting people you do not fully trust.
> The `config init` starter sets `defaults.worker_tools: []`, which keeps every tool out of workers even when a worker backend is configured.
