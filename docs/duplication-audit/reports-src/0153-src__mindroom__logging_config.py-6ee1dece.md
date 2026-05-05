Summary: No meaningful duplication found.
`logging_config.py` is the canonical source for application structlog setup, logger acquisition, logger-level override parsing, and scoped structured log context.
Related logging code exists in CLI quiet config loading and durable JSONL audit sinks, but those paths have different lifecycles and output requirements.

```coverage-tsv
qualname	kind	lines	status	search_terms	candidates_checked
_NioValidationFilter	class	lines 32-52	none-found	"logging.Filter NioValidationFilter nio.responses Error validating response user_id room_id"	none
_NioValidationFilter.filter	method	lines 35-52	none-found	"filter(self record LogRecord nio.responses Error validating response user_id room_id"	none
_normalize_log_level	function	lines 55-60	none-found	"normalize log level getLevelNamesMapping Unsupported log level LOG_LEVEL logger level"	none
_parse_logger_level_overrides	function	lines 63-81	none-found	"MINDROOM_LOGGER_LEVELS logger:LEVEL split comma semicolon overrides normalize_log_level"	none
_build_logger_levels	function	lines 84-105	none-found	"default logger levels nio.crypto handler_level root logger dictConfig loggers"	none
setup_logging	function	lines 108-233	related-only	"setup_logging structlog.configure dictConfig ProcessorFormatter MINDROOM_LOG_FORMAT logging.basicConfig LoggerFactory FileHandler RotatingFileHandler"	src/mindroom/cli/config.py:572; src/mindroom/tool_system/tool_calls.py:423; src/mindroom/llm_request_logging.py:104
get_logger	function	lines 236-246	none-found	"get_logger structlog.get_logger logging.getLogger logger = get_logger"	src/mindroom/tool_system/tool_calls.py:423
bound_log_context	function	lines 249-251	related-only	"bound_log_context bound_contextvars merge_contextvars ContextVar room_id thread_id task_name"	src/mindroom/inbound_turn_normalizer.py:174; src/mindroom/response_attempt.py:123; src/mindroom/turn_controller.py:1046; src/mindroom/interactive.py:487; src/mindroom/scheduling.py:838; src/mindroom/matrix/cache/write_coordinator.py:503
```

Findings:

No real duplication found.

Related-only checks:

1. `src/mindroom/cli/config.py:572` configures structlog temporarily while loading config quietly.
   This overlaps with `setup_logging()` in using `structlog.stdlib.BoundLogger` and `structlog.stdlib.LoggerFactory`, but the behavior is not duplicated application logging setup.
   The CLI helper deliberately avoids file/console formatters, logger-level override parsing, contextvars, and startup logging so later callers can run full `setup_logging()`.

2. `src/mindroom/tool_system/tool_calls.py:423` builds a dedicated stdlib `logging.Logger` with a `RotatingFileHandler` for durable JSONL tool-call audit records.
   This is related to file logging, but it intentionally bypasses root structlog formatting, propagation, runtime log levels, and console output.
   It is not a duplicate of `get_logger()` because it needs isolated handlers and rotation.

3. `src/mindroom/llm_request_logging.py:104` writes daily JSONL files directly with `Path.open()`.
   This is another durable audit sink rather than process logging.
   It does not duplicate `setup_logging()` because each record is an application payload, not a log event handled by stdlib/structlog.

4. `bound_log_context()` is used repeatedly in request, turn, interactive, scheduling, and cache-write flows, including `src/mindroom/inbound_turn_normalizer.py:174`, `src/mindroom/response_attempt.py:123`, `src/mindroom/turn_controller.py:1046`, `src/mindroom/interactive.py:487`, `src/mindroom/scheduling.py:838`, and `src/mindroom/matrix/cache/write_coordinator.py:503`.
   These are call sites of the central helper rather than duplicate implementations.

Proposed generalization:

No refactor recommended.

Risk/tests:

No production change is recommended.
If this module is changed later, keep the existing focused coverage in `tests/test_logging_config.py` around JSON/text formatting, context binding/restoration, logger-level overrides, default nio levels, foreign logger context propagation, and exception rendering.
