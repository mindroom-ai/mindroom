# Report

## Summary of changes

Introduced a per-refresh-pass `_KnowledgePathFilter` for knowledge file inclusion.
The filter now applies lexical confinement and cheap semantic include, exclude, hidden-file, and extension checks before per-file symlink checks, `Path.resolve(strict=True)`, and `is_file()`.
Directory symlink checks are cached within one listing pass for local and Git-tracked knowledge file discovery.
Included files still reject symlinked files, files under symlinked directories, paths outside the knowledge root, and paths whose strict resolved target escapes the root.
Added a regression test proving unsupported extensions are filtered before per-file symlink and strict resolution work.

## Tests run and results

`uv run pytest tests/test_knowledge_manager.py -k "unsupported_extensions_before_filesystem_safety_checks or symlink_file_escape or symlinked_directory_escape" -q` passed with 3 tests.
`uv run ruff check src/mindroom/knowledge/manager.py tests/test_knowledge_manager.py` passed.
`uv run pytest tests/test_knowledge_manager.py -q` passed with 158 tests.
`uv run pytest` passed with 6247 tests passed and 56 skipped.
An earlier full-suite attempt had four load-sensitive failures outside this change.
Those failures passed on direct reruns, and the second full-suite run passed cleanly.
`uv sync --all-extras` completed successfully.
`uv run pre-commit run --all-files` passed.

## Remaining risks/questions

The knowledge-specific test coverage passed, including the existing symlink escape tests.
The first full repository pytest attempt had load-sensitive failures outside this knowledge refresh change, but direct reruns and a second full run passed.
No broad knowledge indexing or embedding semantics were changed.

## Suggested PR title

Reduce filesystem churn during knowledge file filtering

## Suggested PR body

### Summary

- Filter knowledge files by cheap relative-path and extension rules before expensive filesystem safety checks.
- Cache directory symlink checks for each knowledge listing pass.
- Keep strict root confinement and symlink rejection for files that remain eligible for indexing.
- Add regression coverage for unsupported files bypassing per-file symlink and strict resolve checks.

### Tests

- `uv run ruff check src/mindroom/knowledge/manager.py tests/test_knowledge_manager.py`
- `uv run pytest tests/test_knowledge_manager.py -q`
- `uv run pytest`
- `uv sync --all-extras`
- `uv run pre-commit run --all-files`
