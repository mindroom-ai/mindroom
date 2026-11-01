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

- "The room of my colleague's agent" is the colleague's personal room with that agent.
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

### Contact turns run as the agent's owner

Every turn of an agent party in its own contact room is a contact turn, and its requester is the agent's owner.
Only the counterparty and the agent's owner may start one: the counterparty is the other party, plus its owner when the other party is an agent.
A message from anyone else in a contact room starts nothing.
The owner's credentials, private state, budgets, and usage apply.
Without this, requester-scoped credentials and private state would belong to the other side.
The requester is decided once at ingress, where the relayed original sender is promoted today, because every later copy (coalescing, turn records, budgets, the tool context) derives from it; the model still sees the message as written by its physical sender, so it never mistakes the other side for its owner.
Because the owner is the requester, ordinary `access` admits the turn, so `authorization.py` needs no contact clause.

### Contact turns answer only through `matrix_message`

A contact turn's final reply is never shown, and the turn shows no placeholder, typing, streaming, tool-call trace, or reasoning.
The agent answers the other side by calling `matrix_message` into the contact room, which is the single place the contact policy runs.
This keeps one synchronous checkpoint: a revise comes back to the agent as the tool result in the same run, so no model-loop stage, delta buffering, or team-specific path is needed.
A gate on the final reply was rejected because the model loop yields text deltas before it knows an answer is final, the collected-delivery path prefers those deltas over the run's content, and team leaders build their loop hooks separately, so withholding and regenerating a reply would touch every reply path.

### Contact turns get a restricted surface

The trigger of a contact turn is text the owner may not have written, so a contact turn offers less than a normal turn:

- Only the contact's tools for this side (see [Rules and tools per contact](#rules-and-tools-per-contact)) plus `matrix_message`, minus every function that a `tool_approval` rule or the tool itself would gate, because an approval card in the contact room would show the owner's tool arguments to the other side.
  This reuses the filter the OpenAI-compatible API already applies to approval-gated tools, and the toolkit restriction authored subagents use.
- A contact's tools may not include Matrix room tools (those that require room context, such as `scheduler`, `matrix_api`, or `thread_move`), because they post into rooms without passing the policy, nor MindRoom control tools (`contacts`, `delegate`, `agent_manager`, `self_config`, `config_manager`, `dynamic_workflow`, `claude_agent`), because they act with the owner's authority beyond the conversation.
- `matrix_message` sends only to the contact room and to the owner's personal room with this agent, so the agent can report back to its owner and nowhere else.
- No attachments into a contact room, because the judge cannot inspect files.
- Other tools that post into an explicitly named room refuse contact rooms, so text reaches a contact room only through `matrix_message` or the agent's normal reply outside a contact turn, which never happens because every agent-party turn there is a contact turn.

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

### Contact policy: hooks, then the judge

Every `matrix_message` send or edit by an agent party into its contact room, from a contact turn or from a normal turn elsewhere, passes one policy pipeline before it is sent:

1. A send is denied once the target conversation ends with `defaults.max_consecutive_agent_replies` consecutive agent messages, because agent conversations started with `matrix_message` skip the limit that mentions in replies obey, and two agents acting for their owners could otherwise talk forever.
2. Plugin hooks on the new `contact:before_send` event run in priority order.
   Each may leave the text alone, call `ctx.rewrite(text)` so later hooks and the judge see the new text, call `ctx.revise(feedback)`, or call `ctx.deny(reason)`.
   A revise or deny stops the chain.
3. When the agent's `contacts.judge` is set, the judgment layer answers whether the final text follows the effective rules, given the rules and the text.
   A no becomes a revise whose feedback quotes the rules.
   The judge sees only the draft, not the conversation, which is what decides whether the draft discloses too much and keeps the judgment request small.

Outcomes:

| Outcome | Result |
| --- | --- |
| Send | Sent as written |
| Rewrite | Sent with the new text; the tool result says it was changed |
| Revise | Not sent; the tool result carries the feedback, so the agent can write a new message in the same run |
| Deny | Not sent; the tool result carries the reason |

A hook that raises or times out, a judge that fails or abstains, and a judgment request the layer refuses (for example because the text contains a detected secret) all deny.
The pipeline fails closed because the rules are the owner's explicit condition for the contact, unlike `tool:before_call`, which fails open.
`contact:before_send` defaults to a 10 second timeout, because a useful hook may call a model.

### Flow

1. Alice asks her agent to reach Bob's agent.
   The agent calls `request_contact(target, reason, rules=None, tools=None)`, where `target` is an agent name, an agent's Matrix ID, or a person's Matrix ID.
   The call always shows Alice an approval card, like `update_agent`, whatever `tool_approval` says.
   With `party="me"` the requesting party is Alice herself instead of her agent, for when she wants to talk to Bob's agent directly.
2. The store records a pending contact, with Alice's thread as its origin.
   A pending or active contact for the same pair is returned instead of creating another, and a declined one is refused until the target owner removes it.
