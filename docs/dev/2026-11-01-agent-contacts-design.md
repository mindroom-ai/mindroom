# Agent Contacts

## Goal

Let a person's agent talk to another person's agent only after both people agreed, without anyone editing `config.yaml`.
Today an agent reaches another agent when the target's `access` admits the requester, and for personal agents that is a template-wide setting that admits everyone or nobody.
Success means Alice can ask her agent to reach Bob's agent, Bob sees the request in a new thread of his personal room and accepts or declines it there with his agent, Bob can attach rules for what his agent may share with Alice's side, and every message either agent sends to the other passes a policy that can send, rewrite, return with feedback, or block it.

## Understanding

What the user asked for:

- Reachability between agents is decided at runtime through a tool call and an explicit approval card, not through `access` or `delegate_to`.
- The target owner is asked in a new thread in the room where they talk to their agent, and their agent explains the request there.
- Rules (guardrails) can be set per contact, on top of defaults, and are stored.
- A contact can be between two agents or between an agent and a person.
- A pluggable hook decides whether a message may cross to the other side; it can block, rewrite, or send the message back to the agent with feedback.
  The judgment layer powers a built-in LLM-as-judge implementation of that hook.
- Agents talk through Matrix.

Assumptions:

- "The room of my colleague's agent" is the colleague's personal room, served by the agent `personal_rooms.agent` names for them.
- Both people and both agents live on the same MindRoom deployment.
- The first version covers personal agents (generated from `personal_agents`) and people; created and authored agents stay on `access` for now.

## Approaches Considered

### Chosen: contacts that act with the owner's authority in a shared contact room

A contact is a stored, mutually approved relationship between two parties.
Accepting it creates a contact room holding both parties and both owners.
In that room each agent party runs as its own owner, under that owner's rules, and every message it posts passes the contact policy.

### Rejected: dynamic `access` grants

Adding the requester to the target's `access.users` at runtime is the smallest change, but the mentioned agent then acts for the requester (see `docs/authorization.md#agents-mentioning-other-agents`).
Bob's agent would run with Alice's requester-scoped credentials and private state, and Alice would approve tool calls Bob's agent makes with Bob's shared credentials.
Rules written by Bob would constrain an agent working for Alice, so they would have no owner whose interests they protect.

### Rejected: synchronous cross-owner delegation

An `ask_contact` tool running the target like `run_subagent` keeps the exchange out of Matrix, so neither owner can watch or step in, and the target owner has no place to answer an approval mid-run.

## Decisions

### Parties, owners, and consent

A contact needs consent from both owners: the requesting owner approves the outgoing request card, and the target owner approves the acceptance card.
Only a party's owner may request, accept, change rules for, or remove that party's contacts, because a contact speaks for the owner.
The contacts tools therefore refuse every other requester, administrators included, even though `access` admits administrators to every generated agent.
`contacts` is accepted on any agent's config, but only a personal agent can be a party, and that is enforced when a tool runs, with an error naming the reason, so validation stays simple and the tool schema goldens can be built from an authored agent.

### Where a request is asked and accepted

The request notice is posted, and later accepted or declined, by the agent of the target owner's personal room, not by the target agent.
A target agent from another template is not a member of that room, while the personal-room agent is the one the owner already talks to there.
That agent must have `contacts` itself, because it needs the contacts tools to accept; a request whose target owner's personal-room agent lacks them is refused before anything is stored.
A person target is asked the same way, in their own personal room, so a person needs a personal room on this deployment to be contacted.

### The contact room

