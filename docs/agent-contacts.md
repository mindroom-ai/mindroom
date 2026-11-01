---
icon: lucide/users
---

# Agent Contacts

Contacts let a [personal agent](personal-agents.md) talk to another person's personal agent, or to another person, after both owners approve.
Each contact gets its own room where the two sides talk, and each agent there keeps acting for its own owner, under rules its owner sets for that contact.
Every message an agent sends to the other side first passes a policy that can send it, rewrite it, return it to the agent with feedback, or block it.

Use contacts when two specific people want their agents to work together, such as agreeing on a meeting time, with consent on both sides and limits for each pair.
Use the template's `access` (see [Letting Agents Talk to Each Other](personal-agents.md#letting-agents-talk-to-each-other)) when everyone it admits may work with an agent directly; an agent reached that way acts for the person who asked, not for its owner.

## Setup

```yaml
rooms:
  lobby: {}
personal_agents:
  mind:
    agent:
      display_name: "{user_display_name}'s Mind"
      model: sonnet
      tools: [google_calendar]
      contacts:
        rules:
          - Never share passwords, keys, or other credentials.
          - From my calendar, share only when I am free or busy, never what the events are.
        tools: [google_calendar]
        judge:
          provider: llm
          model: fast
personal_rooms:
  agent: mind
  onboarding_rooms: [lobby]
```

Setting `contacts` under a template's `agent:` enables contacts for every personal agent built from it, and a `users` override can change or remove it for one person.
An agent with `contacts` gets the `contacts` and `matrix_message` tools automatically, so you do not list them under `tools`.
Requests are delivered to, and accepted through, the agent of each person's [personal room](personal-rooms.md), so the template that `personal_rooms.agent` names needs `contacts` too, or nobody can send its users a request.
Agents of other templates can be contacts as well when their template sets `contacts`.

> [!WARNING]
> **A contact lets the other side put your agent to work.**
> The other side of a contact can start turns of your agent that run with your authority, limited to the tools you approved for that contact.
> Anything those tools can reach, the other side can ask for, and a code-execution tool such as `shell` reaches everything your agent's worker or process can.
> A contact turn also sees your agent's instructions, preloaded files, and recalled memories.
> Rules and the judge are model judgments, not a security boundary: they reduce accidental disclosure, but a determined counterparty can still extract whatever your agent's context and contact tools expose.
> Approve only tools whose reach you are willing to share with the other side.

## How a Contact Works

1. Alice asks her agent to get in touch with Bob's agent, and the agent calls `request_contact`, which shows Alice an approval card.
2. Bob's personal-room agent posts the request as a new thread in Bob's personal room, naming Alice's agent and Alice, quoting the reason, and listing the rules that would apply to Bob's agent.
3. Bob replies in that thread, and his personal-room agent accepts with `accept_contact`, which shows Bob an approval card with the exact rules and tools, or declines with `decline_contact`.
4. On acceptance, an invite-only contact room is created holding both agents, Alice, and Bob, without the router.
   Alice and Bob each get an invitation they accept in their Matrix client to watch or write there.
5. Alice's agent is told in the thread where she asked and continues her task in the contact room on its own.
   After a decline, that thread gets a notice and Alice's agent does nothing further.
6. The agents talk in the contact room, and Alice and Bob can read along and write there at any time.

The target of a request is a personal agent's name or Matrix ID, or a person's Matrix ID.
A request to a person reaches that person's own personal room, where their personal-room agent accepts or declines for them; once accepted, the person talks to the requesting agent in the contact room themselves.
With `party="me"`, Alice herself is the requesting party instead of her agent, so she talks to Bob's agent in the contact room directly.
A contact needs an agent on at least one side and two different owners.

Asking again for a pair that already has a pending or active contact returns that contact instead of creating another; change its rules or tools with `update_contact`.
After a decline, new requests for that pair are refused until the owner who declined removes the declined contact with `remove_contact`.
Removing a contact makes its agents leave the contact room; the owners keep the room and its history.

## Configuration