3. Bob's agent posts the request as a new thread root in Bob's personal room with it, carrying `io.mindroom.contact_request` with the contact ID in its content.
   The text names the requesting party and its owner, quotes the reason, and lists the rules that would apply by default.
   The request records the contact first and then posts the notice with a transaction ID derived from the contact ID; a repeated request for a pending contact whose notice was never recorded posts it again.
4. Bob replies in that thread, and his agent answers like any turn Bob requested.
   It calls `accept_contact(contact_id, rules=None, tools=None)`, which always shows Bob an approval card with the exact rules and the resulting tool list, or `decline_contact(contact_id)`.
   A target that is a person uses the person's own personal agent, which accepts on their behalf in their personal room.
5. On acceptance the target agent creates the contact room, invite-only like a personal room, invites the other parties and both owners, and makes both agent parties join and keep it across restarts.
   The router is left out, so it never routes messages there; contact turns need no approval cards.
6. Alice's agent is told in the origin thread, and that notice starts a turn requested by Alice, so her agent continues the original task.
   A decline posts a notice there and starts no turn.
7. The agents talk in the contact room with `matrix_message` and replies, mentioning each other as in any agent conversation.
   `defaults.max_consecutive_agent_replies` stops runaway exchanges, and either owner can write in the room at any time.
8. Either owner can change their own side with `update_contact(contact_id, rules=..., tools=...)`, which shows a card and applies from the next contact turn, or call `remove_contact(contact_id)`, which does not show a card because it only takes access away.
   Removing a contact makes its agents leave the contact room; the owners keep the room and its history.

`list_contacts()` lists the caller's contacts with their status, rules, and tools, and the personal agents and people that can be contacted.

### Storage

Contacts live in `tracking/contacts.json` in the storage directory, written atomically under a file lock like `tracking/personal_agents.json`, and cached in memory by the primary process.
Workers never read or write it.

```text
ContactRecord
  id, status (pending | active | declined | removed)
  requester: Party, target: Party        # Party = agent name or person ID, plus owner ID
  reason
  sides: {requester: Side, target: Side}  # Side = rules: str | None, tools: list[str] | None; person sides stay empty
  origin_room_id, origin_thread_id
  notice_room_id, notice_event_id
  room_id
  created_at, decided_at
```

### Configuration

A `contacts` object on an agent enables contacts for it and adds the contacts tools automatically, the way a non-empty `delegate_to` adds `delegate`.
Setting it under a `personal_agents` template's `agent:` enables it for every personal agent.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `rules` | list of strings | `[]` | Rules that apply to every contact of this agent, ahead of each owner's own rules |
| `tools` | list of tool names | `[]` | Default tools offered in contact turns when the owner chose none for that contact; approval-gated functions are never offered |
| `judge` | judgment backend or null | `null` | Same object as `participation.judgment`; null runs no judge |

```yaml
personal_agents:
  mind:
    agent:
      tools: [google_calendar, matrix_message]
      contacts:
        rules:
          - Never share passwords, keys, or other credentials.
          - From my calendar, share only when I am free or busy, never what the events are.
        tools: [google_calendar]
        judge:
          provider: llm
          model: fast
```

### Code placement

New behavior lives in `src/mindroom/contacts/` with a docstring-only `__init__.py`: the store, the tools, the policy pipeline with the judge question, the request notices, and contact-room creation.
Ingress classification, the response runner, answer suppression, the hook registry, and `matrix_message` get small seams that call into it; `bot.py` and `orchestrator.py` do not change, because the tools reach other agents' clients through the orchestrator runtime the tool context already carries.

### Testing

- The store, the tools' ownership checks, refusal of tools the agent lacks, dropping and restoring tools the agent loses, and the request, accept, decline, update, and remove transitions at the store and tool seams.
- Ingress resolving the owner as requester only for the counterparty and the owner, and refusing everyone else in a contact room.
- Contact turns offering only the restricted surface and never showing their final reply.
- The policy pipeline with fake hooks and a fake judgment backend, covering every outcome and every fail-closed path.
- `matrix_message` destinations, attachment refusal, and the policy outcomes in its tool results.
- A live check with two personal agents through the `live-test` skill.

## Security Posture

`docs/architecture/security-posture.md` gains these facts when contacts ship:

- A contact lets the other side start turns that run with the owner's authority, limited to the tools the owner approved for that contact.
  Anything those tools can reach, the other side can ask for, and a code-execution tool such as `shell` reaches everything the agent's worker or process can.
- Rules and the judge are model judgments, not a security boundary; they reduce accidental disclosure but a determined counterparty can still extract what the agent's context and contact tools expose.
- A contact grants nothing outside its contact room.
- In a contact room, only the counterparty and the agent's owner can start the agent's turns.

## Accepted Limitations

- Only personal agents and people can be parties.
- Contact turns cannot use approval-gated tools, Matrix room tools, or attachments.
- The judge sees the draft text only.
- An agent party that fails to call `matrix_message` leaves the other side without an answer; its final reply stays private.
- Both sides must be on the same deployment.
