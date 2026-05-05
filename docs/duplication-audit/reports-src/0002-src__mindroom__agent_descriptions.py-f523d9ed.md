## Summary

Top duplication candidate: `describe_agent` and `commands.handler._format_agent_description` both convert configured agents/teams into short human-readable capability summaries from role, tools, team membership, and team role.
The duplication is related rather than identical because `describe_agent` is routing-prompt oriented and includes router, delegation, collaboration mode, and first instruction details, while `_format_agent_description` is welcome-message oriented and uses Markdown/tool truncation.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
describe_agent	function	lines 17-55	related-only	describe_agent, _format_agent_description, get_agent_tools(agent_name), Team of agents, Collaboration mode, Can delegate to, Tools:	src/mindroom/routing.py:56, src/mindroom/commands/handler.py:95, src/mindroom/voice_handler.py:401, src/mindroom/custom_tools/config_manager.py:474, src/mindroom/custom_tools/config_manager.py:880
```

## Findings

### Related duplication: agent/team capability summary formatting

- Primary behavior: `src/mindroom/agent_descriptions.py:17` builds a text description for router, team, known agent, or unknown entity.
- Related behavior: `src/mindroom/commands/handler.py:95` builds a concise welcome-message description for an agent or team.
- Both paths read `config.agents`, `config.teams`, agent role, effective agent tools via `config.get_agent_tools(agent_name)`, and team membership/role to produce user- or model-facing capability text.
- `src/mindroom/routing.py:56` uses `describe_agent` as the canonical routing prompt formatter, so the primary function is already shared for routing.
- `src/mindroom/voice_handler.py:401` lists available agent/team names and display names for speech normalization, but it does not duplicate role/tool/team capability summarization.
- `src/mindroom/custom_tools/config_manager.py:474` and `src/mindroom/custom_tools/config_manager.py:880` also render agent configuration summaries, but those are full config-management outputs that include display name, model, rooms, validation state, and creation details; they overlap on role/tools only and are not a strong deduplication target for this primary file.

Differences to preserve:

- `describe_agent` includes router handling, unknown entity handling, delegation targets, collaboration mode, and a short first instruction when under 100 characters.
- `_format_agent_description` formats tools as backticked Markdown, truncates tools to three entries with a `+N more` suffix, omits delegation/instructions, and summarizes teams as a count rather than member names/mode.
- `_format_agent_description` returns an empty string for unknown names, while `describe_agent` returns an explicit unknown-agent string.

## Proposed Generalization

A small formatter helper could live in `src/mindroom/agent_descriptions.py`, for example a pure function that extracts a typed summary object for one configured entity and leaves audience-specific rendering to callers.
That helper would centralize entity classification plus common data extraction: role, effective tools, delegate targets, team members, team mode, and first short instruction.

No production refactor is recommended from this audit alone.
The two active renderers serve different audiences, and forcing them through one text renderer would likely add parameters for Markdown, tool truncation, router inclusion, unknown handling, team count/member detail, delegation detail, and instruction inclusion.
If more renderers appear, prefer extracting data collection first rather than a single universal string formatter.

## Risk/tests

- Risk: changing either formatter could subtly alter routing quality or welcome-message readability because their output is consumed by different audiences.
- If generalized later, add focused tests for `describe_agent` covering router, team, agent with role/tools/delegation/short instruction, long instruction omission, and unknown entity.
- Add separate command-handler tests for welcome formatting so Markdown tool truncation and team count behavior remain stable.
