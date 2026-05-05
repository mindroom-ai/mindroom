# Summary

No meaningful duplication found for `src/mindroom/agent_prompts.py`.
The primary behavior, `build_agent_identity_context`, is the single source that renders the Matrix agent identity prompt, model disclosure, team self-identification instruction, Matrix conversation-history guidance, optional OpenAI-compatible history guidance, and full-Matrix-ID mention guidance.

# Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
build_agent_identity_context	function	lines 20-35	none-found	"build_agent_identity_context", "AGENT_IDENTITY_CONTEXT_TEMPLATE", "OPENAI_COMPAT_HISTORY_GUIDANCE", "Your Identity", "Matrix ID:", "powered by the", "complete Matrix ID", "OpenAI-compatible API contexts"	src/mindroom/agents.py:1097; tests/test_agent_datetime_context.py:83; tests/test_agents.py:152; src/mindroom/routing.py:90; src/mindroom/matrix/presence.py:72; src/mindroom/agent_descriptions.py:16
```

# Findings

No real duplicated behavior was found.

`src/mindroom/agents.py:1097` is the only production caller of `build_agent_identity_context`.
It gathers the display name, Matrix ID, model provider, model ID, and OpenAI-compatible guidance flag, then delegates prompt rendering to `src/mindroom/agent_prompts.py:20`.

`tests/test_agent_datetime_context.py:83` and `tests/test_agents.py:152` assert parts of this prompt behavior rather than duplicating it.

Related code in `src/mindroom/routing.py:90`, `src/mindroom/matrix/presence.py:72`, and `src/mindroom/agent_descriptions.py:16` also formats agent/model/role information, but the behavior is different.
Those paths build router metadata, Matrix presence text, or agent descriptions, not the agent identity system prompt.

# Proposed Generalization

No refactor recommended.
The identity prompt already has a focused helper and template in `src/mindroom/agent_prompts.py`.
The nearby related formatters serve different surfaces and should stay separate unless their behavior converges.

# Risk/Tests

Risk is low because no production code changes are recommended.
If this helper is changed later, relevant tests are `tests/test_agent_datetime_context.py` for prompt inclusion and `tests/test_agents.py` for optional OpenAI-compatible guidance.
