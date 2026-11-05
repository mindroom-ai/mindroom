# Contacts Page in the Connections Portal

This record explains why the Connections portal gets a Contacts page, what it lets an owner do without a model in the loop, and what is deliberately left out.

## Why

[Agent contacts](../agent-contacts.md) shipped with only a chat surface: an owner sees their contacts through `list_contacts` and changes them through tool calls that each show an approval card.
That makes the most basic questions slow to answer, such as "who is my agent in touch with, and under which rules and tools?", because they need a model turn and a YAML dump.
Every change also costs a model turn and a card, even when the owner already knows exactly what they want.
The Connections portal is where people already manage what their agents may reach, so a contact's tools and rules belong next to the accounts those tools use.

## Understanding

What the user asked for:

- A page beside **Agents** and **Agent Keys** that shows the signed-in user's current contacts with their rules and tools.
- The proposed management features in the same pull request: edit rules and tools, accept, decline, and remove, plus warnings about each tool's reach, flags for contact tools whose account is not connected, and request and decision dates.

Assumptions:

- The page is for owners, so it shows only contacts the signed-in user owns a side of.
- Requesting a new contact stays in chat, because a request carries a task the requesting agent continues once it is accepted, and the portal has no conversation for it to continue in.

## Decisions

### Only your own side of a contact is shown

The page shows the rules and tools of the viewer's side only.
The other owner's rules are their private conditions, and showing them would tell the other side what that owner treats as sensitive.
The other party is shown by display name with its agent ID and its owner's Matrix ID beside it, because the other side chooses its agent's display name.

### Removed contacts are not listed

The page lists pending, active, and declined contacts.
A removed contact has no terms left to manage, owners who joined its room keep it in their Matrix client, and the store also holds removed copies of retried acceptances' abandoned rooms, which would read as duplicates.
A declined contact stays listed, because it blocks new requests until the owner who declined removes it.

### Portal actions skip the approval card

The contacts tools show an approval card because the model that issues the call also reads untrusted text, such as a request's reason.
A click on the page comes from the owner directly, authenticated by the portal session and limited to same-origin requests like every other portal mutation, so no model can be steered into it.
Accepting, declining, and removing still ask for confirmation in the page, and accepting or editing shows the terms in a form before anything is stored.
A stolen portal session can already connect accounts and allow Agent Keys, so acting on contacts adds no new class of risk.

### Contact operations act for an owner, not a tool context

`contacts/flow.py` took a `ToolRuntimeContext` and an agent name for every operation, though accept, decline, update, and remove only need the owner, the config, and the running bots.
The operations now take a `ContactActor` naming the owner and, for tool calls, the agent the owner acts through.
Building a fake tool context in the API was rejected, because it would invent a room, thread, and turn that do not exist.
Requesting keeps its tool context, because a request needs the conversation it came from; the page lists contacts in its own shape rather than through the tool's listing, which is written for a model.

Accepting and declining through a tool still require the agent of the owner's personal room, because that is where the request was asked; the portal acts as the owner, so it has no agent to check, and the personal-room agent posts any notice it needs.
After a decision made on the page, that agent posts it in the request's thread, so the thread does not keep asking for an answer that was already given, and a later reply there gets a clear refusal instead of a stale question.

Accepting, declining, and removing make agents create, post to, join, or leave rooms, so they take a slot of the runtime's response admission gate, as dashboard room departures do, and are refused while a config reload swaps the agents out.
Updating rules and tools only writes the store, so it needs no slot.
The runtime lends the API a lookup of its agent bots for this, rather than the orchestrator itself, because the API needs nothing else from it.

### Tool warnings come from tool metadata

Each tool a contact may use is shown with what its reach means for the other side, derived from metadata every tool already declares and the agent's `file_access`.
A tool that runs code reaches everything the agent's worker or process can, and so does an `unconfined` tool, or an `agent` tool on an agent with `file_access: unrestricted`, since nothing limits it to the agent's files; only an `agent` tool on a confined agent reaches just the agent's files.
The server works this out, because it depends on the agent's `file_access`, which the page does not have, and a warning that understates a tool's reach misleads the owner at the moment they grant it.
An OAuth `auth_provider` acts with the owner's connected account, or with a shared service account when one serves the provider, which the warning names, since the other side can then drive that account.
The warnings say what the other side can ask for, matching the security warning in the contacts docs, at the moment the owner chooses the tool.

A tool that uses an account the owner has not connected is flagged, because contact turns run as the owner and would fail to use it; a provider a shared service account serves is never flagged, because the owner has no account of their own to connect there.
The flag reuses the per-agent connection state the Agents page already loads, so it needs no new route and always agrees with that page.

### Requests and decisions are dated

Contact records gain `requested_at`, set when a request is first stored, and `decided_at`, set when it is accepted or declined, so the page can say when a request was made and since when a contact is active or declined.

## Accepted Limitations

- The page cannot request a new contact; owners ask their agent in chat.
- Once an owner chooses tools for a contact, the page cannot return that side to the agent's configured default tools; it can only choose another explicit list.
- The other owner's display name is not looked up, so owners are shown by Matrix ID.
- The page shows no history of messages the contact policy rewrote, returned, or blocked.
