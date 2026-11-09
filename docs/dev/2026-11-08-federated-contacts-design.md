# Contacts Across Deployments

## Goal

Let a personal agent be in contact with a personal agent of another MindRoom deployment, on the same homeserver or a federated one, with the same consent, rules, judge, and restricted contact turns that contacts within one deployment have.
Success means Alice's agent on deployment A asks for Bob's agent on deployment B by its Matrix ID, Bob sees and answers the request in his personal room on B, and the two agents then talk in a shared room where every message each sends passes its own owner's policy.

## Understanding

What the user asked for:

- The agent contacts feature works across servers.

Assumptions:

- "Across servers" includes other deployments on the same homeserver, because the hosted `mindroom.chat` homeserver serves many independent deployments and their people, so another server name cannot be what marks the other side as remote.
- Both sides run MindRoom with this change; the other side is a personal agent with `contacts`.
- People on another deployment, and `party="me"` toward one, stay out of scope, because a person has no deployment-side account that receives the request, and a person who asks has no agent party to create the room.

## Approaches Considered

### Chosen: Matrix membership is the protocol

Each deployment keeps its own record of a contact in its own store, and the two learn each other's decisions only from the contact room's membership:

| Step | Matrix action |
| --- | --- |
| Request | The requesting agent creates the contact room and invites the target agent, with the request's reason as the invite's `reason` |
| Accept | The target agent joins the room |
| Decline | The target agent rejects the invite |
| Withdraw a pending request | The requesting agent withdraws the invite and leaves |
| Remove | Either agent leaves; the other side ends its record when it sees the leave, and leaves too |

The room is invite-only with shared history, like a contact room within one deployment.
The other agent gets power level 100 only once it joins, so each side then invites and withdraws its own owner; before that the requesting agent alone has power, because withdrawing an invite needs more power than the invitee has wherever room creators are not privileged, as in room versions up to 11.
No custom event or state type crosses deployments, so nothing beyond the Client-Server API every homeserver implements has to agree between the two sides.

### Rejected: request data in the invite's member content

Custom keys in the invite's `m.room.member` content would let the target read who asks for whom before joining, but `POST /invite` accepts only `reason`, and sending the invite through the state endpoint to carry other keys is a Synapse behavior other homeservers do not promise.

### Rejected: request data in the create event's stripped state

Synapse includes `m.room.create` in an invite's stripped state, but matrix-nio parses only member, name, and alias events there and drops the rest, so the target would need its own sync parsing.

### Rejected: joining before the owner consents

Joining every invited room to read a request event from its state would expose the target's membership to every inviter its patterns match before its owner decides, and a room the agent joined for a request that is not one would see it join and leave.

### Rejected: to-device messages or an HTTP API between deployments

To-device messages reach only the devices a user has when they are sent, and an agent can log in as a new device after a restart.
An HTTP API needs each deployment reachable from the other and a shared secret, while Matrix federation already authenticates every sender's server.

## Decisions

### Opt-in by Matrix ID patterns

`contacts.remote_agents` lists exact or wildcard Matrix IDs of agents on other deployments that this agent may ask and be asked by, with the same pattern rules as `accept_invites`; it defaults to empty, which keeps contacts within the deployment.
A server allowlist was rejected, because people and other deployments' agents share the hosted homeserver, so a server name cannot tell an agent's invite from a person's.
Without a list, any Matrix user could post text into an owner's personal room through a request.
An invite from a sender this deployment does not manage that matches the target agent's patterns is a contact request and never reaches the agent's generic invite handling, even when its `accept_invites` would also accept the sender.

### Only the agent's Matrix ID is trusted

Federation proves which server sent an event, so the other side's agent is known by its Matrix ID, and nothing else about it is.
The other side's owner is not exchanged, because no deployment can verify another deployment's claim of who owns its agent, and a false claim naming a person on this deployment would let that person start turns of this agent.
So only the other agent may start a contact turn here, and each owner's messages reach only their own agent.
Prompts, the judge, and notices name the other side by Matrix ID as an agent of another MindRoom deployment, since its display name is the other side's to choose.

### Each side enforces only its own owner's decisions

The owner's answers to one request from another deployment run one at a time, because every one of them joins or leaves the request's one room, while an acceptance within one deployment opens a room of its own.
A declined request blocks new requests on the side whose owner declined, where the target rejects new invites from that agent until its owner removes the declined contact.
The requesting side lets its own owner remove a declined request, because the owner who declined is out of its reach; a new request then reaches the other side, which rejects it again while its declined record stands.
A request from the agent that declined this side's request is asked anyway and clears that decline here, since the side that declined has evidently cleared its own.

### The requester creates the room when it asks