Accepting creates a room that is invite-only like a personal room, with shared history so a member who joins later reads the whole conversation.
It holds both parties and both owners, and leaves the router out, because the router would route messages there and contact turns never need approval cards.
The target agent creates it, or the requesting agent when the target is a person, because the creator stays in the room and must therefore be a party.
The pending contact names its room before anyone is invited, because a room the store does not know yet would run an owner's early message as a normal turn, and a setup that crashes would leave it unrecognized.
A retried acceptance keeps the room of the attempt it replaces as a removed contact's room, because an agent whose leave failed may still be in it and would otherwise run ordinary turns there.
An acceptance that abandons its room withdraws the owners' invitations before its agents leave, because only a member with kick power can withdraw them, and a pending invitation would lead an owner to a dead room named like the real one.
Whenever the creating agent leaves a removed contact's room, it first withdraws the invitations of owners who never joined, because once its agents are gone the room can have no joined member, and Synapse then refuses the join with `NoKnownServersError`; owners who joined keep the room and its history.
An owner who joins between the agent's last read of the member list and its kick is still removed, because the kick that withdraws an invitation also removes a member.
Agent parties keep the room across restarts through cleanup exclusions read from the contacts store, which stays the single source of truth.
The invited-rooms store was rejected for this, because it is not persisted for agents whose `accept_invites` is false.
Contact rooms are also left out of the rooms an agent keeps for accepted invites, because an agent that accepts invites records the contact room's invite like any other, and would otherwise rejoin a removed contact's room at its next start.
A removed contact's room takes no more turns or messages from its agents, because a leave can fail and a running or queued turn can still try to post there.
An agent whose `contacts` is removed treats each of its active contacts as removed and leaves their rooms, because in a room it no longer reads as a contact room its owner's and the other side's messages would start normal turns with its full tools, and its sends there would skip the policy.
When it leaves those rooms it also marks those contacts removed in the store, and a pending request that needs it cannot be accepted while it lacks `contacts`.
Deriving that status only when read was rejected, because restoring `contacts` would revive contacts whose rooms the agent had left: the other side would keep writing there, a new request would find the contact active, and nobody would answer.

### Contact turns run as the agent's owner

Every turn of an agent party in its own contact room is a contact turn, and its requester is the agent's owner.
Only the counterparty and the agent's owner may start one: the counterparty is the other party, plus its owner when the other party is an agent.
Without the owner as requester, requester-scoped credentials and private state would belong to the other side.
The requester is decided once at ingress, where the relayed original sender is promoted today, because every later copy (coalescing, turn records, budgets, the tool context) derives from it; the model still sees the message as written by its physical sender, so it never mistakes the other side for its owner.
Because the owner is the requester, ordinary `access` admits the turn, so `authorization.py` needs no contact clause.

Ingress also refuses every other sender in a contact room itself, because ordinary `access` would admit an administrator there.
The agent's own messages never start a contact turn, so its earlier messages stay recognizable as its own.
Messages from the other side and from the owner both run as the owner, so coalescing and the newer-message check also compare the speaker, which keeps one from absorbing or superseding the other.
No edit regenerates a contact turn, because its reply never shows.
Ingress reads the contacts store only for personal agents, which keeps a corrupt store from affecting other agents and saves a file check per event for them.

### Contact detection: once per turn, and from the store for tools

A response asks the contacts store once, when it starts, whether its room is or was this agent's contact room, and everything it shows and offers follows that answer: its progress, final reply, and error notices, its tools and instructions, and its memory.
A response whose contact is already removed runs nothing, because ingress admitted it before the removal.
Asking again at each seam was rejected, because a contact removed or a config reloaded while the turn runs would let a later check show the turn's private final reply in a room where the other owner still sits.
Ingress, the memory flush, knowledge graph ingestion, and exploration ask the store themselves, and tools ask it at each call whether a room is or was this agent's contact room, so a turn keeps its restricted destinations after a removal and no turn writes into a removed contact's room.
Carrying a flag from ingress was rejected, because tool calls from normal turns into a contact room and the agent's own sends there have no ingress facts, and two sources could disagree.

### Who answers in a contact room

An owner's unmentioned message is answered only by that owner's agent, because each agent of a contact runs a message there as its own owner, so access cannot choose between them: both agents would otherwise answer every message an owner writes, or neither would when access lets each owner use both agents.
Agents never form a team in a room that is or was a contact room, because a team turn would show an unrestricted reply to the other side; each mentioned agent answers alone as its own owner's contact turn.
In its own active contact room an agent can always name the other agent as `recipient`, because that agent's access never admits this agent's owner, the requester of every turn there, while its ingress admits the relay and runs it as its own owner; recipient discovery everywhere else still follows access.
A first live run found the two agents unable to name each other before this; a re-run had them wake each other through `recipient`, each turn running as its own owner, until `defaults.max_consecutive_agent_replies` stopped the exchange and a person's message reset the count.

### Contact turns answer only through `matrix_message`

The agent answers the other side by calling `matrix_message` into the contact room, which is the single place the contact policy runs.
This keeps one synchronous checkpoint: a revise comes back to the agent as the tool result in the same run, so no model-loop stage, delta buffering, or team-specific path is needed.
A gate on the final reply was rejected because the model loop yields text deltas before it knows an answer is final, the collected-delivery path prefers those deltas over the run's content, and team leaders build their loop hooks separately, so withholding and regenerating a reply would touch every reply path.

