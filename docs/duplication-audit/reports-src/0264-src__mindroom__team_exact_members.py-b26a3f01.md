# Summary

No meaningful duplication found.
`src/mindroom/team_exact_members.py` is already the shared implementation for exact team-member materialization and live shared-agent availability.
The closest related behavior is team request eligibility classification in `src/mindroom/teams.py`, configured-team construction in `src/mindroom/api/openai_compat.py`, and turn-policy filtering in `src/mindroom/turn_policy.py`; these consume the helper or operate at a different abstraction level.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
ResolvedExactTeamMembers	class	lines 24-31	related-only	ResolvedExactTeamMembers, requested_agent_names, failed_agent_names, materialized_agent_names, TeamResolution, TeamResolutionMember	src/mindroom/teams.py:449, src/mindroom/teams.py:459, src/mindroom/api/openai_compat.py:1396
resolve_live_shared_agent_names	function	lines 34-50	related-only	resolve_live_shared_agent_names, agent_bots.items, bot.running, ROUTER_AGENT_NAME, materializable_agent_names	src/mindroom/turn_policy.py:262, src/mindroom/teams.py:1287, src/mindroom/orchestrator.py:458
materialize_exact_requested_team_members	function	lines 53-94	related-only	materialize_exact_requested_team_members, materialize_exact_team_members, create_agent, failed_agent_names, display_names=[str(agent.name)	src/mindroom/teams.py:1203, src/mindroom/teams.py:1274, src/mindroom/api/openai_compat.py:1396
```

# Findings

No real duplication was identified.

`ResolvedExactTeamMembers` overlaps conceptually with `TeamResolution` and `TeamResolutionMember` in `src/mindroom/teams.py:449` and `src/mindroom/teams.py:459`, but the data models represent different stages.
`TeamResolution` stores Matrix-level eligibility and final routing outcome, while `ResolvedExactTeamMembers` stores Agno agent instances, display names, materialized names, and failed materialization names after runtime construction.
Merging them would couple request classification to runtime agent construction.

`resolve_live_shared_agent_names` is used directly by `TurnPolicy.materializable_agent_names` in `src/mindroom/turn_policy.py:262` and `_materialize_team_members` in `src/mindroom/teams.py:1287`.
The similar `_running_bots_for_entities` helper in `src/mindroom/orchestrator.py:458` only returns running bot objects for explicit entity names and includes teams/router depending on the caller.
It does not duplicate the shared-agent name filtering because it lacks the `config.agents` and router exclusion policy used for team materialization.

`materialize_exact_requested_team_members` is consumed by `materialize_exact_team_members` in `src/mindroom/teams.py:1203`.
The surrounding code in `src/mindroom/api/openai_compat.py:1396` and `src/mindroom/teams.py:1274` delegates to that path instead of reimplementing the loop.
Other team code builds team labels, history scopes, or eligibility results, but it does not repeat the behavior of prechecking materializable names, invoking a member factory, logging per-member construction failures, and returning a structured exact-materialization result.

# Proposed Generalization

No refactor recommended.
The module already appears to be the intended narrow generalization for exact team-member materialization.

# Risk/Tests

No production code was changed.
If this module is changed later, focused tests should cover:

- known-unavailable materializable names short-circuit before `build_member` is called.
- partial `build_member` failures produce `failed_agent_names` while preserving successfully materialized agents.
- `resolve_live_shared_agent_names` excludes the router, excludes non-configured entities, and returns `None` when runtime agent bots are not dictionary-like.
