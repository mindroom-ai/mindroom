## Summary

No meaningful duplication found in `src/mindroom/routing.py`.
The module contains the only active AI-based single-agent routing flow I found under `src/mindroom`.
There are related patterns for structured Agno output parsing, MatrixID-to-agent-name filtering, and agent capability descriptions, but none duplicate the full routing behavior closely enough to justify a refactor.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_AgentSuggestion	class	lines 25-29	related-only	output_schema BaseModel agent_name reasoning TeamModeDecision ScheduledWorkflow	src/mindroom/teams.py:615, src/mindroom/scheduling.py:688, src/mindroom/thread_summary.py:344, src/mindroom/topic_generator.py:95
suggest_agent	async_function	lines 32-128	related-only	Decide which agent Route messages Available agents describe_agent output_schema arun routing	src/mindroom/scheduling.py:638, src/mindroom/teams.py:615, src/mindroom/custom_tools/delegate.py:61, src/mindroom/voice_handler.py:427
suggest_agent_for_message	async_function	lines 131-157	related-only	MatrixID.parse agent_name(config runtime_paths) replace_visible_message ResolvedVisibleMessage sender domain available_agents	src/mindroom/thread_utils.py:89, src/mindroom/authorization.py:173, src/mindroom/response_runner.py:914, src/mindroom/teams.py:714
```

## Findings

No real duplication requiring consolidation was found.

Related pattern: structured Agno classification/parsing appears in `src/mindroom/routing.py:93`, `src/mindroom/teams.py:615`, and `src/mindroom/scheduling.py:688`.
All three create a short-lived `Agent` with an `output_schema`, call `arun`, check `response.content`, log unexpected types, and return a domain fallback or error.
The behavior is related, but the prompts, fallback semantics, session IDs, model selection, and result validation differ enough that a shared wrapper would mostly hide important call-site policy.

Related pattern: agent identity filtering appears in `src/mindroom/routing.py:144`, `src/mindroom/authorization.py:173`, `src/mindroom/thread_utils.py:89`, `src/mindroom/response_runner.py:914`, and `src/mindroom/teams.py:714`.
These call sites all convert `MatrixID` values to configured agent names with a username fallback or filtering behavior.
They are not duplicates of `suggest_agent_for_message` because routing must discard unresolved agents before prompting, while team and response flows preserve usernames as display/fallback labels and authorization filters apply sender permissions.

Related pattern: agent description rendering is shared correctly through `describe_agent`.
`src/mindroom/routing.py:57` and `src/mindroom/custom_tools/delegate.py:61` both build text for available specialist agents, but they already reuse `src/mindroom/agent_descriptions.py:17`.
The remaining formatting difference is local prompt shape, not duplicated source-of-truth behavior.

## Proposed Generalization

No refactor recommended.
If future work adds another AI chooser that selects one configured agent from a `MatrixID` or agent-name list, consider a small helper near `routing.py` for formatting candidate agent descriptions and validating the chosen agent name.
Do not extract the current structured-output call pattern yet; the fallback behavior differs by domain.

## Risk/Tests

No production code was changed.
If the related MatrixID filtering were ever generalized, tests should cover unresolved Matrix IDs, router exclusion where required, username fallback behavior, and sender authorization differences.
If the structured Agno parsing pattern were ever generalized, tests should cover invalid schema content, model exceptions, configured model selection, and each call site's existing fallback result.