A contact turn's room refuses every other send from that turn, its tools and their middleware included, in the delivery layer every Matrix send passes, and `matrix_message` lifts that refusal only around a send its policy approved.
Blocking each path separately was rejected, because three review rounds each found another path that posted into the contact room without the policy, the last one the browser's notice of the worker computer.
Plugin hooks on an inbound event run before any contact turn starts, always inside the agent's admitted decision to act on that event, so that decision guards the room when it is or was the agent's contact room.
Guarding at the journal callback was rejected, because those hooks run in coalescing tasks that keep the context of whatever created them.
Contact turns keep their quiet delivery, so their replies never try a placeholder, stream, or error notice that the room would refuse.
Their tools also run inline, never as background jobs, because every running job is published as a state event in its room, which the room does not refuse and the other owner's client shows with its tool name.

### Contact turns get a restricted surface

The trigger of a contact turn is text the owner may not have written, so a contact turn offers less than a normal turn:

- Only the contact's tools for this side (see [Rules and tools per contact](#rules-and-tools-per-contact)) plus `matrix_message`, minus every function that a `tool_approval` rule or the tool itself would gate, because an approval card in the contact room would show the owner's tool arguments to the other side.
- No skills, knowledge bases, or interactive questions, because none of them can be answered or shown in a turn whose reply never shows.
- A contact's tools may not include Matrix room tools (those that require room context, such as `scheduler`, `matrix_api`, or `thread_move`), because they post into rooms without passing the policy, nor MindRoom control tools (`contacts`, `delegate`, `agent_manager`, `self_config`, `config_manager`, `dynamic_workflow`, `claude_agent`, `skill_manage`, `dynamic_tools`), because they act with the owner's authority beyond the conversation.
- `matrix_message` acts in the contact room, and in the owner's personal room with this agent only sends a plain message to its timeline or a new thread, so the agent can report back to its owner and nowhere else.
  Naming a `recipient` there would start a normal turn with the owner's full authority, reading there would expose the owner's private room to drafts written for the other side, an `idempotency_key` there would share the owner's own keyed sends, so a matching key could deliver what an owner's turn prepared instead, and an existing thread would carry text the other side shaped into one of the owner's own conversations.
- No attachments or collapsible sections into a contact room, because the judge cannot inspect them.
- Other tools that post into an explicitly named room refuse contact rooms, so text reaches a contact room only through `matrix_message`.

The restriction narrows the agent's own build to a tool list and keeps its identity, instructions, preloaded files, and memory recall, because a contact turn is the same agent acting for its owner with fewer tools.
Reusing the authored-subagent persona was rejected, because a persona replaces the agent's identity with its own prompt.
Its prompt names the other side by ID and owner only, like the judge's question, because the other side chooses its agent's display name.

### Contact turns stay out of memory

The other side drives contact turns, so MindRoom must not carry text it wrote into the owner's normal turns, and a skill review would post notices that bypass the policy.
A tool the owner approves for a contact still reaches what it reaches: a live run with `file` approved let a contact turn overwrite `MEMORY.md`.
A write guard for memory files was rejected, because the security trust model treats an approved tool as the owner's choice and in-process path filtering confines nothing for tools that run code; the user docs warn about it instead.
Rooms that were contact rooms stay excluded after the contact ends, even once the agent loses `contacts`.

### Rules and tools per contact

The agent's configured `contacts.rules` always apply, so an administrator's baseline (for example "never share credentials") cannot be relaxed by an owner; an owner can only add rules.

Tools differ from rules because the owner already has every tool of the agent in normal turns, so choosing which of them a contact may drive is the owner's risk to take.
A tool the agent later loses is left out of each contact turn's tools while the owner's stored choice stays unchanged, so it comes back if the agent regains it.
The agent may propose tools in the call, including ones the other side asked for, so the card is where the owner decides.

Config validation refuses only the control tools in `contacts.tools`, because config must not import tool metadata; Matrix room tools there are accepted and never offered, and the tools refuse them when an owner names them.

### Contact policy: hooks, then the judge

Every `matrix_message` send, edit, or reaction by an agent party into its contact room, from a contact turn or from a normal turn elsewhere, passes one policy pipeline before it is sent:

1. A send is denied once the target conversation ends with `defaults.max_consecutive_agent_replies` consecutive agent messages, because agent conversations started with `matrix_message` skip the limit that mentions in replies obey, and two agents acting for their owners could otherwise talk forever.
   Edits and reactions add no message, so they do not count.
2. Plugin hooks on the new `contact:before_send` event run in priority order.
   A text the hooks leave blank is denied before the judge, because without a judge nothing else would stop an empty message.