The room exists before the target accepts, because the invite is the request.
The requesting agent keeps its pending request's room across restarts, like an active contact's room, because the target can join only while a member remains; a contact within one deployment still keeps only its active room, because an acceptance cut short there leaves a pending room its agents must not rejoin.
The target marks the contact active and then joins, because the other side reads the join as the acceptance and may write at once, and a message refused while the target's record is still pending is never delivered; a join that fails or is cancelled puts the request back to pending and leaves again.

### Membership events drive the requesting side

The requesting side activates its contact when the target agent joins, records a decline when the invite is rejected, and ends an active contact when the other agent leaves or is removed, from the journaled member events of the contact room.
A join, rejection, or leave that reached no live event, as across a sync gap, is read from the other agent's membership in the room's state when the agent starts, so it cannot strand a contact; the room's joined members could not tell a pending invite from a rejected one.
The generic invite handling never joins the room of any contact the agent has or had, at a reload or start included and whatever the config then says of the inviter, because its join is the acceptance the other side reads.
A local agent that someone else removes from the room ends its contact the same way, because it can no longer speak there.

### The other agent behaves like an agent in the room

Any sender that this deployment does not manage counts as a person everywhere else, so in a contact room the other side's agent is classified from the contact itself:

- Its unmentioned messages start no turn, because agents within one deployment wake each other only through a mention, and otherwise every message would wake the other side and the two would answer each other until the limit stopped them.
- Its messages count toward `defaults.max_consecutive_agent_replies`, because a run of agent messages that a remote agent's message resets would never reach the limit, and the two agents could talk forever.
- It can be named as `recipient`, so the agent wakes it with a mention as it wakes an agent of its own deployment.

Treating every remote contact agent as a `bot_accounts` entry everywhere was rejected, because the human checks read the config only, and making them read the contacts store would let a corrupt store affect every agent's ingress.

### Notices of remote requests

A remote request is stored before the invite's handling returns, and its notice is posted after, because the homeserver hands an invite to that handling once and a restart restores it without the reason.
A remote request's notice is posted when the invite arrives, and again whenever the same invite is seen while its notice is missing.
A request whose notice could not be posted still waits on the owner's Contacts page and in `list_contacts`.
A new invite to a room whose request was declined or removed is rejected, so a settled room never waits as a dangling invite.

## Security Posture

[Security Posture](../architecture/security-posture.md) records what a remote contact exposes: its conversation is stored on both homeservers, and each request posts the other side's text into the owner's personal room.

## Accepted Limitations

- A request sent before the other deployment lists the asking agent is not picked up once it does, because a restored invite carries no reason; the asking owner removes it and asks again.
- An agent whose deployment does not list the asking agent in `remote_agents` treats the request as an ordinary invite, and one that accepts every invitation joins, which the asking side reads as an acceptance its owner never gave; nobody answers in that room.
  Telling that join from a real acceptance needs a signal on the join, such as a `reason`, which the agent's membership commands cannot send.
- People on another deployment cannot be contacted and cannot ask, and `party="me"` reaches only this deployment.
- The other side's owner reads the contact room but cannot start a turn of this agent, and a mention of it from them is refused.
- In a thread the other agent wrote in, an owner's unmentioned message counts the other agent as a second person, so the owner mentions their own agent there.
- A remote request whose notice could not be posted is shown only on the Contacts page and by `list_contacts`.
- The asked side never sees a request withdrawn, because the agent receives no member event when an invite to a room it never joined is withdrawn; the request stays pending there until its owner answers it, where accepting fails and removing it clears it, or until the same agent asks again, which replaces it.
- An agent that stops between marking a remote request accepted and joining its room keeps the contact active without joining, so the other side stays pending until either owner removes the request.

## Evidence

A live run on 2026-11-09 used two Tuwunel homeservers federating over TLS, one deployment on each, and a third deployment on the first homeserver, with a real model driving every tool call.
Across the federated pair it covered a request, its notice, acceptance in the owner's thread, both agents talking through mentions, a refused turn from the other side's owner, `max_consecutive_agent_replies` counting the other agent's messages, `update_contact`, removal, withdrawal of a pending request, a decline, a refusal of a pair the other side had declined, and a restart that missed an acceptance and one that missed a removal.
Across the pair on one homeserver it covered a request, acceptance, conversation, and removal.

The run found that declining, refusing, and removing a pending request from another deployment sent nothing to the homeserver, because the bot's membership commands read an invite as already left, so the asking side stayed pending.
Rejecting the invite therefore leaves the room on the homeserver directly.

A second live run with both homeservers on room version 11, where creators have no unlimited power, covered withdrawing a pending request, the asked agent inviting its owner before it gets power, the grant of power level 100 after the join, and both agents withdrawing their owners' unjoined invites on removal.
It also showed that the asked side never sees a withdrawal, which led to a new invite from the same agent replacing the stale request.

## Open Questions

- The Contacts page has not been exercised in a live run with a request from another deployment.
