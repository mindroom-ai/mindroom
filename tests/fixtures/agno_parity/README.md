# Agno parity goldens

These fixtures pin what MindRoom produced while Agno 3.0.9 ran it, so the owned runtime can prove it reproduces the same bytes.
They were captured before Agno was removed and cannot be regenerated afterwards; a deliberate change updates the golden in the same commit.

## Prompts

`prompts/<scenario>.json` holds every model request one scenario sends, as the provider adapter receives it from the request loop.
`tests/agno_parity/capture_prompts.py` builds each scenario through the production builders (`ai.stream_agent_response`, `teams.team_response_stream`, `matrix_rtc.call_tools.build_call_tools`, and each helper's call site), and `tests/agno_parity/test_prompt_goldens_current.py` proves the Agno-backed code still reproduces every file.
Rewrite the fixtures with `.venv/bin/python -m tests.agno_parity.capture_prompts [scenario ...]` from the repository root.

Each file has these keys:

- `scenario` and `description` name what the file pins.
- `input` records what the scenario ran: the authored `config.yaml` mapping, workspace files, the turn's prompt and thread history, and the entry point.
- `requests` lists each request with the model `id` and `provider` string, whether it was a streaming call, its `messages`, its `tools`, its `tool_choice`, and its `response_format`.
- `exported` appears only where MindRoom hands model-visible values to a model outside its own loop: the realtime voice model's instructions and tool schemas, and the Live voice model's system prompt.

The capture replaces the provider call, so it sees every request hook MindRoom wraps around that call, such as portable tool search, but not the provider's own wire conversion, which the `providers/` goldens pin.
Messages are `Message.to_dict()` without the volatile `id`, `created_at`, and `metrics` keys, plus `"add_to_agent_memory": false` on transient context messages that are never stored.
`tools` holds the formatted tool definitions in the order the request carried them.
A pydantic `response_format` is recorded as `{"pydantic_model": <class name>, "json_schema": <model_json_schema()>}`; a dict format is recorded as passed.

Normalization:

- The capture's temporary root becomes `<TMP>`, which appears in preloaded context-file headings.
- Today's date in the date context, such as `Monday, October 19, 2026`, becomes `<DATE>`; the timezone is pinned to `UTC`, and message timestamps and the schedule parser's current time are fixed inputs.
- Requests are grouped by the model instance that received them, in the order MindRoom built those models, and each model's requests stay in call order.
  A team builds its members before its leader, so member requests come first; members run concurrently, so their relative call order is not part of the contract.
- Collaborate-mode members all answer `ok`, because Agno's streaming broadcast result concatenates member output in arrival order, which is not deterministic.

Scenarios use Claude (`anthropic`, `claude-sonnet-5-5`) unless named otherwise.
Structured-output helpers are captured twice: on `anthropic`, which receives the schema as `response_format`, and on `vertexai_claude`, which Agno asks for JSON in the system message with `{"type": "json_object"}`.
Agent scenarios set `learning: false`, because Agno Learning is retired with Agno and its prompt block and tools have no owned equivalent.
