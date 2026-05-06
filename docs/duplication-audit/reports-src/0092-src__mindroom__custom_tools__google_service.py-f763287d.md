## Summary

No meaningful duplication found.
`src/mindroom/custom_tools/google_service.py` is already the shared implementation for per-thread Google API service caching used by the Gmail, Google Drive, Google Calendar, and Google Sheets wrappers.
The only other active `threading.local` usage under `src/mindroom` is tool registry plugin-registration scope state, which is related only by storage primitive and not by behavior.

## Coverage TSV

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_GoogleServiceThreadState	class	lines 9-11	related-only	threading.local class, ThreadState, per-thread service cache	src/mindroom/tool_system/registry_state.py:29; src/mindroom/tool_system/registry_state.py:68
_GoogleServiceThreadState.__init__	method	lines 10-11	none-found	thread-local init service Any None, self.service cache initialization	none
ThreadLocalGoogleServiceMixin	class	lines 14-28	related-only	ThreadLocalGoogleServiceMixin imports, google service cache mixin, service property	src/mindroom/custom_tools/gmail.py:26; src/mindroom/custom_tools/google_drive.py:30; src/mindroom/custom_tools/google_calendar.py:26; src/mindroom/custom_tools/google_sheets.py:32; src/mindroom/tool_system/registry_state.py:29
ThreadLocalGoogleServiceMixin._google_service_state	method	lines 17-19	none-found	_google_service_state, _google_service_thread_state, setdefault thread local cast	none
ThreadLocalGoogleServiceMixin.service	method	lines 22-24; lines 27-28	none-found	def service property, return thread-local service, self.service getter; service setter, set thread-local service, self.service assignment	none; tests/test_google_tool_wrappers.py:103; tests/test_google_tool_wrappers.py:134
```

## Findings

No real duplication found.

`src/mindroom/custom_tools/gmail.py:26`, `src/mindroom/custom_tools/google_drive.py:30`, `src/mindroom/custom_tools/google_calendar.py:26`, and `src/mindroom/custom_tools/google_sheets.py:32` all consume `ThreadLocalGoogleServiceMixin` rather than reimplementing it.
Those wrappers share other OAuth initialization patterns, but they do not duplicate the per-thread `service` storage behavior from the primary file.

`src/mindroom/tool_system/registry_state.py:29` also uses `threading.local`, with context managers at `src/mindroom/tool_system/registry_state.py:68` and `src/mindroom/tool_system/registry_state.py:85` storing temporary plugin registration metadata.
That is only related by its use of thread-local storage.
It is module-level scoped context state, while the primary file provides an instance mixin that caches a mutable Google API service object independently per worker thread.

Tests in `tests/test_google_tool_wrappers.py:94` and `tests/test_google_tool_wrappers.py:115` directly cover the intended non-sharing and first-access race behavior for the primary helper.
They are not production duplication candidates.

## Proposed Generalization

No refactor recommended.
The primary file is already a focused shared helper, and the other thread-local usage has different ownership, lifetime, and data semantics.

## Risk/Tests

Changing this helper would risk cross-thread reuse of Google API service objects, especially httplib2-backed services used by upstream Google toolkits.
Relevant tests are `tests/test_google_tool_wrappers.py::test_google_service_objects_are_thread_local` and `tests/test_google_tool_wrappers.py::test_google_service_state_first_access_is_thread_safe`.
No production code was edited.
