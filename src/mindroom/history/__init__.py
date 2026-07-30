"""Persisted history compaction helpers.

This package init is intentionally empty: import from the owning submodule
(``types``, ``runtime``, ``storage``, ``policy``, ``prompt_tokens``,
``manual``) so slim entry points that only need leaf history types (config
load, the sandbox runner, the tool registry) do not drag in the history
runtime and, through model loading, every provider SDK (#1436).
"""
