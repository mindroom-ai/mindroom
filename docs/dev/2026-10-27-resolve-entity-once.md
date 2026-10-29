# Resolve each entity's model, tools, placement, and policy once

Created 2026-10-27 for the `refactor/resolve-entity-once` pull request.
This record holds the reasons and rejected alternatives behind `turn_model.py`, `tool_system/agent_tools.py`, `config/resolution.py`, and the typed policy inputs; the code shows what they do.

## Problem

Before this change, "what does agent X get" had many answers.
The turn's model was chosen in three places with different precedence and budgeting, tool lists and tool placement were rebuilt by each consumer (descriptions, saved approvals, jobs, the MCP gateway, Connections), and `runtime_overlays` patched a config copy so later readers would see overridden values.
Execution policy was derived twice, once from typed config and once from raw dicts, and about ten call sites assembled the agent's worker target by hand.
Every divergence between those copies was a bug class: a saved approval could authorize a delegate past the maximum delegation depth, and a saved `openclaw_compat` approval was refused on roomless turns that the live build allowed.

## Decisions

- **One record per entity, recomputed on demand.**
  `Config.resolve_scope` and `resolve_agent` return frozen records built from small per-fact functions; validators, tolerant readers, and callers that need one fact call the same functions.
  There is no cache: `model_copy` copies private attributes, and both production and tests mutate configs after validation, so a cached record would go stale without any signal.
  Building a full agent record costs about 35 microseconds; hot paths that need one fact, such as execution policy in `resolve_agent_execution`, call that fact's function (about 3 microseconds) instead of the whole record, which also keeps dedicated workers, whose configs carry no models, from evaluating facts they cannot answer.
- **The turn's model has one owner.**
  Precedence is explicit, then the thread override, then the saved room override, then `room_models`, then the authored model, and the requester's budget may then swap in its fallback.
  A reply resolves its model without a budget for the cold-cache check, and the budget applies afterwards to the model that check picks, so the check judges the model the room chose rather than the fallback.
  For teams, an explicit model or a configured team's thread override governs every member; each distinct member model is budgeted once.
- **Tool candidates come from the record; eligibility and placement stay per turn.**
  Which tool owner wins depends on the attached deferred entries of the turn, the delegation depth, room flags, and portable search, so one config-time owner per tool would be wrong.
  `ToolPlacement.runs_in_worker` keeps the existing short-circuit order exactly, pinned by a byte-identical golden, because placement is the worker security boundary and reordering checks can change the answer for unsupported backends.
- **Policy inputs are one pydantic model.**
  The dashboard draft schema already validated the fields the policy reads, so it became `PolicyInputs` and the raw-dict parser went away; typed configs enter through `PolicyInputs.from_agent`.
  The agent's execution carries its worker target, built once, and its worker key and identity derive from that target.
- **Every agent has a workspace.**
  Shared agents without file memory had no workspace, and each feature that writes files (skills, output files, context files, CLI output) carried its own no-workspace branch and refusal text.
  The workspace directory is cheap, and removing the branch removed a whole family of special cases; file memory still decides only where `MEMORY.md` lives.

## Rejected alternatives

- A per-`Config` record cache: stale after mutation, and no measured need.
- One config-time owner per tool: the owner depends on per-turn deferred entries.
- Placement inside `Config`: it needs the environment and tool registry metadata, which config validation must not load.
- Reflecting `PolicyInputs.model_fields` into the worker projection, the Kubernetes snapshot, and delegation storage: each projection discloses a different allowlist on purpose.
- A `create_agent` parameter object or topic mixins on `Config`: more indirection with no removed decision.
- Renaming `_SandboxProxyConfig`: it removes no decision and its request half belongs to the worker transport.

## Limitations and open questions

- Dedicated worker backends still derive policy from the config file they read rather than from policy the primary sends; sending the resolved policy in the worker request belongs to the worker protocol.
- Voice-call transcripts stay routed by memory backend rather than by workspace.
- A shared agent resolved with no requester while `CUSTOMER_ID` or `ACCOUNT_ID` is set now gets its tenant's shared worker key instead of none; with a requester, keys are unchanged.
