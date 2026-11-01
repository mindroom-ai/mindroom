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

A party is either an agent, identified by its agent name, or a person, identified by their canonical Matrix ID after bridge-alias resolution.
An agent party's owner is the `user_id` of its personal-agent roster entry; a person party owns itself.
A contact needs consent from both owners: the requesting owner approves the outgoing request card, and the target owner approves the acceptance card.
Only a party's owner may request, accept, change rules for, or remove that party's contacts, because a contact speaks for the owner.
The contacts tools therefore refuse every other requester, administrators included, even though `access` admits administrators to every generated agent.
`contacts` is accepted on any agent's config, but only a personal agent can be a party, and that is enforced when a tool runs, with an error naming the reason, so validation stays simple and the tool schema goldens can be built from an authored agent.

### Where a request is asked and accepted

The request notice is posted, and later accepted, by the agent of the target owner's personal room, not by the target agent.
A target agent from another template is not a member of that room, while the personal-room agent is the one the owner already talks to there.
That agent must have `contacts` itself, because it needs the contacts tools to accept; a request whose target owner's personal-room agent lacks them is refused before anything is stored.
A person target is asked the same way, in their own personal room, so a person needs a personal room on this deployment to be contacted.

### The contact room

Accepting creates a room that is invite-only like a personal room, with shared history so a member who joins later reads the whole conversation.
It holds both parties and both owners, and leaves the router out, because the router would route messages there and contact turns never need approval cards.
The target agent creates it, or the requesting agent when the target is a person, because the creator stays in the room and must therefore be a party.
A room that cannot be fully set up is left again and the request stays pending, so accepting again retries.
Agent parties keep the room across restarts through cleanup exclusions read from the contacts store, which stays the single source of truth.
The invited-rooms store was rejected for this, because it is not persisted for agents whose `accept_invites` is false.
Contact rooms are also left out of the rooms an agent keeps for accepted invites, because an agent that accepts invites records the contact room's invite, which arrives before the contact is stored, and would otherwise rejoin a removed contact's room at its next start.
Removing a contact makes its agents leave; the owners keep the room and its history.
A removed contact's room takes no more turns or messages from its agents, because a leave can fail and a running or queued turn can still try to post there.

### Contact turns run as the agent's owner

Every turn of an agent party in its own contact room is a contact turn, and its requester is the agent's owner.
Only the counterparty and the agent's owner may start one: the counterparty is the other party, plus its owner when the other party is an agent.
The owner's credentials, private state, budgets, and usage apply.
Without this, requester-scoped credentials and private state would belong to the other side.
The requester is decided once at ingress, where the relayed original sender is promoted today, because every later copy (coalescing, turn records, budgets, the tool context) derives from it; the model still sees the message as written by its physical sender, so it never mistakes the other side for its owner.
Because the owner is the requester, ordinary `access` admits the turn, so `authorization.py` needs no contact clause.

Ingress also refuses every other sender in a contact room itself, because ordinary `access` would admit an administrator there.
The agent's own messages never start a contact turn, so its earlier messages stay recognizable as its own.
Messages from the other side and from the owner both run as the owner, so coalescing and the newer-message check also compare the speaker, which keeps one from absorbing or superseding the other.
An edit made by someone other than the owner never regenerates a reply, because it would otherwise count as the owner revising their own message.
Ingress reads the contacts store only for personal agents with `contacts`, which keeps a corrupt store from affecting other agents and saves a file check per event for them.

### Contact detection: once per turn, and from the store for tools

A response asks the contacts store once, when it starts, whether its room is or was this agent's contact room, and everything it shows and offers follows that answer: its progress, final reply, and error notices, its tools and instructions, and its memory.
A response whose contact is already removed runs nothing, because ingress admitted it before the removal.
Asking again at each seam was rejected, because a contact removed or a config reloaded while the turn runs would let a later check show the turn's private final reply in a room where the other owner still sits.
Ingress, the memory flush, knowledge graph ingestion, and exploration ask the store themselves, and tools ask it at each call whether a room is or was this agent's contact room, so a turn keeps its restricted destinations after a removal and no turn writes into a removed contact's room.
Carrying a flag from ingress was rejected, because tool calls from normal turns into a contact room and the agent's own sends there have no ingress facts, and two sources could disagree.
The store read is cached by modification time, so the checks stay cheap.

### Who answers in a contact room

When both parties are agents, an unmentioned message from a person is answered only by that person's own agent, and a mentioned agent always answers.
Each agent of a contact runs a message there as its own owner, so without this rule both agents would answer every message an owner writes.
Agents never form a team in a room that is or was a contact room, because a team turn would show an unrestricted reply to the other side; each mentioned agent answers alone as its own owner's contact turn.
Agent messages wake the other agent only through a mention, which `matrix_message` adds when it names a `recipient`.

### Contact turns answer only through `matrix_message`

