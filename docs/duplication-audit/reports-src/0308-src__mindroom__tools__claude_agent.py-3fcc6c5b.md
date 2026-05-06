## Summary

The only duplicated behavior in `src/mindroom/tools/claude_agent.py` is the common registered-tool factory pattern: a typed factory lazily imports a toolkit class from `mindroom.custom_tools` and returns it.
This exact shape appears in several neighboring tool registration modules, but the Claude Agent metadata and config fields are specific to this tool.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
claude_agent_tools	function	lines 110-114	duplicate-found	"def *_tools() -> type", "from mindroom.custom_tools", "return *Tools", "claude_agent_tools"	src/mindroom/tools/attachments.py:38, src/mindroom/tools/coding.py:52, src/mindroom/tools/subagents.py:26, src/mindroom/tools/matrix_api.py:41, src/mindroom/tools/__init__.py:42
```

## Findings

### Registered toolkit factory boilerplate

- `src/mindroom/tools/claude_agent.py:110` defines `claude_agent_tools()`, imports `ClaudeAgentTools` inside the function, and returns the class.
- `src/mindroom/tools/attachments.py:38`, `src/mindroom/tools/coding.py:52`, `src/mindroom/tools/subagents.py:26`, and `src/mindroom/tools/matrix_api.py:41` use the same behavior for their custom toolkits.
- The duplication is functional boilerplate for registry-time factory functions: keep import-time cost and optional dependencies out of module import, while exposing a callable that returns a toolkit class.
- Differences to preserve: each module has distinct metadata, function names, dependencies, managed init args, execution target, and docstring text.

## Proposed Generalization

No refactor recommended.
The duplicated code is only two function-body statements per tool module, and the explicit factories make imports and types easy to follow.
A generic helper such as `lazy_custom_tool_factory("mindroom.custom_tools.claude_agent", "ClaudeAgentTools")` would reduce a few lines but weaken static typing and obscure the direct import target.

## Risk/tests

- Refactoring this pattern could affect tool registration import order and optional dependency loading.
- Any future generalization would need registry tests proving `register_tool_with_metadata` still records the correct factory, metadata, and function names for custom tool modules.
- Coverage is complete for the required symbol checklist.