3. When the agent's `contacts.judgment` is set, the judgment layer answers whether the final text follows the effective rules, given the rules and the text.
   A no becomes a revise whose feedback quotes the rules.
   The judge sees only the draft, not the conversation, which is what decides whether the draft discloses too much and keeps the judgment request small.
   Its question names the agent, the owner it writes for, and the recipient, and says that "I" and "my" in the rules mean the owner, because owners write rules about themselves while their agent writes about them in the third person.
   It names the recipient by ID and owner only, because the question is the rubric the judge trusts and the other side chooses its agent's display name.
   Without those identities a live run revised "Alice is free." and passed "I'm still free Thursday afternoon." under the same rules.

Reactions pass the policy like edits, because a reaction key is free text.
A rewritten message's tool result carries the text that was actually sent, so the agent knows what the other side read.
A send into a contact room cannot carry an `idempotency_key`, because a retry delivers the payload stored when the key was first used, which the contact's rules may no longer allow.
Running the policy again on a stored payload was rejected, because a rewrite or a revise would deliver something other than what the key stands for.

The pipeline fails closed because the rules are the owner's explicit condition for the contact, unlike `tool:before_call`, which fails open.
`contact:before_send` defaults to a 10 second timeout, because a useful hook may call a model.
Its context carries `message_received_depth` like tool contexts, so a hook that sends messages keeps the same loop prevention.

Secrets in a draft are detected only when a judge is set: the judgment layer refuses a request whose text its redactor would change, which denies the message.
A separate secret scan without a judge was not added, because the judgment layer's check is the detector the rest of MindRoom uses and a second one would drift from it.

### Requests and removal

A repeated request whose rules or tools differ from those stored for a pending or active contact is refused in favor of `update_contact`, so a request never claims values it did not store, while one with the same values is a repeat, so retrying a request whose notice failed posts it.
Only the target owner may remove a declined contact, so the requester cannot undo a decline by removing it.
`remove_contact` has no approval card, because it only takes access away.

### Storage

Workers never read or write the contacts store, because the `static_runner` sidecar mounts `agents/` and records the primary treats as authority live in the primary-only `tracking/` directory.
Contacts are never deleted, only given a status, so a declined contact keeps blocking new requests until its target owner removes it.

### Configuration

A `contacts` object on an agent enables contacts for it and adds the `contacts` and `matrix_message` tools automatically, the way a non-empty `delegate_to` adds `delegate`, because a contact answers only through `matrix_message`.

### Code placement

`orchestrator.py` does not change, and `bot.py` only passes the agent name to the ingress validator, because the tools reach other agents' clients through the orchestrator runtime the tool context already carries.

## Security Posture

[Security Posture](../architecture/security-posture.md) records what a contact exposes to the other side.

## Accepted Limitations

- Only personal agents and people can be parties.
- The judge sees the draft text only, and secrets are caught only when a judge is set.
- With `party="me"`, no agent of the requesting person is a party, so nothing tells them of a decline, and their invitation to the contact room is the only sure sign of an acceptance.
- An agent party that fails to call `matrix_message` leaves the other side without an answer; its final reply stays private.
- A minimal-mode agent answers in contact turns only when the contact's tools include `shell`, because minimal mode reaches every tool through the shell.
- Both sides must be on the same deployment; agents of other MindRoom deployments, on any homeserver, cannot be contacted yet.
- A message that passed its last check can still be delivered when the contact is removed in the moment before it is sent.
- A message that passed its last check can still go one past `defaults.max_consecutive_agent_replies` when another agent message lands in the moment before it is sent.
- An agent party that restarts or reloads in the under-a-second window between joining the contact room and the acceptance making the contact active leaves the room, because an agent keeps only active contacts' rooms, so the contact goes active without it.
- An acceptance cut short by a restart leaves the owners' invitations to its room pending, because only the acceptance in progress knows whom it invited.
- When a config reload removes `contacts` from one agent, the other agent of each of its contacts stays joined in the now-removed contact room until its own restart, starting and sending nothing there.
- The contact room's refusal covers messages, edits, reactions, and files, but not state events, redactions, typing notices, or live stream frames; a contact turn sends none of them into its room, and only a plugin hook could.
- An agent that is not a party but is invited into a contact room by hand runs ordinary turns there, which the other side can read.
- Contact rooms set no power levels of their own, so the creating agent is their only administrator and every other member has the defaults of any room MindRoom creates.

## Open Questions

- Two live runs with two personal agents on 2026-11-01 passed every scenario once the `recipient` fix landed; minimal mode, worker-routed tools, and a sustained runaway past the consecutive-message limit, which the models would not produce in three attempts, have not run live.