A contact turn's final reply is never shown, and the turn shows no placeholder, typing, streaming, tool-call trace, reasoning, error notice, or the reaction that acknowledges a message queued during a response.
A contact turn that fails or is interrupted ends unshown and is only logged.
The agent answers the other side by calling `matrix_message` into the contact room, which is the single place the contact policy runs.
This keeps one synchronous checkpoint: a revise comes back to the agent as the tool result in the same run, so no model-loop stage, delta buffering, or team-specific path is needed.
A gate on the final reply was rejected because the model loop yields text deltas before it knows an answer is final, the collected-delivery path prefers those deltas over the run's content, and team leaders build their loop hooks separately, so withholding and regenerating a reply would touch every reply path.

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

### Contact turns stay out of memory

Contact turns write no long-term memory, start no skill review, and their conversations are left out of the memory auto-flush, knowledge graph ingestion, and the tool calls exploration follows.
The other side drives these turns, so text it wrote must not persist into the owner's normal turns, and a skill review would post notices that bypass the policy.
Rooms that were contact rooms stay excluded after the contact ends, as long as the agent keeps `contacts` configured.

### Rules and tools per contact

Each agent side of a contact carries its own rules and tools, which only that side's owner sets, always through an approval card showing the exact values.

Rules are plain text the agent sees in its system prompt for contact turns and the judge sees with each message.
The effective rules of an agent party are its `contacts.rules` from config followed by the rules its owner set on that contact.
Config rules always apply, so an administrator's baseline (for example "never share credentials") cannot be relaxed by an owner; an owner can only add rules.

Tools differ from rules because the owner already has every tool of the agent in normal turns, so choosing which of them a contact may drive is the owner's risk to take.
The owner's list for a contact replaces the default; `null` means the agent's `contacts.tools` from config.
A list may name only the agent's own tools, and anything else is refused with the names it may use, as `update_agent` refuses tools.
A tool the agent later loses drops out of every contact's list at the next config load and comes back if the agent regains it, as with `generated_agent_tool_dropped`.
The agent may propose tools in the call, including ones the other side asked for, so the card is where the owner decides.

Config validation refuses only the control tools in `contacts.tools`, because config must not import tool metadata; Matrix room tools there are accepted and never offered, and the tools refuse them when an owner names them.

### Contact policy: hooks, then the judge

Every `matrix_message` send, edit, or reaction by an agent party into its contact room, from a contact turn or from a normal turn elsewhere, passes one policy pipeline before it is sent:

1. A send is denied once the target conversation ends with `defaults.max_consecutive_agent_replies` consecutive agent messages, because agent conversations started with `matrix_message` skip the limit that mentions in replies obey, and two agents acting for their owners could otherwise talk forever.
   Edits and reactions add no message, so they do not count.
2. Plugin hooks on the new `contact:before_send` event run in priority order.
   Each may leave the text alone, call `ctx.rewrite(text)` so later hooks and the judge see the new text, call `ctx.revise(feedback)`, or call `ctx.deny(reason)`.
   A revise or deny stops the chain, and any verdict a hook sets other than send, revise, or deny counts as deny.
   A text the hooks leave blank is denied before the judge, because without a judge nothing else would stop an empty message.
3. When the agent's `contacts.judge` is set, the judgment layer answers whether the final text follows the effective rules, given the rules and the text.
   A no becomes a revise whose feedback quotes the rules.
   The judge sees only the draft, not the conversation, which is what decides whether the draft discloses too much and keeps the judgment request small.

Reactions pass the policy like edits, because a reaction key is free text.
A rewritten message's tool result carries the text that was actually sent, so the agent knows what the other side read.

Outcomes:

| Outcome | Result |
| --- | --- |
| Send | Sent as written |
| Rewrite | Sent with the new text; the tool result says it was changed and quotes it |
| Revise | Not sent; the tool result carries the feedback, so the agent can write a new message in the same run |
| Deny | Not sent; the tool result carries the reason |

A hook that raises or times out, a judge that fails or abstains, and a judgment request the layer refuses all deny.
The pipeline fails closed because the rules are the owner's explicit condition for the contact, unlike `tool:before_call`, which fails open.
`contact:before_send` defaults to a 10 second timeout, because a useful hook may call a model.
Its context carries `message_received_depth` like tool contexts, so a hook that sends messages keeps the same loop prevention.

Secrets in a draft are detected only when a judge is set: the judgment layer refuses a request whose text its redactor would change, which denies the message.
A separate secret scan without a judge was not added, because the judgment layer's check is the detector the rest of MindRoom uses and a second one would drift from it.

### Flow

1. Alice asks her agent to reach Bob's agent.
   The agent calls `request_contact(target, reason, rules=None, tools=None)`, where `target` is an agent name, an agent's Matrix ID, or a person's Matrix ID.
   The call always shows Alice an approval card, like `update_agent`, whatever `tool_approval` says.
   With `party="me"` the requesting party is Alice herself instead of her agent, for when she wants to talk to Bob's agent directly.
