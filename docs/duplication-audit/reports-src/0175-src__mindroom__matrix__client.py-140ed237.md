Summary: No meaningful duplication found.
`src/mindroom/matrix/client.py` is a thin curated re-export facade over Matrix implementation modules.
The only related patterns found are the source modules' own `__all__` export lists, which serve their local module APIs and are not duplicated behavior.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
MODULE_LEVEL	module	lines 1-60	related-only	"curated public seam", "__all__", "from mindroom.matrix.client", "matrix.client import", "client_delivery/client_room_admin/client_session/client_thread_history/client_visible_messages exports"	src/mindroom/matrix/client_session.py:157; src/mindroom/matrix/client_delivery.py:495; src/mindroom/matrix/client_room_admin.py:462; src/mindroom/matrix/client_thread_history.py:1192; src/mindroom/matrix/client_visible_messages.py:499; src/mindroom/matrix/__init__.py:1
```

Findings: No real duplicated behavior.
`src/mindroom/matrix/client.py:5` through `src/mindroom/matrix/client.py:60` imports selected public names from focused Matrix modules and declares a facade `__all__`.
The implementation modules also declare their own `__all__` lists at `src/mindroom/matrix/client_session.py:157`, `src/mindroom/matrix/client_delivery.py:495`, `src/mindroom/matrix/client_room_admin.py:462`, `src/mindroom/matrix/client_thread_history.py:1192`, and `src/mindroom/matrix/client_visible_messages.py:499`.
Those lists are related packaging metadata, but they are not functionally duplicated with the facade because the facade intentionally exposes only a curated subset and does not implement parsing, validation, IO, Matrix calls, or transformation logic.
`src/mindroom/matrix/__init__.py:1` is only a package docstring and does not duplicate the facade.

Proposed generalization: No refactor recommended.
Generating the facade `__all__` from source-module `__all__` values would reduce a small amount of export-list repetition, but it would weaken the explicit curated API boundary and could accidentally expose implementation helpers.

Risk/tests: No production change recommended.
If this facade is changed later, import smoke tests should cover representative legacy imports from `mindroom.matrix.client`, especially `login`, `matrix_client`, `send_message_result`, `edit_message_result`, `create_room`, `get_room_threads_page`, and `replace_visible_message`.
