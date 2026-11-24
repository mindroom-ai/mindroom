# Delegation child snapshots earlier releases stored

Each `<release>.json` holds what that release's own writers stored for three delegated children of agent `leader`, so the startup rewrite of delegation child snapshots is tested against rows those writers produced instead of hand-written ones.
The children are one authored with a tool list, one authored without a tool list on an explicitly chosen model, and one that runs the configured agent; releases before personas existed store all three as configured children.

| File | Release and what its snapshots lack |
| --- | --- |
| `v2026.10.18.json` | The last release without `agent_mode`, `persona`, and `requested_model_name`. |
| `v2026.10.230.json` | The last release without `persona` and `requested_model_name`. |
| `v2026.10.244.json` | The last release without `requested_model_name`, and its personas lack `skills` and `knowledge`. |
| `v2026.10.245.json` | Its personas lack `skills` and `knowledge`. |
| `v2026.10.263.json` | Its personas lack `knowledge`. |
| `v2026.10.683.json` | v2026.10.244's rows after v2026.10.683 read them and wrote them back: every field present, the requested model null, and the database converted to layout version 4. |

In each file, `subagent_sessions` holds the session files the release's `reserve_subagent_turn` and `update_subagent_turn` wrote, and `conversation_database` is the dump of agent `leader`'s conversation database, holding a parent run paused on the first child's approval, which the release's `persist_delegation_state` saved with all three children and the pending call's source.
`views` is what v2026.10.683's readers, which filled absent fields on every read, read of each copy: every field of each child, with the requested model a follow-up resolves and the skills and knowledge each persona keeps.

## Recording again

The recorder ran each release's source, extracted with `git archive <release> src/mindroom`, on this repository's environment, which pins the same Agno release those releases pinned.
It prepared the three children with that release's `prepare_child_turn`, saved the paused run through that release's session storage, and then reserved and updated each child's session file.
For `v2026.10.683.json` it copied v2026.10.244's state, rewrote each session file with v2026.10.683's `update_subagent_turn_sync`, and saved the run's delegation state again with its `persist_delegation_state` after its store had converted the database.
The views came from v2026.10.683's `DelegationChild.from_dict` and from `DelegationState.from_metadata` on the run its store returned.
Recording again draws new random identifiers, so the files differ in them only.
