Summary: No meaningful duplication found.
The primary module is a focused Vertex Claude compatibility shim that strips OpenAI-style provider-level `strict` flags from Agno tool definitions before Agno formats them for Anthropic-on-Vertex requests.
Related Vertex Claude provider handling exists in model loading, prompt-cache hooks, doctor checks, and thread-summary temperature handling, but none duplicates this sanitizer or the exact request/beta override behavior.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
strip_vertex_claude_tool_strict	function	lines 10-44	none-found	strip_vertex_claude_tool_strict; pop("strict"); function strict; tools strict; process_entrypoint strict	src/mindroom/vertex_claude_compat.py:10; src/mindroom/tool_system/output_files.py:178; src/mindroom/tool_system/output_files.py:600; src/mindroom/history/compaction.py:830; src/mindroom/tools/shell.py:356
MindroomVertexAIClaude	class	lines 47-72	related-only	MindroomVertexAIClaude; VertexAIClaude; vertexai_claude; agno.models.vertexai.claude	src/mindroom/model_loading.py:23; src/mindroom/model_loading.py:119; src/mindroom/vertex_claude_prompt_cache.py:7; src/mindroom/vertex_claude_prompt_cache.py:76; src/mindroom/thread_summary.py:15; src/mindroom/thread_summary.py:88; src/mindroom/cli/doctor.py:14; src/mindroom/cli/doctor.py:288
MindroomVertexAIClaude._prepare_request_kwargs	method	lines 50-62	none-found	_prepare_request_kwargs; prepare request kwargs; format tools for model; strict tool request	src/mindroom/vertex_claude_compat.py:50; src/mindroom/vertex_claude_prompt_cache.py:94; src/mindroom/vertex_claude_prompt_cache.py:113; src/mindroom/vertex_claude_prompt_cache.py:132; src/mindroom/vertex_claude_prompt_cache.py:151
MindroomVertexAIClaude._has_beta_features	method	lines 64-72	none-found	_has_beta_features; beta features; strict structured outputs; tool strict beta	src/mindroom/vertex_claude_compat.py:64; src/mindroom/tools/claude_agent.py:41; src/mindroom/custom_tools/claude_agent.py:241; src/mindroom/custom_tools/claude_agent.py:300
```

Findings:
No real duplicated behavior was found for `strip_vertex_claude_tool_strict`.
The closest matches are strict-schema preparation paths in `src/mindroom/tool_system/output_files.py:178`, `src/mindroom/history/compaction.py:830`, and `src/mindroom/tools/shell.py:356`.
Those call `Function.process_entrypoint(strict=...)` or force a function's `strict` setting for schema generation; they do not sanitize already-built provider payload dictionaries and should preserve different semantics.

No real duplicated behavior was found for `MindroomVertexAIClaude`.
`src/mindroom/model_loading.py:119` selects this class for the `vertexai_claude` provider, while `src/mindroom/vertex_claude_prompt_cache.py:76`, `src/mindroom/thread_summary.py:88`, and `src/mindroom/cli/doctor.py:288` contain other Vertex Claude-specific handling.
Those are related provider adaptations, but they address prompt-cache block annotation, summary temperature suppression, and connection validation rather than the tool strict compatibility fix.

No real duplicated behavior was found for `_prepare_request_kwargs`.
The method only delegates to Agno's Vertex Claude request builder after sanitizing tools.
The wrapper functions in `src/mindroom/vertex_claude_prompt_cache.py:94`, `src/mindroom/vertex_claude_prompt_cache.py:113`, `src/mindroom/vertex_claude_prompt_cache.py:132`, and `src/mindroom/vertex_claude_prompt_cache.py:151` also delegate to existing model methods with lightly transformed inputs, but they transform message lists for prompt caching rather than request tool payloads.

No real duplicated behavior was found for `_has_beta_features`.
This method prevents provider-level `strict` flags from affecting Agno's structured-output beta detection.
The Claude-agent beta configuration in `src/mindroom/tools/claude_agent.py:41` and `src/mindroom/custom_tools/claude_agent.py:300` is related only by the word beta; it controls a custom tool option and does not inspect model request payloads.

Proposed generalization: No refactor recommended.
The sanitizer is single-purpose, already small, and is used at the two call points where Agno's Vertex Claude implementation consumes tool definitions.
Extracting a broader provider-payload normalization layer would add indirection without removing active duplication.

Risk/tests:
The main behavior risk is accidentally stripping schema properties named `strict`; existing coverage in `tests/test_extra_kwargs.py:348` checks that schema fields are preserved and the caller's input is not mutated.
The request-path risk is Agno changing `_prepare_request_kwargs` or `_has_beta_features` signatures; existing coverage in `tests/test_extra_kwargs.py:360` exercises the current override behavior.
No production code was edited.
