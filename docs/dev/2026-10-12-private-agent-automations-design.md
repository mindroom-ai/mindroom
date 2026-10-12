# Automations for Private Agents

## Goal

Let requester-private agents run `prompt_curation`, `dreaming`, and plugin automations, so a private agent such as a per-user `mind` keeps each requester's own memory and prompt files in shape.
Before this change config validation refused automations on private agents, because the runner had no requester to run them as and could only maintain one shared workspace per agent.
Success means each requester's private instance gets its own scheduled checks, prompts, and follow-up steps, the prompts and replies are visible only to that requester, and nothing one requester's run reads or writes reaches another requester's instance.

## Constraints

- A private agent's workspace, file memory, and knowledge resolve from the requester identity of the turn, so a prompt can only maintain an instance when its turn runs as that instance's requester.
- An automation's reply quotes the private files it works on, so it must be posted where only the requester and the agent can read it.
- Worker code can write directories and owner records under `private_instances/`, so the list of requesters must come from the copy of each owner record the primary keeps below `tracking/`, as the thread exporter already does.
- Requester authorization still applies to unattended work done on a requester's behalf.

## Decisions

### Run once per recorded requester

The runner enumerates a private agent's instances with `private_instances_for_agent` and keeps a schedule, a one-chain-at-a-time lock, and automation state per requester.
Instances whose primary owner record is missing, which includes instances from before the record moved below `tracking/`, get no automations until their requester's next turn writes it, matching thread exports.
Shared agents keep one schedule and lock per agent and their existing state directory.

### Post in the requester's personal room

Each prompt and notice for a private instance goes to that requester's personal room with the agent, read from the primary-only personal-room record.
Personal rooms are the only rooms MindRoom keeps private to one requester and the agent: it removes anyone else invited into rooms it created.
MindRoom never removes anyone from an adopted personal room, whose attested roster can include other people, so each prompt is posted only while the room's joined members are the requester and MindRoom's agents.
A private agent therefore needs `personal_rooms.agent` to name it before it may list or inherit automations, and its entries may not set `room`.

Rejected alternatives:

- **The configured `room`, as for shared agents.** Every member of that room would see each requester's private memory in the replies.
- **Detecting a direct-message room per requester.** MindRoom has no primitive that keeps such a room private, and its membership can change after detection.
- **The room where the requester last spoke to the agent.** It can be shared with other people, and the choice changes from day to day.

### Run as the requester

The prompt carries the requester as its trusted original sender, the same way a personal-room welcome dispatch does, so the turn resolves the requester's private instance, applies their access policy and model budget, and records usage for them.
Before posting, the runner skips a requester the agent may no longer answer in that room, so a revoked requester gets no prompt that would only time out.
The check and its steps resolve the instance with the same execution identity the turn builds, so both always act on the same workspace.

### Per-instance state

A private instance's automation state lives in the primary records directory of its state root (`tracking/private_instances/<scope>/<agent>/automations/`), which no worker mounts and which is keyed by the instance's canonical path rather than by a name the runner invents.
Shared agents keep `tracking/automations/<agent>/`, so existing dreaming progress stays valid.

### Built-in automations on a private instance

`prompt_curation` measures `MEMORY.md` and `private.context_files` in the requester's private root.
An agent-level `context_files` entry of a private agent loads from the shared agent workspace that every requester reads, so no single requester's run condenses it.
`dreaming` reads the conversations the thread exporter wrote into that private workspace, protects `private.context_files` from proposals, and refreshes memory search for the requester's instance.

## Accepted Limitations

- Only the agent named by `personal_rooms.agent` can run automations as a private agent, because there is one personal-rooms agent per installation.
- A requester who never got a personal room, or whose personal-room record cannot be read, is skipped.
- A requester who left their personal room still gets prompts there, because the prompts maintain files the requester's other conversations with the agent use.
- Access revoked while a chain is running is enforced by ingress on the next prompt, so that step times out after an hour instead of being skipped.
