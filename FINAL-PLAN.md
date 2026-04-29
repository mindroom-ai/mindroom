# ISSUE-211 Final Implementation Plan

## Goal

Make `mindroom_output_path` behave like the rare optional output-redirection escape hatch it is, not like a normal required tool argument.

## Acceptance criteria

1. Generated schemas for wrapped tools keep `mindroom_output_path` in `properties` but never in `required`.
2. The schema entry is explicitly optional/nullable/default-null and starts its description with `Optional`.
3. Empty or whitespace-only `mindroom_output_path` is treated as omission at runtime.
4. Valid explicit relative paths still write output files and return compact receipts.
5. Unsafe non-empty paths remain rejected with existing safety behavior.
6. Sandbox runner request handling mirrors wrapper normalization for empty/whitespace values.
7. Focused tests prove the normal, custom/skip-entrypoint, strict-mode, runtime, sandbox, and proxy paths.

## Implementation decisions

- Keep the fix small and centered on `src/mindroom/tool_system/output_files.py`.
- Add/use one canonical schema helper that overwrites the reserved `mindroom_output_path` property and removes it from top-level `required`.
- Do not monkey-patch Agno globally.
- Do not broadly disable strict mode unless the implementer proves this is the narrowest safe option; preferred design is post-processing/sanitizing after schema construction or an equivalent MindRoom-owned narrow hook.
- Add one prompt sentence only if needed: omit the argument for normal calls and do not pass an empty string.
- Add runtime normalization in the tool wrapper and in `src/mindroom/api/sandbox_runner.py` for empty/whitespace values.

## Files to inspect/touch

Primary:
- `src/mindroom/tool_system/output_files.py`
- `tests/test_tool_output_files.py`
- `src/mindroom/api/sandbox_runner.py`
- `tests/api/test_sandbox_runner_api.py`

Likely inspect/tiny touch:
- `src/mindroom/agent_prompts.py`
- `src/mindroom/tool_system/sandbox_proxy.py`
- `tests/test_sandbox_proxy.py`
- `src/mindroom/agents.py`
- `src/mindroom/tool_system/metadata.py`
- Agno `tools/function.py` in `.venv` read-only, to confirm strict processing behavior.

Do not touch unrelated tool hooks, provider code, Matrix/Cinny, or broad sandbox architecture.

## Tests required from implementer

Focused first pass:

```bash
uv run pytest tests/test_tool_output_files.py tests/test_sandbox_proxy.py tests/api/test_sandbox_runner_api.py -x -n 0 --no-cov -v
```

Then repository checks before handoff:

```bash
uv run pytest
uv run pre-commit run --all-files
```

If the full pytest command hits the known local libstdc++ issue, rerun inside `nix-shell shell.nix` as documented.

## Live-test evidence plan

After implementation/review, capture evidence for:

1. A trivial small-output prompt/tool trace omits `mindroom_output_path`.
2. An explicit save-to-file request still uses a valid relative path and creates the workspace file.
3. A crafted empty-string call returns the normal raw tool result rather than the current non-empty-path error.

Save evidence under `/tmp/ISSUE-211-evidence/`.