2. The store records a pending contact, with Alice's thread as its origin.
   A pending or active contact for the same pair is returned unchanged instead of creating another, and a repeated request that sets rules or tools is refused in favor of `update_contact`, so a request never claims values it did not store.
   A declined one is refused until the target owner removes it, and only the target owner may remove a declined contact, so the requester cannot undo a decline by removing it.
3. Bob's personal-room agent posts the request as a new thread root in Bob's personal room, carrying `io.mindroom.contact_request` with the contact ID in its content.
   The text names the requesting party and its owner, quotes the reason, and lists the rules that would apply by default.
   The request records the contact first and then posts the notice with a transaction ID derived from the contact ID; a repeated request for a pending contact whose notice was never recorded posts it again.
4. Bob replies in that thread, and his personal-room agent answers like any turn Bob requested.
   It calls `accept_contact(contact_id, rules=None, tools=None)`, which always shows Bob an approval card with the exact rules and the resulting tool list, or `decline_contact(contact_id)`.
5. On acceptance the contact room is created as described in [The contact room](#the-contact-room).
6. Alice's agent is told in the origin thread, and that notice starts a turn requested by Alice, so her agent continues the original task.
   A decline posts a notice there and starts no turn.
7. The agents talk in the contact room with `matrix_message`, mentioning each other through `recipient`.
   `defaults.max_consecutive_agent_replies` stops runaway exchanges, and either owner can write in the room at any time.
8. Either owner can change their own side with `update_contact(contact_id, rules=..., tools=...)`, which shows a card and applies from the next contact turn, or call `remove_contact(contact_id)`, which does not show a card because it only takes access away.

`list_contacts()` lists the caller's contacts with their status, rules, and tools, and the personal agents of other owners that can be contacted.

### Storage

Contacts live in `tracking/contacts.json` in the storage directory, written atomically under a file lock like `tracking/personal_agents.json`, and cached in memory by the primary process.
Workers never read or write it, because the `static_runner` sidecar mounts `agents/` and records the primary treats as authority live in the primary-only `tracking/` directory.
Contacts are never deleted, only given a status, so a declined contact keeps blocking new requests until its target owner removes it.

### Configuration

A `contacts` object on an agent enables contacts for it and adds the `contacts` and `matrix_message` tools automatically, the way a non-empty `delegate_to` adds `delegate`, because a contact answers only through `matrix_message`.
Setting it under a `personal_agents` template's `agent:` enables it for every personal agent; agents created with `create_agent` never inherit it.
Its fields (`rules`, `tools`, and `judge`, the same object as `participation.judgment`) are documented on `docs/agent-contacts.md`.

### Code placement

New behavior lives in `src/mindroom/contacts/` with a docstring-only `__init__.py`: the store, party resolution, admission, contact-turn decisions, the policy pipeline with the judge question, and the operations behind the tools, including request notices and contact-room creation.
Ingress classification, the response runner, answer suppression, delivery, the hook registry, the memory flush, knowledge graph ingestion, exploration, and `matrix_message` get small seams that call into it.
`orchestrator.py` does not change, and `bot.py` only passes the agent name to the ingress validator, because the tools reach other agents' clients through the orchestrator runtime the tool context already carries.

### Testing

- The store, the tools' ownership checks, refusal of tools the agent lacks, dropping and restoring tools the agent loses, and the request, accept, decline, update, and remove transitions at the store and tool seams.
- Ingress resolving the owner as requester only for the counterparty and the owner, and refusing everyone else in a contact room.
- Contact turns offering only the restricted surface, staying out of memory, and never showing their final reply.
- The policy pipeline with fake hooks and a fake judgment backend, covering every outcome and every fail-closed path.
- `matrix_message` destinations, attachment refusal, and the policy outcomes in its tool results.

## Security Posture

`docs/architecture/security-posture.md` records these facts:

- A contact lets the other side start turns that run with the owner's authority, limited to the tools the owner approved for that contact.
  Anything those tools can reach, the other side can ask for, and a code-execution tool such as `shell` reaches everything the agent's worker or process can.
- Rules and the judge are model judgments, not a security boundary; they reduce accidental disclosure but a determined counterparty can still extract what the agent's context and contact tools expose.
- A contact grants no Matrix access outside its contact room.
- In a contact room, only the counterparty and the agent's owner can start the agent's turns.
- Contact turns write no long-term memory.

## Accepted Limitations

- Only personal agents and people can be parties.
- Contact turns cannot use approval-gated tools, Matrix room tools, attachments, skills, or knowledge bases.
- The judge sees the draft text only, and secrets are caught only when a judge is set.
- An agent party that fails to call `matrix_message` leaves the other side without an answer; its final reply stays private.
- A minimal-mode agent answers in contact turns only when the contact's tools include `shell`, because minimal mode reaches every tool through the shell.
- Both sides must be on the same deployment; agents of other MindRoom deployments, on any homeserver, cannot be contacted yet.

## Open Questions

- A live check with two personal agents through the `live-test` skill, including an acceptance where the personal-room agent that accepts also creates the contact room, has not run yet.
