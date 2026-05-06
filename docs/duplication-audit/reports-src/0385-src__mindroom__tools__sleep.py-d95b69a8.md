Summary: No meaningful duplication found for `src/mindroom/tools/sleep.py`.
The module is a metadata-decorated factory for Agno `SleepTools`; related toolkit factories exist, but none duplicate the sleep-tool behavior or import/register `SleepTools`.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
sleep_tools	function	lines 42-46	related-only	sleep_tools SleepTools agno.tools.sleep function_names sleep enable_sleep all toolkit factory	src/mindroom/tools/sleep.py:42; src/mindroom/tools/__init__.py:114; src/mindroom/tools/__init__.py:235; src/mindroom/tools/calculator.py:27; src/mindroom/tools/scheduler.py:26; src/mindroom/tools/thread_tags.py:26; src/mindroom/tools/config_manager.py:34; src/mindroom/tools_metadata.json:5597
```

Findings:

- No duplicate `SleepTools` factory was found under `src`.
  `src/mindroom/tools/sleep.py:42` is the only source factory returning `agno.tools.sleep.SleepTools`.
  `src/mindroom/tools/__init__.py:114` and `src/mindroom/tools/__init__.py:235` only import and export that same factory.
- Several modules use the same metadata-decorated toolkit factory shape, including `src/mindroom/tools/calculator.py:27`, `src/mindroom/tools/scheduler.py:26`, `src/mindroom/tools/thread_tags.py:26`, and `src/mindroom/tools/config_manager.py:34`.
  This is related boilerplate rather than duplicated behavior because each registers distinct metadata and returns a distinct toolkit class.
- The `all` config field name appears in many tool registrations, but for `sleep` it is just one authored configuration option at `src/mindroom/tools/sleep.py:31`.
  The repeated field name is part of the existing tool metadata convention and does not duplicate the `sleep_tools` behavior.
- `src/mindroom/tools_metadata.json:5597` contains generated/exported metadata for the sleep tool.
  It mirrors registration output and should not be treated as an independent implementation duplicate.

Proposed generalization: No refactor recommended.
The common factory/decorator shape is intentionally explicit and keeps per-tool metadata local.
Extracting a helper for one-line factories would add indirection without removing active behavioral duplication in this primary file.

Risk/tests: No production change is recommended.
If the tool registration convention is refactored later, tests should cover registry loading, `sleep` metadata export, and tool function discovery for `function_names=("sleep",)`.
