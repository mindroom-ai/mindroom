# Agno parity fixtures

These fixtures pin what Agno 3.0.9 plus MindRoom's patches produce, so the owned runtime that replaces Agno can be checked against them.

## Tools

`.venv/bin/python -m tests.agno_parity.capture_tools` writes `tools/schemas.json` and `tools/arguments.json`, and `tests/agno_parity/test_tool_goldens_current.py` checks the current code against both.
Both files are pretty JSON with sorted keys.

### `tools/schemas.json`

The file maps each built-in tool registration name to `{"instructions", "add_instructions", "functions"}`.
`instructions` and `add_instructions` are the toolkit's own values; Agno adds `instructions` to the system prompt only when `add_instructions` is true, and no function adds instructions of its own.
`functions` lists the functions in the order Agno prepares them for an async run: sync functions in registration order, each replaced in place by an async variant of the same name, then async-only functions.
Each function records `name`, `description`, `parameters`, `strict`, `requires_confirmation`, `external_execution`, `approval_type`, `stop_after_tool_call`, and `show_result` after Agno's per-run preparation (`parse_tools`), which runs `process_entrypoint` and MindRoom's schema postprocessors.
The provider payload `Function.to_dict()` is the same entry without `stop_after_tool_call`, `show_result`, and null values.

Each tool is built by `build_agent_toolkit` with its default configuration, for one agent of a config shaped like `mindroom config init` output.
File memory gives that agent a workspace, so output-file wrapping adds `mindroom_output_path`, and the agent delegates and uses the knowledge graph, so the `delegate` and `knowledge_graph` tools exist.
`dynamic_tools` is built beside a deferred `calculator`, because tool search exists only with a deferred toolkit.
Tools that need settings to construct get dummy credentials (`DUMMY_CREDENTIALS`, `DUMMY_ENV`), and SDK clients that contact their service during construction are replaced by mocks (`OFFLINE_CLIENTS`).
The capture hides the host environment and refuses network connections, so the `update_awareness` instructions report an unknown latest release.

Entries leave out policy that agent construction applies after a tool is built: approval capability (which pauses, hides, or job-gates functions per `tool_approval`, and marks `delegate`'s `run_subagent` and `continue_subagent` as external execution), persona and function filters, hook bridges, and the per-turn `background_after` parameter that tool jobs add to managed calls.

Normalization: the running MindRoom version in the `update_awareness` instructions is replaced by `<MINDROOM_VERSION>`.

Not captured:

- `apify`, because its optional dependency `apify-client` is not installable on this platform (aarch64).
- `composio`, because its functions come from the Composio action catalog, which only the service returns.

Agno behavior the golden pins as it is:

- `gemini`'s `generate_image` has no description and exposes only `mindroom_output_path`, because Agno cannot resolve its `Agent` annotation and falls back to an empty schema.
- `openclaw_compat` builds a toolkit without functions.

### `tools/arguments.json`

The file maps a case id to `{"function", "raw_arguments", "arguments", "kwargs"}` or `{"function", "raw_arguments", "arguments", "error_text"}`.
Each case sends one tool call, whose arguments string is `raw_arguments`, to a probe in `tests/agno_parity/tool_argument_probes.py` through Agno's `Model.get_function_calls_to_run` and `Model.arun_function_calls`.
`kwargs` is what the probe body received, each value encoded by `encode_kwargs` as `{"type": <Python type name>, "value": ...}`, so coercion such as `"3"` to `3` stays visible.
`error_text` is the tool message content the model reads when the call fails.
`arguments` is the tool message's `tool_args`, which history keeps: the decoded arguments after Agno turns top-level `"true"`, `"false"`, `"none"`, and `"null"` strings into booleans and null, and drops values the model sent for injected parameters such as `agent`.
`arguments` is null when the model sent `[]`, and absent when Agno rejects the call before running it.
The probes import no Agno code, so owned implementations keep using them after Agno is removed.

Normalization: the pydantic version in error links (`https://errors.pydantic.dev/2.13/v/...`) is replaced by `<PYDANTIC_VERSION>`.
Decoding errors embed the message of Python's `ast.literal_eval`, and probes that skip validation fail with Python's own `TypeError` text, so both depend on the interpreter version (Python 3.13).