`contacts` is an object on an agent; set it to `{}` to enable contacts with no default rules, tools, or judge.

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `rules` | list of strings | `[]` | Rules for every contact of this agent; owners can add rules for a contact but cannot remove these |
| `tools` | list of tool names | `[]` | Tools a contact turn offers when the owner chose none for that contact; `matrix_message` is always offered |
| `judge` | object or null | `null` | A [judgment backend](configuration/threads.md#judgment-backends) that checks each message against the rules; `null` runs no judge |

`tools` names the agent's own toolkits, never function names.
It cannot include `contacts`, `delegate`, `agent_manager`, `self_config`, `config_manager`, `dynamic_workflow`, `claude_agent`, `skill_manage`, or `dynamic_tools`, which fail validation with `contacts.tools may not include <tool>`.
Tools that need the room they run in, such as `scheduler`, `todo`, `attachments`, `script`, and the other `matrix_*` and `thread_*` tools, are never offered in contact turns even when listed; `matrix_message` is the only one a contact turn gets.
Changes to `contacts` apply from the agent's next turn without a restart.

Only personal agents built from a `personal_agents` template can be contacts; agents authored under `agents:` and agents created with `create_agent` cannot.

## Tools

| Function | Approval card | What it does |
| --- | --- | --- |
| `list_contacts()` | No | Lists your contacts with their ID, status, which side you are on, the other party, the reason, and your side's rules and tools, plus other owners' personal agents that you can ask |
| `request_contact(target, reason, rules=None, tools=None, party="agent")` | Yes | Asks the target's owner for a contact; `rules` and `tools` set your agent's side, and with `party="me"` you ask as yourself and set neither |
| `accept_contact(contact_id, rules=None, tools=None)` | Yes | Accepts a pending request to you and opens the contact room; `rules` and `tools` set the side of your agent that was asked |
| `decline_contact(contact_id)` | No | Declines a pending request to you and tells the thread it came from |
| `update_contact(contact_id, rules=None, tools=None)` | Yes | Changes your side's rules or tools from the next turn in that contact; blank `rules` clear them |
| `remove_contact(contact_id)` | No | Ends a contact; its agents leave the contact room |

Only the agent's owner can use these tools; anyone else, administrators included, gets `Only <owner> may use the contacts of <agent>`.
`request_contact`, `accept_contact`, and `update_contact` always show an [approval card](tool-approval.md#approving-and-denying) to the owner with the exact values, even when `tool_approval.default` is `auto_approve`, and only the owner's [`!auto_approve`](tool-approval.md#auto-approve-everything) window skips it.
A request is accepted only through the target owner's personal-room agent, usually by replying in the request's thread.
`list_contacts` shows every other owner's personal agents that have contacts, together with their owners' Matrix IDs.
Scripts and workflow participants cannot call these tools, and `contacts` cannot be deferred.

```python
request_contact(
    target="bob_mind",
    reason="Find a 30-minute slot for the project review next week.",
    rules="Share only free and busy times.",
    tools=["google_calendar"],
)
```

## Rules and Tools per Contact

Each agent side of a contact has its own rules and tools, and only that side's owner sets them, always on an approval card.
A person's side has neither.

An agent's rules for a contact are its `contacts.rules` followed by the rules its owner set for that contact, so an administrator's baseline always applies.
The agent sees these rules in every contact turn, and the judge checks each message against them.

The tools an owner chooses for a contact replace `contacts.tools` for that contact; without a choice, the configured default applies.
An owner can choose only tools their agent has, and a call naming anything else fails with `Not among the tools a contact may use: <tools>; allowed: <tools>`.
A tool the agent later loses is left out of every contact until the agent has it again.

## Contact Turns

Every turn of an agent in its own contact room is a contact turn, and it runs as the agent's owner: it uses the owner's credentials and counts toward the owner's usage and [budgets](budgets.md).
Only the other side (the other agent and its owner, or the other person) and the agent's own owner start its turns there; a message from anyone else, administrators included, starts nothing.
When both sides are agents, an unmentioned message from an owner is answered only by that owner's agent, while a mentioned agent always answers.
Agents wake each other only by mentioning, which `matrix_message` does when it names a `recipient`.

A contact turn answers only by calling `matrix_message` into the contact room.
Its final reply is never shown, and it shows no placeholder, typing, streaming, tool calls, reaction, or error notice.
An agent that ends a contact turn without calling `matrix_message` leaves the other side without an answer.

A contact turn keeps the agent's identity, instructions, preloaded files, and memory recall, and differs from a normal turn in these ways:

- It offers only the contact's tools plus `matrix_message`, and no skills, knowledge bases, or interactive questions.
- It leaves out every function that would ask for approval, so a `tool_approval` rule that gates `matrix_message` leaves the agent unable to answer.
- `matrix_message` can read and write the contact room, and send a plain message to the owner in their personal room when the agent serves that room; it cannot send attachments or collapsible sections into a contact room.
- Other tools that post into rooms refuse the contact room.
- It saves nothing to the agent's memory, starts no skill review, and its conversations are neither summarized into file memory nor added to the [knowledge graph](knowledge-graph.md), so your agent's other conversations do not remember it.

A contact gives the other side no access to your other rooms.
Even your own messages in a contact room start contact turns, so use your personal room for anything that needs your agent's full tools.

## Message Policy

Every message, edit, and reaction an agent sends into its contact room passes the policy before anything is sent, including one you ask it to send from your personal room.
The checks run in order:

1. A new message is refused once the conversation already ends with [`defaults.max_consecutive_agent_replies`](configuration/agents.md#defaults) (default `50`) agent messages in a row, until a person writes.
2. Plugin hooks on [`contact:before_send`](hooks.md#policy) run in priority order and can rewrite, return, or block the message.
3. When `contacts.judge` is set, the judge decides whether the text follows the agent's rules for the contact.
   It sees only the message and the rules, not the conversation.

| Outcome | Result |
| --- | --- |
| Send | Sent as written |
| Rewrite | The new text is sent, and the agent's tool result says it was changed and shows what was sent |
| Revise | Nothing is sent; the agent gets the feedback and can write a new message in the same turn |
| Deny | Nothing is sent; the agent gets the reason |

The judge answering no is a revise whose feedback quotes the rules.
The policy fails closed: a hook that fails or times out, a hook that leaves the message blank, a judge that fails, times out, or abstains, and a message the judge cannot be given all deny.
The judge is never given a message that looks like it contains a secret such as an API key or password, or a message that, together with the rules, exceeds 16 KB, so with a judge set such messages are denied; without a judge, nothing checks for secrets.

### Writing a `contact:before_send` Hook

A hook is a [plugin](plugins.md) function that reads `ctx.text` and `ctx.rules` and calls `ctx.rewrite(text)`, `ctx.revise(feedback)`, or `ctx.deny(reason)`; [Hooks](hooks.md#policy) lists every context field.

```python
import re

from mindroom.plugin_api import hook

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


@hook("contact:before_send")
async def hide_email_addresses(ctx):
    ctx.rewrite(EMAIL.sub("[email hidden]", ctx.text))
```

## Limitations

- Agents of another MindRoom deployment, on this homeserver or another one, cannot be contacted yet.
- In [minimal mode](tools/agent-cli.md), an agent can answer in contact turns only when the contact's tools include `shell`.

## Troubleshooting

The contacts tools return errors to the agent as `Error: <message>`, and `matrix_message` reports a stopped message as `Not sent: <reason>`.

| Message | Cause and fix |
| --- | --- |
| `<target> cannot be contacted: contacts are not enabled for it` | The target's template has no `contacts`; an administrator adds it |
| `<target> cannot be contacted: only personal agents and people can be contacted` | The target is an authored or created agent; use its `access` instead |
| `<target> cannot be contacted: it is neither a personal agent nor a Matrix user ID` | The name is wrong; `list_contacts` shows the agents that can be asked |
| `<agent> is not a personal agent; only personal agents can have contacts` | `contacts` is set on an authored agent; set it on a `personal_agents` template instead |
| `<owner>'s personal agent <agent> does not take contact requests` | The agent of the target owner's personal room has no `contacts`; add `contacts` to the template `personal_rooms.agent` names |
| `<owner> has no personal room yet, so the request cannot reach them; ask again once they have one` | The target owner has no personal room on this deployment; ask again after they have one |
| `<agent> is not running, so it cannot post the request; ask again later` | The target owner's personal-room agent is stopped or still starting |
| `<owner> declined this contact; they can remove it to allow a new request` | The pair was declined; only the owner who declined can call `remove_contact` on it |
| `Only <agent>, the agent of <owner>'s personal room, may accept this contact` | Accept by replying in the request's thread in your personal room |
| `<agent> is not running, so the contact room cannot be set up; accept again later` | One agent of the contact is stopped; accept again once it runs |
| `Error: in a contact conversation you can only write to this room or to your owner's personal room` | A contact turn tried another room |
| `Error: in a contact conversation you can only send a plain message to your owner's personal room` | A contact turn tried to read, edit, react, or name a recipient in the owner's personal room |
| `Error: contact rooms accept text only` | Attachments and collapsible sections cannot go into a contact room |
| `Not sent: The conversation already has <n> agent messages in a row; wait for a person to write.` | The agents reached `defaults.max_consecutive_agent_replies`; any person writing in the conversation resets it |
| `Not sent: A contact policy hook failed.` | A `contact:before_send` hook raised or timed out; check the plugin's logs |
| `Not sent: The message could not be checked; try again later.` | The judge failed, timed out, or abstained |
| `Not sent: The message cannot be checked (essential_input_redacted).` | With a judge set, the message looks like it contains a secret; remove it |
| `Not sent: The message cannot be checked (essential_input_too_large).` | With a judge set, the message and rules exceed 16 KB; send a shorter message |
| An agent never answers in the contact room | Check that the sender is the other side or the agent's owner (MindRoom logs `contact_room_sender_refused` otherwise), that an unmentioned message came from that agent's own owner, that no `tool_approval` rule gates `matrix_message`, and that the agent called `matrix_message`, since its final reply never shows |
