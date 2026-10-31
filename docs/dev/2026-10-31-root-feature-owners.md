# Root feature owners: hooks, bot adapters, and managed accounts

This record keeps the decisions behind the October 2026 move of feature logic out of `bot.py` and `orchestrator.py` that the code cannot show: why each owner sits where it does, which pieces stayed in the composition roots on purpose, and the alternatives review rejected.

## Hook capabilities

A bot's hook support owns every capability it binds into hook contexts, its live sender included.
The orchestrator offers the router bot's hook support through one accessor; the hook support decides which capability falls back to the router's client.
The router never asks that accessor for its own capabilities, which is what keeps capability lookup from recursing.
A router without a client still binds its own sender, because a hook context retained across a failed router start must send once the same router connects.

Scheduled hooks keep their own capability set on purpose: the scheduled-task runtime's hook lifetime, which outlives any bot generation, and a sender built from the client, config, and reader the fire was handed, because the scheduling executor receives exactly those values rather than a bot and is called that way by every scheduled fire and its tests; during a fire the operation lends the owner bot's client, so it sends as the bot's own sender would.

Agent lifecycle emission stayed in the bot.
The bot supplies the identity, the room lists, and the timing at every call, and stopped hooks must run before its hook lifetime ends, so a hooks-side emitter would only move the payload.

## Bot adapters

The approval-response and job-cancel event-type constants live with the journal classifier, their only reader, because the classifier must not import approval execution or the job runtime for a wire string.

`admit_job_follow_up` stayed on the bot: the follow-up coordinator would still need the bot's journal principal and a public way to wake its dispatcher, so moving it would remove no hop.
`approval_room_ids` stayed on the bot: approval transport and thread export read it from the bot, so a room-lifecycle owner would only add a pass-through.

## Managed accounts

Account preparation checks every requested localpart before it creates any account, then checks the entity requests again after it prepares the internal user, because a homeserver may give the internal user another localpart than the one requested and the runtime persists the one it got.

The bot construction path still prepares the accounts of the entities it builds, after a reload already prepared them before publishing.
That repetition is deliberate: the construction path also serves restarts, background retries, and MCP catalog restarts, and threading the reload's prepared accounts through it would add state to every fleet create for no change in behavior.
