## Summary

No meaningful duplication found.
`src/mindroom/api/sandbox_protocol.py` centralizes a small subprocess envelope and stderr marker protocol that is only used by `sandbox_runner.py`.
The closest related behavior is the workspace env hook marker parsing in `sandbox_exec.py`, but it has different delimiters, stream handling, error behavior, and payload shape, so a shared abstraction would add indirection without removing active duplication.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
SandboxSubprocessEnvelope	class	lines 12-17	none-found	SandboxSubprocessEnvelope, committed_config, runtime_paths dict request envelope, class .*Envelope(BaseModel)	src/mindroom/api/sandbox_runner.py:286, src/mindroom/api/sandbox_runner.py:1158, src/mindroom/api/sandbox_runner.py:1219, src/mindroom/api/sandbox_runner.py:1237
serialize_subprocess_envelope	function	lines 20-31	none-found	serialize_subprocess_envelope, model_dump_json, subprocess envelope serialization, committed_config runtime_paths request	src/mindroom/api/sandbox_runner.py:1158, src/mindroom/api/sandbox_runner.py:1161, src/mindroom/api/openai_compat.py:1123, src/mindroom/scheduling.py:546
parse_subprocess_envelope	function	lines 34-36	none-found	parse_subprocess_envelope, model_validate_json, subprocess payload validation, SandboxSubprocessEnvelope.model_validate_json	src/mindroom/api/sandbox_runner.py:1088, src/mindroom/api/sandbox_runner.py:1219, tests/api/test_sandbox_runner_api.py:4059, tests/api/test_sandbox_runner_api.py:4511
response_marker_payload	function	lines 39-41	related-only	RESPONSE_MARKER, response_marker_payload, marker payload prefix, SANDBOX_RESPONSE	src/mindroom/api/sandbox_runner.py:1207, src/mindroom/api/sandbox_runner.py:1223, src/mindroom/api/sandbox_runner.py:1268, src/mindroom/api/sandbox_exec.py:386
extract_response_json	function	lines 44-51	related-only	extract_response_json, rfind RESPONSE_MARKER, marker extraction, find marker_chunk	src/mindroom/api/sandbox_runner.py:1085, src/mindroom/api/sandbox_exec.py:500, src/mindroom/api/sandbox_exec.py:508, src/mindroom/tool_system/events.py:428
```

## Findings

No real duplication was found for the subprocess envelope model or its JSON serialize/parse helpers.
`SandboxSubprocessEnvelope` is the only envelope-shaped Pydantic model carrying `request`, `runtime_paths`, and `committed_config`.
`SandboxRunnerExecuteRequest` in `src/mindroom/api/sandbox_runner.py:286` is related because it is nested inside the envelope, but it models the tool execution request itself rather than the parent-to-child transport wrapper.

The stderr response marker helpers have one related pattern in `src/mindroom/api/sandbox_exec.py:500`.
`_parse_workspace_env_hook_output` also locates a marker in subprocess output and slices the text after the marker.
The behavior is not duplicated enough to generalize: sandbox protocol uses a fixed `__SANDBOX_RESPONSE__` marker, searches stderr with `rfind`, strips a trailing JSON response, and returns `None` on absence; workspace env hook uses a per-call random marker plus NUL delimiter, searches stdout with `find`, raises `WorkspaceEnvHookError` on absence, and parses a NUL-separated environment block with filtering and size checks.

`src/mindroom/tool_system/events.py:428` also uses `rfind`, but only to replace the most recent visible Matrix tool marker in message text.
It does not implement subprocess framing or response extraction.

## Proposed Generalization

No refactor recommended.
The existing protocol helpers are already the right-sized shared boundary for their active call sites in `sandbox_runner.py`.
Sharing marker extraction with the workspace env hook would need parameters for delimiter, search direction, absent-marker behavior, and post-processing, which would be more complex than the current local code.

## Risk/Tests

No production changes were made.
If this module changes later, the relevant tests are the subprocess sections in `tests/api/test_sandbox_runner_api.py`, especially call sites that build or parse `sandbox_protocol.response_marker_payload(...)` and `parse_subprocess_envelope(...)`.
Any future extraction across marker protocols should add focused tests for missing marker handling, multiple markers, empty trailing payloads, and payloads with unrelated stdout/stderr text before the marker.
